"""Detect an external VPN (e.g. Nextin) that already owns routing/DNS.

The 2026-11-14 outage was two stacks taking over the machine at once: a
LaunchDaemon started mihomo in TUN mode at boot while Nextin's
NetworkExtension already owned ``utun`` 198.18.0.1, port 7890 and fake-ip DNS.
Nothing in mihomo's own logs says "another VPN is here", so this module looks
at the host directly:

* ``scutil --nc list`` - connected VPN services (name + bundle id);
* ``lsof -nP -iTCP:<port> -sTCP:LISTEN -Fpc`` - who listens on 7890/9090;
* ``launchctl print system/<label>`` - the PID of *our* LaunchDaemon job;
* ``ps -o comm= -p <pid>`` - the executable path of a port owner;
* ``route -n get default`` - whether the default route sits on a ``utun``;
* ``ifconfig`` - utun addresses inside 198.18.0.0/15.

Project identity is never guessed from a process *name*: a port is ours only
when the owner is the PID of our launchd job or its executable is
``.../libexec/mihomo-proxy-management/current/mihomo``.  Anything else - a
``mihomo``-named process started by hand, a different build - is foreign.

Every parser is pure; :func:`detect` only reads through an injected
:class:`~mpm_common.executor.Executor`, so the tests never touch a real host.
Any failure while detecting is treated as a conflict (fail-safe): starting a
second network stack is far worse than refusing to start.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any

from mpm_common.executor import Executor
from mpm_common.sanitize import HOST

from .launchdctl import parse_print
from .paths import LABEL

#: Ports this project owns (mixed-port + controller).
PROJECT_PORTS = (7890, 9090)
CONTROLLER_PORT = 9090

#: The installed mihomo binary; its path (not its process name) identifies us.
PROJECT_EXECUTABLE = "/usr/local/libexec/mihomo-proxy-management/current/mihomo"

#: Nextin's fake-ip block; mihomo must never use it (see config.FAKE_IP_RANGE).
FOREIGN_ADDRESS_BLOCK = "198.18.0.0/16"
PROJECT_ADDRESS_BLOCK = "198.19.0.0/16"
UTUN_ADDRESS_SUPERNET = "198.18.0.0/15"


@dataclass(frozen=True)
class VpnConnection:
    name: str
    bundle: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "bundle": self.bundle}


@dataclass(frozen=True)
class PortOwner:
    port: int
    pid: int | None
    command: str

    def to_dict(self) -> dict[str, Any]:
        return {"port": self.port, "pid": self.pid, "command": self.command}


@dataclass
class ConflictReport:
    vpn_connected: list[VpnConnection] = field(default_factory=list)
    port_owners: list[PortOwner] = field(default_factory=list)
    foreign_default_utun: str = ""
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    error: str = ""
    project_pids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "vpn_connected": [item.to_dict() for item in self.vpn_connected],
            "port_owners": [item.to_dict() for item in self.port_owners],
            "foreign_default_utun": self.foreign_default_utun,
            "project_pids": list(self.project_pids),
            "reasons": [HOST.text(reason) for reason in self.reasons],
            "error": HOST.text(self.error),
        }


# ---- pure parsers ---------------------------------------------------------


_CONNECTED = re.compile(r"\(Connected\)", re.IGNORECASE)
_QUOTED = re.compile(r'"([^"]+)"')
_BUNDLE = re.compile(r"\[(?:VPN:)?([^\]]+)\]")


def parse_scutil_nc_list(text: str) -> list[VpnConnection]:
    """Connected services from ``scutil --nc list``.

    Typical lines (macOS 14)::

        * (Connected)    "Nextin" [VPN:com.example.nextin]
        * (Connected)    1234-... PPP --> ...
    """
    connections: list[VpnConnection] = []
    for line in (text or "").splitlines():
        if not _CONNECTED.search(line):
            continue
        quoted = _QUOTED.search(line)
        bundle = _BUNDLE.search(line)
        name = quoted.group(1).strip() if quoted else ""
        if not name:
            tail = _CONNECTED.split(line, maxsplit=1)[-1]
            tail = _BUNDLE.sub("", tail).strip()
            name = tail.split()[0] if tail.split() else ""
        connections.append(
            VpnConnection(name=name, bundle=bundle.group(1).strip() if bundle else "")
        )
    return connections


def parse_lsof_listeners(text: str, port: int) -> list[PortOwner]:
    """Parse ``lsof -Fpc`` field output (``p<pid>`` / ``c<command>`` lines)."""
    owners: list[PortOwner] = []
    pid: int | None = None
    for line in (text or "").splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("c"):
            owners.append(PortOwner(port=port, pid=pid, command=line[1:].strip()))
    return owners


def parse_default_interface(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("interface:"):
            return stripped.split(":", 1)[1].strip()
    return ""


_IFACE_START = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+):\s+flags=")
_INET = re.compile(r"^\s+inet\s+(?P<address>\d+\.\d+\.\d+\.\d+)")


def parse_utun_addresses(text: str) -> list[tuple[str, str]]:
    """``(interface, inet)`` pairs for every ``utun*`` block in ``ifconfig``."""
    found: list[tuple[str, str]] = []
    current = ""
    for line in (text or "").splitlines():
        start = _IFACE_START.match(line)
        if start:
            current = start.group("name")
            continue
        if not current.startswith("utun"):
            continue
        inet = _INET.match(line)
        if inet:
            found.append((current, inet.group("address")))
    return found


def _in(address: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def in_project_block(address: str) -> bool:
    """Whether an address belongs to mihomo's own fake-ip range (198.19/16)."""
    return _in(address, PROJECT_ADDRESS_BLOCK)


