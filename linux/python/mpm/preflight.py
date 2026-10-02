"""Preflight: support matrix, TUN capability, port occupancy (D3=B, D9a=A).

Degrading from TUN to mixed-port is *explicit*: a reason is always recorded so
status/test can report ``DEGRADED`` with a cause.  Nothing here ever writes a
system change (no modprobe, no nftables, no sysctl, no resolv.conf).
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field
from typing import Iterable

from .errors import Unsupported
from .executor import Executor

SUPPORTED_DISTROS = {
    ("ubuntu", "22.04"),
    ("ubuntu", "24.04"),
    ("debian", "12"),
}
SUPPORTED_ARCHES = {"x86_64", "aarch64"}

TUN_DEV = "/dev/net/tun"
AVX2_FLAGS_PATH = "/proc/cpuinfo"


@dataclass
class HostFacts:
    """Everything preflight needs, injectable so tests never probe the host."""

    id_like: dict[str, str] = field(default_factory=dict)
    machine: str = ""
    has_tun_device: bool = False
    tun_writable: bool = False
    avx2: bool = False
    is_root: bool = False
    has_systemd: bool = False
    inside_container: bool = False
    sysctl_ip_forward: str = ""
    resolved_active: bool = False

    def distro(self) -> str:
        return (self.id_like.get("ID") or "").strip().lower()

    def version(self) -> str:
        return (self.id_like.get("VERSION_ID") or "").strip().strip('"')

    def codename(self) -> str:
        return (self.id_like.get("VERSION_CODENAME") or "").strip()


@dataclass
class Preflight:
    supported: bool
    reasons: list[str]
    mode: str  # "tun" | "mixed"
    degraded_reasons: list[str]
    arch: str
    asset_suffix: str  # amd64-v3 | amd64-compatible | arm64
    distro: str
    distro_version: str
    root: bool
    systemd: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "reasons": list(self.reasons),
            "mode": self.mode,
            "degraded": self.mode == "mixed",
            "degraded_reasons": list(self.degraded_reasons),
            "arch": self.arch,
            "asset_suffix": self.asset_suffix,
            "distro": f"{self.distro}{self.distro_version}",
            "root": self.root,
            "systemd": self.systemd,
        }


def _os_release(path: str = "/etc/os-release") -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return data


def _has_avx2(path: str = AVX2_FLAGS_PATH) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("flags"):
                    return " avx2 " in (" " + line.split(":", 1)[-1].strip() + " ")
    except OSError:
        return False
    return False


def collect_facts(*, root: str = "/", probe: bool = True) -> HostFacts:
    """Read host facts.  With ``probe=False`` nothing is touched (unit tests)."""
    facts = HostFacts(machine=platform.machine())
    osr = _os_release(os.path.join(root, "etc/os-release")) if root != "/" else _os_release()
    facts.id_like = osr
    if not probe:
        return facts
    facts.machine = platform.machine()
    facts.avx2 = _has_avx2()
    facts.is_root = getattr(os, "geteuid", lambda: 0)() == 0
    facts.has_systemd = os.path.isdir(os.path.join(root, "run/systemd/system")) or os.path.isdir(
        "/run/systemd/system"
    )
    facts.inside_container = bool(os.environ.get("container") or _os_release().get("CHASSIS"))
    tun = os.path.join(root, "dev/net/tun") if root != "/" else TUN_DEV
    facts.has_tun_device = os.path.exists(tun)
    try:
        fd = os.open(tun, os.O_RDWR)
    except OSError:
        facts.tun_writable = False
    else:
        os.close(fd)
        facts.tun_writable = True
    try:
        with open("/proc/sys/net/ipv4/ip_forward", encoding="utf-8") as handle:
            facts.sysctl_ip_forward = handle.read().strip()
    except OSError:
        facts.sysctl_ip_forward = ""
    facts.resolved_active = os.path.islink("/etc/resolv.conf") and "resolved" in os.readlink(
        "/etc/resolv.conf"
    )
    return facts


def evaluate(facts: HostFacts) -> Preflight:
    """Pure decision function: support matrix + TUN -> mixed degradation."""
    reasons: list[str] = []
    degraded: list[str] = []

    arch = facts.machine or "unknown"
    if arch not in SUPPORTED_ARCHES:
        reasons.append(f"unsupported architecture: {arch} (x86_64/arm64 only, D9a=A)")
        supported = False
    else:
        supported = True

    if facts.inside_container and not facts.has_systemd:
        reasons.append("no systemd detected (container without systemd is unsupported)")
        supported = False

    if not facts.has_systemd:
        reasons.append("systemd is required for the system profile")
        supported = False

    if not facts.is_root:
        reasons.append("root privileges are required for the system profile")
        supported = False

    if supported:
        distro = facts.distro()
        release = facts.version()
        if not facts.id_like or not distro or not release:
            # /etc/os-release missing, empty or without ID/VERSION_ID: the
            # support matrix cannot be evaluated, so the host is NOT supported.
            # Skipping the check here would silently accept unknown systems.
            reasons.append(
                "/etc/os-release is missing or unrecognisable (no ID/VERSION_ID): "
                "cannot verify against the support matrix "
                "(supported: Ubuntu 22.04/24.04, Debian 12)"
            )
            supported = False
        elif (distro, release) not in SUPPORTED_DISTROS:
            reasons.append(
                "untested distribution release: %s %s (supported: Ubuntu 22.04/24.04, Debian 12)"
                % (distro, release)
            )
            supported = False

    if supported:
        if not facts.has_tun_device:
            degraded.append(f"{TUN_DEV} is absent (kernel tun module not loaded; not auto-loaded)")
        elif not facts.tun_writable:
            degraded.append(f"{TUN_DEV} is not writable (missing CAP_NET_ADMIN or device perms)")
    mode = "tun" if supported and not degraded else "mixed"

    if arch == "aarch64":
        asset_suffix = "arm64"
    elif arch == "x86_64":
        asset_suffix = "amd64-v3" if facts.avx2 else "amd64-compatible"
    else:
        asset_suffix = ""

    return Preflight(
        supported=supported,
        reasons=reasons,
        mode=mode,
        degraded_reasons=degraded,
        arch=arch,
        asset_suffix=asset_suffix,
        distro=facts.distro(),
        distro_version=facts.version(),
        root=facts.is_root,
        systemd=facts.has_systemd,
    )


def preflight(facts: HostFacts | None = None, *, strict: bool = True) -> Preflight:
    result = evaluate(facts if facts is not None else collect_facts())
    if strict and not result.supported:
        raise Unsupported("host is not supported: " + "; ".join(result.reasons))
    return result


# ---- port occupancy ---------------------------------------------------------

_LISTEN_STATE = {"LISTEN", "LISTENING", "0A"}


@dataclass
class Listener:
    address: str
    port: int
    inode: str


def parse_proc_net_tcp(text: str) -> list[Listener]:
    """Parse /proc/net/tcp{,6}; only LISTEN sockets matter."""
    listeners: list[Listener] = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 10:
            continue
        local = cols[1]
        state = cols[3]
        if state.upper() not in _LISTEN_STATE:
            continue
        addr_hex, port_hex = local.rsplit(":", 1)
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        listeners.append(Listener(address=addr_hex, port=port, inode=cols[9]))
    return listeners


def _cgroup_to_unit(cgroup_text: str) -> str:
    """Extract a systemd unit name from a /proc/<pid>/cgroup payload.

    Handles cgroup v1 (``<hierarchy>:<controllers>:<path>``) and v2
    (``0::<path>``) by taking the text after the final colon.
    """
    for line in cgroup_text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        path = line.rsplit(":", 1)[-1].strip()
        if not path.startswith("/"):
            continue
        for part in reversed(path.split("/")):
            if part.endswith((".service", ".scope")):
                return part
    return ""


def read_owning_unit(pid: str, *, proc_root: str = "/proc") -> str:
    """Read-only lookup of the unit owning ``pid``; never signals anything."""
    path = os.path.join(proc_root, str(pid), "cgroup")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return _cgroup_to_unit(handle.read())
    except OSError:
        return ""


def parse_ss_users(line: str) -> str:
    """Extract the pid from ``users:(("sshd",pid=1234,fd=3))``."""
    match = re.search(r"\bpid=(\d+)", line)
    return match.group(1) if match else ""


def port_occupants(
    executor: Executor, ports: Iterable[int], *, proc_root: str = "/proc"
) -> dict[int, str]:
    """Map every already-LISTENing port to the systemd unit that owns it.

    Empty map when nothing is listening.  This function only observes; it never
    signals, kills or reconfigures anything.  A port owned by another unit makes
    ``start`` refuse to run (spec section 7).
    """
    wanted = {int(p) for p in ports}
    listening: set[int] = set()
    for name in ("net/tcp", "net/tcp6"):
        path = os.path.join(proc_root, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for listener in parse_proc_net_tcp(text):
            if listener.port in wanted:
                listening.add(listener.port)

    found: dict[int, str] = {}
    for port in sorted(listening):
        result = executor.run(["ss", "-H", "-ltnp", f"( sport = :{port} )"])
        unit = ""
        for line in result.stdout.splitlines():
            pid = parse_ss_users(line)
            if pid:
                unit = read_owning_unit(pid, proc_root=proc_root)
                break
        found[port] = unit or "foreign-process"
    return found


__all__ = [
    "AVX2_FLAGS_PATH",
    "HostFacts",
    "Listener",
    "Preflight",
    "SUPPORTED_ARCHES",
    "SUPPORTED_DISTROS",
    "TUN_DEV",
    "collect_facts",
    "evaluate",
    "port_occupants",
    "preflight",
]
