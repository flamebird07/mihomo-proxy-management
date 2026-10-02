"""Test support: sandbox layout, fake systemd state machine, mock supply, canaries.

Everything here is host-free:

* a throwaway root prefix stands in for ``/``
* :class:`FakeSystemdState` + :class:`mpm.executor.FakeExecutor` replace
  ``systemctl`` (no unit is ever created, started or stopped on the host)
* mock gzipped ELF payloads + a mock lock replace real downloads
* a mock transport replaces the controller socket
* secrets are runtime-generated canaries pointing at ``example.invalid``
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import secrets as pysecrets
import shutil
import struct
import sys
import tempfile
import tarfile
from dataclasses import dataclass, field
from typing import Any

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LINUX_DIR = os.path.dirname(TESTS_DIR)
PKG_ROOT = os.path.join(LINUX_DIR, "python")
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from mpm import lifecycle  # noqa: E402
from mpm.atomicio import write_atomic  # noqa: E402
from mpm.controller import Controller  # noqa: E402
from mpm.executor import FakeExecutor  # noqa: E402
from mpm.paths import Layout, UNIT_NAME  # noqa: E402
from mpm.preflight import HostFacts  # noqa: E402
from mpm.sanitize import HOST  # noqa: E402

UNIT = UNIT_NAME
RUN_AS_ROOT = os.geteuid() == 0

FOREIGN = "subscription-foreign"
CN = "subscription-cn"

# fixed mock geo payload; the lock digest is computed from these same bytes
GEO_MOCK_BYTES = b"geo-data-mock"


def token(prefix: str) -> str:
    """Runtime canary value - never hardcoded anywhere in the repo."""
    return f"{prefix}-{pysecrets.token_hex(12)}"


def canary_url(kind: str = "foreign") -> str:
    """URL that cannot resolve (RFC 2606 .invalid) plus a unique credential."""
    return f"https://{kind}.example.invalid/sub?token={token('tok')}&clash=1"


def make_elf(machine: int = 0x3E, *, magic: bytes = b"\x7fELF") -> bytes:
    """Minimal fake ELF header with correct e_machine at offset 18 (never run)."""
    ident = bytearray(magic + b"\x02\x01\x01" + b"\x00" * 9)
    ident[4] = 2 if machine in (0x3E, 0xB7) else 1
    header = struct.pack(
        "<HHIQQQIHHHHHH",
        2,           # e_type  = ET_EXEC
        machine,     # e_machine
        1,           # e_version
        64,          # e_entry
        0,           # e_phoff
        0,           # e_shoff
        0,           # e_flags
        64,          # e_ehsize
        56, 8, 0, 0, 0,
    )
    return bytes(ident) + header + b"\x90" * 128


def gz(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(payload)
    return buffer.getvalue()


def tar_gz(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def build_lock(directory: str, *, binary_payload: bytes, geo_payload: bytes = b"geo-data-mock") -> str:
    """Write a mock lock whose digests match the mock payloads we serve."""
    binary_digest = hashlib.sha256(binary_payload).hexdigest()
    geo_digest = hashlib.sha256(geo_payload).hexdigest()
    tag = "v0.0.0-mock"

    def asset(key: str, name: str, digest: str, size: int, kind: str, install_as: str = "") -> dict:
        repo = "MetaCubeX/mihomo" if kind == "binary" else "MetaCubeX/meta-rules-dat"
        item: dict[str, Any] = {
            "key": key,
            "name": name,
            "kind": kind,
            "url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
            "sha256": digest,
            "size": size,
            "source": "local mock fixture (not a real release)",
        }
        if install_as:
            item["install_as"] = install_as
        return item

    payload = {
        "schema_version": 1,
        "mihomo_tag": tag,
        "geo_tag": "mock",
        "policy": {"pinning": "mock"},
        "assets": [
            asset("linux-amd64-v3", f"mihomo-linux-amd64-v3-{tag}.gz", binary_digest, len(binary_payload), "binary"),
            asset(
                "linux-amd64-compatible",
                f"mihomo-linux-amd64-compatible-{tag}.gz",
                binary_digest,
                len(binary_payload),
                "binary",
            ),
            asset("linux-arm64", f"mihomo-linux-arm64-{tag}.gz", binary_digest, len(binary_payload), "binary"),
            asset("geoip", "geoip.dat", geo_digest, len(geo_payload), "geo", "GeoIP.dat"),
            asset("geosite", "geosite.dat", geo_digest, len(geo_payload), "geo", "GeoSite.dat"),
        ],
    }
    path = os.path.join(directory, "mock.lock.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
    return path


@dataclass
class FakeSystemdState:
    """Unit state machine so idempotency is assertable without a host."""

    active: bool = False
    enabled: bool = False
    unit_installed: bool = False
    main_pid: str = "4242"

    def show_output(self) -> str:
        return "\n".join(
            [
                "active" if self.active else "inactive",
                "running" if self.active else "dead",
                "enabled" if self.enabled else "disabled",
                self.main_pid if self.active else "0",
            ]
        )


@dataclass
class MockControllerTransport:
    """Stand-in for the mihomo REST API; records headers for assertions."""

    responses: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    version_payload: dict[str, Any] = field(default_factory=lambda: {"version": "mock-1.0.0"})
    providers_payload: dict[str, Any] = field(default_factory=dict)
    proxies_payload: dict[str, Any] = field(default_factory=lambda: {"proxies": {"PROXY": {}}})

    def _proxies_document(self) -> dict[str, Any]:
        """``/proxies`` including per-node ``alive`` (as mihomo really does).

        Node liveness comes from the provider fixtures, so a test that marks
        every node dead is visible through both ``/providers`` and ``/proxies``.
        """
        proxies: dict[str, Any] = dict(self.proxies_payload.get("proxies") or {})
        for name, entry in (self.providers_payload or {}).items():
            if not isinstance(entry, dict):
                continue
            for node in entry.get("all") or []:
                # only inject what the fixture actually states: a provider entry
                # without an ``alive`` key must stay "liveness unknown"
                if isinstance(node, dict) and node.get("name") and "alive" in node:
                    proxies[str(node["name"])] = {
                        "type": "socks5",
                        "udp": True,
                        "alive": bool(node["alive"]),
                    }
        return {"proxies": proxies}

    def __call__(self, method: str, url: str, headers: dict, body: Any, timeout: float):
        tail = url.split("://", 1)[-1]
        path = "/" + tail.split("/", 1)[1] if "/" in tail else "/"
        endpoint = path.split("?")[0]
        key = f"{method} {endpoint}"
        self.calls.append({"method": method, "path": endpoint, "headers": dict(headers), "key": key})
        if key in self.responses:
            return self.responses[key]
        if endpoint.startswith("/version"):
            return 200, json.dumps(self.version_payload).encode()
        if endpoint.startswith("/providers/proxies/") and endpoint != "/providers/proxies":
            name = endpoint[len("/providers/proxies/"):]
            if name.endswith("/healthcheck"):
                name = name[: -len("/healthcheck")].strip("/")
                return (200, b"{}") if name in self.providers_payload else (404, b"{}")
            entry = self.providers_payload.get(name)
            if entry is None:
                return 404, b"{}"
            return (204, b"") if method == "PUT" else (200, json.dumps(entry).encode())
        if endpoint.startswith("/providers/proxies"):
            return 200, json.dumps({"providers": self.providers_payload}).encode()
        if endpoint.startswith("/proxies"):
            if "/delay" in endpoint:
                return 200, json.dumps({"delay": 42}).encode()
            return 200, json.dumps(self._proxies_document()).encode()
        if method == "PUT":
            return 204, b""
        return 404, b"{}"


def provider_entry(name: str, nodes: int, *, alive: bool = True) -> dict[str, Any]:
    """A provider view of ``nodes`` nodes, each with an explicit ``alive`` flag.

    mihomo reports nodes under ``all`` as ``{name, alive}`` objects and the bare
    names under ``vehicle``; both shapes are produced here so a test can express
    "nodes exist but every one of them is dead".
    """
    names = [f"{name}-node-{i}" for i in range(nodes)]
    return {
        "name": name,
        "vehicle": names,
        "all": [{"name": node, "alive": bool(alive)} for node in names],
        "proxies": names,
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def make_facts(
    *,
    tun: bool = True,
    machine: str = "x86_64",
    avx2: bool = True,
    root: bool | None = None,
    systemd: bool = True,
    distro: str = "ubuntu",
    version: str = "24.04",
    container: bool = False,
) -> HostFacts:
    facts = HostFacts()
    facts.id_like = {"ID": distro, "VERSION_ID": version, "VERSION_CODENAME": "mock"}
    facts.machine = machine
    facts.has_tun_device = tun
    facts.tun_writable = tun
    facts.avx2 = avx2
    facts.is_root = RUN_AS_ROOT if root is None else root
    facts.has_systemd = systemd
    facts.inside_container = container
    return facts


class CapturingStream:
    """In-memory stream so every output byte can be scanned for canaries."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()

    def write(self, data: str) -> int:
        return self.buffer.write(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    @property
    def text(self) -> str:
        return self.buffer.getvalue()

    def reset(self) -> None:
        self.buffer = io.StringIO()


class Sandbox:
    """A complete fake host: root prefix, secret file, fakes, captured output."""

    def __init__(
        self,
        *,
        tun: bool = True,
        cn: bool = False,
        facts: HostFacts | None = None,
        machine: str = "x86_64",
        avx2: bool = True,
        binary_bytes: bytes | None = None,
        foreign_url: str | None = None,
        cn_url: str | None = None,
    ) -> None:
        self.root = tempfile.mkdtemp(prefix="mpm-test-")
        self.layout = Layout(self.root)
        self.out = CapturingStream()
        self.err = CapturingStream()
        self.canary_foreign = foreign_url or canary_url("foreign")
        # The CN canary always exists so rendering tests can reach it; whether
        # it is *configured on this host* is decided by ``self.cn_enabled``.
        self.cn_enabled = cn
        self.canary_cn = cn_url or canary_url("cn")
        self.canary_secret = token("ctrl")
        self.canary_health = f"https://cn-health.example.invalid/generate_204?probe={token('probe')}"
        HOST.forget_all()

        self.facts = facts or make_facts(tun=tun, machine=machine, avx2=avx2)
        self.systemd_state = FakeSystemdState()
        self.verify_failure = False
        self._ss_line: dict[str, str] = {}
        self.executor = FakeExecutor()
        self._wire_executor()
        self.transport = MockControllerTransport()
        self.providers: dict[str, dict[str, Any]] = {FOREIGN: provider_entry(FOREIGN, 3)}
        if self.cn_enabled:
            self.providers[CN] = provider_entry(CN, 2)
        self.transport.providers_payload = self.providers

        self.mock_binary = binary_bytes if binary_bytes is not None else gz(make_elf())
        self.lock_path = build_lock(self.root, binary_payload=self.mock_binary)
        self.install_report: Any = None
        self._proc_entries: list[tuple[int, int]] = []
        self.proc_root = os.path.join(self.root, "proc")
        os.makedirs(os.path.join(self.proc_root, "net"), exist_ok=True)
        self._write_proc(entries=[])
        self.environ: dict[str, str] = {}

    # ---- fixtures ----------------------------------------------------
    def _wire_executor(self) -> None:
        state = self.systemd_state

        def systemctl(argv: list[str]) -> tuple[int, str, str]:
            if "show" in argv:
                return 0, state.show_output(), ""
            if "list-unit-files" in argv:
                # the unit is "installed" exactly when we wrote the unit file
                installed = os.path.isfile(self.layout.unit)
                return (0, f"{UNIT} enabled\n" if installed else "", "")
            if "is-enabled" in argv:
                return (0, "enabled\n" if state.enabled else "disabled\n", "")
            if "is-active" in argv:
                return (0, "active\n" if state.active else "inactive\n", "")
            if "start" in argv:
                if not os.path.isfile(self.layout.unit):
                    return (1, "", f"Failed to start {UNIT}: Unit {UNIT} not loaded.")
                state.active = True
            elif "stop" in argv or ("disable" in argv and "--now" in argv):
                state.active = False
                if "disable" in argv:
                    state.enabled = False
            elif "enable" in argv:
                if not os.path.isfile(self.layout.unit):
                    return (1, "", f"Failed to enable {UNIT}: No unit files specified.")
                state.enabled = True
            return (0, "", "")

        for name in ("systemctl show", "systemctl list-unit-files", "systemctl is-active",
                     "systemctl is-enabled", "systemctl start", "systemctl stop",
                     "systemctl enable", "systemctl disable",
                     "systemctl daemon-reload", "systemctl reset-failed"):
            self.executor.set(name, systemctl)
        self.executor.set("systemd-analyze verify", lambda argv: self._verify(argv))
        self.executor.set("ss", lambda argv: self._ss(argv))

    def _verify(self, argv: list[str]) -> tuple[int, str, str]:
        """Mirror systemd-analyze: a missing unit file is a hard error."""
        path = argv[-1] if argv else ""
        if not os.path.isfile(path):
            return (1, "", f"{path}: Unit file does not exist, won't run.")
        if self.verify_failure:
            return (1, "", "mock unit defect")
        return (0, "", "")

    def _ss(self, argv: list[str]) -> tuple[int, str, str]:
        port = ""
        for item in argv:
            if item.startswith("( sport = :"):
                port = item.split(":", 1)[1].rstrip(" )")
        line = getattr(self, "_ss_line", {}).get(port, "")
        return (0, line, "")

    def add_port_entry(self, port: int, *, inode: int = 1234) -> None:
        self._proc_entries.append((port, inode))
        self._write_proc(entries=list(self._proc_entries))

    def _write_proc(self, *, entries: list[tuple[int, int]]) -> None:
        header = "   sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
        lines = [header]
        for index, (port, inode) in enumerate(entries, start=1):
            lines.append(
                f"   {index}: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 "
                f"00000000     0        0 {inode} 1 0000000000000000 100 0 0 10 0"
            )
        net = os.path.join(self.proc_root, "net")
        os.makedirs(net, exist_ok=True)
        with open(os.path.join(net, "tcp"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        with open(os.path.join(net, "tcp6"), "w", encoding="utf-8") as handle:
            handle.write(header + "\n")

    def make_port_owner(self, port: int, *, unit: str, pid: str = "9999") -> None:
        """Simulate ``port`` being LISTENed by ``unit`` (unit="" = orphan pid)."""
        self.add_port_entry(port)
        holder = os.path.join(self.proc_root, pid)
        if unit:
            os.makedirs(holder, exist_ok=True)
            with open(os.path.join(holder, "cgroup"), "w", encoding="utf-8") as handle:
                handle.write(f"0::/system.slice/{unit}/\n")
        else:
            shutil.rmtree(holder, ignore_errors=True)
        self._ss_line[str(port)] = (
            f'LISTEN 0 128 127.0.0.1:{port} 0.0.0.0:* users:(("x",pid={pid},fd=6))\n'
        )

    def set_binary_test(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Control the fake ``mihomo -t`` result (keyed by the installed path)."""
        self.executor.set(self.layout.binary, (returncode, stdout, stderr))

    def clear_port_occupancy(self) -> None:
        self._proc_entries = []
        self._write_proc(entries=[])
        self._ss_line = {}

    def write_secret_file(self, *, mode: int = 0o600, with_cn: bool | None = None, extra: str = "") -> str:
        include_cn = self.cn_enabled if with_cn is None else with_cn
        lines = [f"MPM_SUBSCRIPTION_URL={self.canary_foreign}"]
        if include_cn:
            lines.append(f"MPM_CN_SUBSCRIPTION_URL={self.canary_cn}")
            lines.append(f"MPM_CN_HEALTHCHECK_URL={self.canary_health}")
        lines.append(f"MPM_CONTROLLER_SECRET={self.canary_secret}")
        path = self.layout.subscription_env
        write_atomic(path, "\n".join(lines) + "\n" + extra, mode=mode)
        return path

    def use_env_secrets(self, *, include_cn: bool | None = None) -> None:
        include_cn = self.cn_enabled if include_cn is None else include_cn
        self.environ = {"MPM_SUBSCRIPTION_URL": self.canary_foreign,
                        "MPM_CONTROLLER_SECRET": self.canary_secret}
        if include_cn:
            self.environ["MPM_CN_SUBSCRIPTION_URL"] = self.canary_cn
            self.environ["MPM_CN_HEALTHCHECK_URL"] = self.canary_health

    def ctx(
        self,
        *,
        enable: bool = False,
        dry_run: bool = False,
        quiet: bool = False,
        system_profile: bool | None = None,
        secret_file: str | None = None,
        environ: dict[str, str] | None = None,
        lock_path: str | None = None,
        fetch_override: object = ...,
    ) -> lifecycle.Context:
        return lifecycle.Context(
            layout=self.layout,
            executor=self.executor,
            facts=self.facts,
            out=self.out,  # type: ignore[arg-type]
            err=self.err,  # type: ignore[arg-type]
            lock_path=lock_path or self.lock_path,
            environ=dict(self.environ if environ is None else environ),
            secret_file=secret_file,
            enable=enable,
            sleeper=lambda _s: None,
            controller_factory=self._controller,
            fetch_override=self.fetch_mock if fetch_override is Ellipsis else fetch_override,
            proc_root=self.proc_root,
            dry_run=dry_run,
            quiet=quiet,
            # production requires root-owned secret files.  An unprivileged CI
            # runner keeps the identical code path except for that ownership
            # check (see mpm.secrets_io.resolve).
            system_profile=RUN_AS_ROOT if system_profile is None else system_profile,
        )

    def _controller(self, secret: str) -> Controller:
        return Controller(secret=secret, transport=self.transport)

    def fetch_mock(self, url: str, dest: str) -> None:
        """Serve mock payloads into the 0700 staging dir (no network, no exec)."""
        name = url.rsplit("/", 1)[-1]
        payload = self.mock_binary if name.startswith("mihomo-") else GEO_MOCK_BYTES
        with open(dest, "wb") as handle:
            handle.write(payload)
        os.chmod(dest, 0o600)

    def forget_canaries(self) -> None:
        """Drop registered secrets so a *changed* canary is not masked."""
        HOST.forget_all()

    def outputs(self) -> str:
        return self.out.text + self.err.text

    def canaries(self) -> list[str]:
        """Every secret value this sandbox knows about, for leak scanning.

        The CN canary is included even when CN is not configured: a value that
        was never installed must not appear anywhere either.
        """
        return [self.canary_foreign, self.canary_cn, self.canary_secret, self.canary_health]

    def installed(self, *, enable: bool = False, cn: bool | None = None) -> lifecycle.Context:
        """Convenience: secret file + install, returns the live context."""
        self.write_secret_file(with_cn=self.cn_enabled if cn is None else cn)
        ctx = self.ctx(enable=enable)
        self.install_report = lifecycle.install(ctx, quiet=True)
        return ctx

    def rebuild_lock(self, *, binary_payload: bytes | None = None, geo_payload: bytes | None = None) -> str:
        """Re-pin the mock lock (used by supply-chain negative tests)."""
        self.lock_path = build_lock(
            self.root,
            binary_payload=self.mock_binary if binary_payload is None else binary_payload,
            geo_payload=GEO_MOCK_BYTES if geo_payload is None else geo_payload,
        )
        return self.lock_path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        HOST.forget_all()


def mutate_lock(path: str, *, key: str, **changes: Any) -> str:
    """Edit one asset field in a mock lock (supply-chain negative tests)."""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.loads(handle.read())
    for entry in payload["assets"]:
        if entry["key"] == key:
            for field_name, value in changes.items():
                if value is _DELETE:
                    entry.pop(field_name, None)
                else:
                    entry[field_name] = value
    target = os.path.join(os.path.dirname(path), "mutated.%s.lock.json" % token("lock"))
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
    return target


class _Delete:
    __slots__ = ()


_DELETE = _Delete()
DELETE = _DELETE  # same sentinel object: mutate_lock compares identity


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def file_mode(path: str) -> int:
    return os.stat(path).st_mode & 0o7777


__all__ = [
    "CN",
    "DELETE",
    "CapturingStream",
    "FOREIGN",
    "FakeSystemdState",
    "MockControllerTransport",
    "mutate_lock",
    "RUN_AS_ROOT",
    "Sandbox",
    "UNIT",
    "build_lock",
    "canary_url",
    "file_mode",
    "gz",
    "make_elf",
    "make_facts",
    "provider_entry",
    "read_text",
    "tar_gz",
    "token",
]