# ---- project identity -----------------------------------------------------


def launchd_pid(executor: Executor, *, label: str = LABEL) -> int | None:
    """PID of our LaunchDaemon job, or ``None`` when it is not loaded.

    Reading ``system/<label>`` does not need root; a failure here only means
    "unknown", never "ours".
    """
    try:
        result = executor.run(["launchctl", "print", f"system/{label}"])
    except Exception:  # noqa: BLE001 - identity is best effort, never fatal
        return None
    state = parse_print(
        (result.stdout or "") + (result.stderr or ""),
        label=label,
        returncode=result.returncode,
    )
    return state.pid


def _normalize_path(path: str) -> str:
    """POSIX-normalized path, so identity works on any host OS."""
    return os.path.normpath(str(path).strip()).replace("\\", "/")


def is_project_executable(path: str, binary_path: str = "") -> bool:
    """Whether ``path`` is this project's installed mihomo binary."""
    if not path:
        return False
    normalized = _normalize_path(path)
    if binary_path and normalized == _normalize_path(binary_path):
        return True
    return normalized.endswith(PROJECT_EXECUTABLE)


class _ProjectIdentity:
    """Decide whether a port owner is this project (PID or executable path)."""

    def __init__(
        self,
        executor: Executor,
        *,
        job_pid: int | None,
        binary_path: str,
    ) -> None:
        self.executor = executor
        self.job_pid = job_pid if job_pid is not None else launchd_pid(executor)
        self.binary_path = binary_path
        self._paths: dict[int, str] = {}

    def classify(self, owner: PortOwner) -> str:
        """``"project"``, ``"foreign"`` or ``"unknown"`` for a port owner.

        ``"project"`` needs a *positive* identification: the launchd job PID
        or the fixed executable path.  A PID that cannot be resolved to an
        executable path is ``"unknown"``, never ``"foreign"``.
        """
        if owner.pid is None:
            return "unknown"
        if self.job_pid is not None and int(owner.pid) == int(self.job_pid):
            return "project"
        path = self._executable(owner.pid)
        if not path:
            return "unknown"
        return "project" if is_project_executable(path, self.binary_path) else "foreign"

    def owns(self, owner: PortOwner) -> bool:
        return self.classify(owner) == "project"

    def _executable(self, pid: int) -> str:
        if pid in self._paths:
            return self._paths[pid]
        path = ""
        try:
            result = self.executor.run(["ps", "-o", "comm=", "-p", str(int(pid))])
            if result.ok:
                path = (result.stdout or "").strip()
        except Exception:  # noqa: BLE001 - unknown identity is "not ours"
            path = ""
        self._paths[pid] = path
        return path


# ---- detection ------------------------------------------------------------


def port_owner_identity(
    executor: Executor,
    owner: PortOwner,
    *,
    job_pid: int | None = None,
    binary_path: str = "",
) -> str:
    """``"project"`` / ``"foreign"`` / ``"unknown"`` for a port owner.

    The one entry point for "does this listener belong to us": the launchd
    PID or the fixed executable path (see :class:`_ProjectIdentity`).
    """
    return _ProjectIdentity(
        executor, job_pid=job_pid, binary_path=binary_path
    ).classify(owner)


def project_core_state(executor: Executor, *, job_pid: int | None = None) -> str:
    """``"running"`` / ``"stopped"`` / ``"unknown"`` for this project's core.

    The launchd PID (when the job reports one) is checked first; otherwise
    the fixed executable path is matched with ``pgrep -f``, because an
    orphaned core runs without launchd.  A broken probe is ``"unknown"``:
    it is never folded into ``"stopped"``.
    """
    if job_pid is not None:
        try:
            result = executor.run(["ps", "-o", "comm=", "-p", str(int(job_pid))])
        except Exception:  # noqa: BLE001 - identity is best effort, never fatal
            return "unknown"
        if result.ok and is_project_executable((result.stdout or "").strip()):
            return "running"
    try:
        result = executor.run(["pgrep", "-f", PROJECT_EXECUTABLE])
    except Exception:  # noqa: BLE001
        return "unknown"
    if result.returncode == 0:
        return "running"
    if result.returncode == 1:
        return "stopped"
    return "unknown"


def _lsof_argv(port: int) -> list[str]:
    return ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"]


def _read(executor: Executor, argv: list[str], *, allow: tuple[int, ...] = (0,)) -> str:
    """Run a read-only probe; a non-allowed return code is a detection error."""
    result = executor.run(argv)
    if result.returncode not in allow:
        raise RuntimeError(f"{argv[0]} failed (rc={result.returncode})")
    return result.stdout or ""


def detect(
    executor: Executor,
    *,
    ports: tuple[int, ...] = PROJECT_PORTS,
    job_pid: int | None = None,
    binary_path: str = "",
) -> ConflictReport:
    """Read-only conflict scan; never raises (a failure is a conflict)."""
    report = ConflictReport()
    try:
        vpn_text = _read(executor, ["scutil", "--nc", "list"])
        report.vpn_connected = parse_scutil_nc_list(vpn_text)

        owners: list[PortOwner] = []
        for port in ports:
            # lsof exits 1 when nothing listens: that is "no owner", not an error
            text = _read(executor, _lsof_argv(port), allow=(0, 1))
            owners.extend(parse_lsof_listeners(text, port))
        report.port_owners = owners

        default_interface = parse_default_interface(
            _read(executor, ["route", "-n", "get", "default"])
        )
        utun_addresses = parse_utun_addresses(_read(executor, ["ifconfig"]))
    except Exception as exc:  # noqa: BLE001 - fail-safe is the whole point
        detail = HOST.text(str(exc) or type(exc).__name__)[:200]
        report.ok = False
        report.error = detail
        report.reasons = [
            "冲突检测失败，按“有冲突”处理（不会启动 mihomo）：" + detail
        ]
        return report

    identity = _ProjectIdentity(executor, job_pid=job_pid, binary_path=binary_path)
    ours = [identity.owns(owner) for owner in owners]
    report.project_pids = sorted(
        {int(owner.pid) for owner, mine in zip(owners, ours) if mine and owner.pid is not None}
    )
    mihomo_owns_controller = any(
        owner.port == CONTROLLER_PORT and mine
        for owner, mine in zip(owners, ours)
    )

    foreign_owners = [
        owner for owner, mine in zip(owners, ours) if not mine
    ]

    if default_interface.startswith("utun"):
        default_address = next(
            (address for name, address in utun_addresses if name == default_interface), ""
        )
        default_is_ours = mihomo_owns_controller and (
            not default_address or _in(default_address, PROJECT_ADDRESS_BLOCK)
        )
        if not default_is_ours:
            report.foreign_default_utun = default_interface

    foreign_addresses: list[tuple[str, str]] = []
    for name, address in utun_addresses:
        if _in(address, FOREIGN_ADDRESS_BLOCK):
            foreign_addresses.append((name, address))
        elif _in(address, PROJECT_ADDRESS_BLOCK) and not mihomo_owns_controller:
            foreign_addresses.append((name, address))

    reasons: list[str] = []
    for connection in report.vpn_connected:
        label = connection.name or "未命名 VPN"
        suffix = f" ({connection.bundle})" if connection.bundle else ""
        reasons.append(f"外部 VPN 已连接：{label}{suffix}")
    for owner in foreign_owners:
        reasons.append(
            f"端口 {owner.port} 被非本项目进程占用：{owner.command or 'unknown'}"
            + (f" (pid {owner.pid})" if owner.pid is not None else "")
        )
    if report.foreign_default_utun:
        reasons.append(f"默认路由走外部 utun：{report.foreign_default_utun}")
    for name, address in foreign_addresses:
        reasons.append(f"utun 地址被外部 VPN 占用：{name} {address}")

    report.reasons = [HOST.text(reason) for reason in reasons]
    report.ok = not reasons
    return report


def describe(report: ConflictReport) -> str:
    """One-line operator hint (never contains a secret)."""
    if report.ok:
        return "no external VPN or route conflict detected"
    return (
        "检测到外部 VPN/路由冲突，已拒绝启动 mihomo。先运行 "
        "`mpm-switch mihomo` 切换到本项目，或在系统设置里断开 Nextin 后重试。原因："
        + "；".join(report.reasons)
    )


__all__ = [
    "CONTROLLER_PORT",
    "ConflictReport",
    "FOREIGN_ADDRESS_BLOCK",
    "PROJECT_ADDRESS_BLOCK",
    "PROJECT_EXECUTABLE",
    "PROJECT_PORTS",
    "PortOwner",
    "UTUN_ADDRESS_SUPERNET",
    "VpnConnection",
    "describe",
    "detect",
    "in_project_block",
    "is_project_executable",
    "launchd_pid",
    "parse_default_interface",
    "parse_lsof_listeners",
    "parse_scutil_nc_list",
    "parse_utun_addresses",
]
