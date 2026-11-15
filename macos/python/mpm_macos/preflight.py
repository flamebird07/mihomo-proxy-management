"""Darwin preflight: arm64 + root + required command capabilities.

macOS has no Linux TUN device node: mihomo asks the system utun API for an
``utunN`` interface, so this module never probes a device path.  TUN success is
decided later from process/controller state plus an injected interface/route
probe (see ``lifecycle.observe_tun``).

Every input is a :class:`PlatformFacts`, so the whole decision is testable on
Windows and Linux.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mpm_common.errors import Unsupported
from mpm_common.platforms import MACOS_MIN_SUPPORTED_MAJOR, PlatformFacts, detect

SUPPORTED_SYSTEM = "darwin"
SUPPORTED_ARCH = "arm64"

# Commands the lifecycle cannot work without, discovered on the PATH.
REQUIRED_COMMANDS = ("launchctl", "networksetup")
# Commands used when available; a missing one is a warning, not an error.
OPTIONAL_COMMANDS = ("caffeinate", "ifconfig", "route")

# mihomo is *not* a PATH command: a LaunchDaemon runs with
# /usr/bin:/bin:/usr/sbin:/sbin and the installed binary lives under libexec.
# The shared context builder (mpm_macos.context) registers the absolute
# installed path - and only that path - into ``facts.commands``; preflight
# only verifies that a usable path was registered.  MPM_MIHOMO_BINARY and
# PATH are install-source candidates only, never runtime capabilities.
# ``install`` passes ``require_mihomo=False``: it supplies the binary itself
# (override or PATH source), so the platform check must not demand the
# runtime binary before the copy; every other capability check stays strict.
MIHOMO_COMMAND = "mihomo"
MIHOMO_MISSING_REASON = (
    "mihomo binary not available: install it first "
    "(MPM_MIHOMO_BINARY or PATH for the installer); the runtime uses the "
    "installed absolute path only"
)


@dataclass
class PlatformCheck:
    supported: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    system: str = "unknown"
    arch: str = "unknown"
    is_root: bool = False
    mode: str = "mixed"  # tun | mixed
    commands: dict[str, bool] = field(default_factory=dict)
    release: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "system": self.system,
            "arch": self.arch,
            "root": self.is_root,
            "mode": self.mode,
            "tun": self.mode == "tun",
            "commands": dict(self.commands),
            "release": self.release,
        }


def collect_facts() -> PlatformFacts:
    """Read the real host (never called by unit tests)."""
    return detect(commands=REQUIRED_COMMANDS + OPTIONAL_COMMANDS)


def evaluate(facts: PlatformFacts, *, require_mihomo: bool = True) -> PlatformCheck:
    """Pure decision function - no filesystem or process access.

    ``require_mihomo=False`` is for ``install`` only: the installer copies the
    binary itself, so the platform check runs before it exists.  The mihomo
    capability is still reported in ``commands``; all other checks are
    unchanged.
    """
    reasons: list[str] = []
    warnings: list[str] = []

    if facts.system != SUPPORTED_SYSTEM:
        reasons.append(f"macOS (Darwin) is required; host reports '{facts.system}'")
    if facts.machine != SUPPORTED_ARCH:
        reasons.append(
            f"Apple Silicon (arm64) is required; host reports '{facts.machine or 'unknown'}'"
        )
    if not facts.is_root:
        reasons.append("root privileges are required (the core runs as a LaunchDaemon)")

    commands = {name: facts.has_command(name) for name in REQUIRED_COMMANDS + OPTIONAL_COMMANDS}
    commands[MIHOMO_COMMAND] = bool(facts.command(MIHOMO_COMMAND))
    for name in REQUIRED_COMMANDS:
        if not commands[name]:
            reasons.append(f"required command not found: {name}")
    if require_mihomo and not commands[MIHOMO_COMMAND]:
        reasons.append(MIHOMO_MISSING_REASON)
    for name in OPTIONAL_COMMANDS:
        if not commands[name]:
            warnings.append(f"optional command not found: {name}")

    if facts.system == SUPPORTED_SYSTEM:
        major = facts.release_major()
        if major == 0:
            warnings.append(
                "macOS version unknown; capability checks (not the version string) decide support"
            )
        elif major < MACOS_MIN_SUPPORTED_MAJOR:
            warnings.append(
                f"untested macOS release '{facts.release}'; capability checks still apply"
            )

    supported = not reasons
    return PlatformCheck(
        supported=supported,
        reasons=reasons,
        warnings=warnings,
        system=facts.system,
        arch=facts.machine,
        is_root=facts.is_root,
        mode="tun" if supported else "mixed",
        commands=commands,
        release=facts.release,
    )


def preflight(
    facts: PlatformFacts | None = None,
    *,
    strict: bool = True,
    require_mihomo: bool = True,
) -> PlatformCheck:
    result = evaluate(
        facts if facts is not None else collect_facts(), require_mihomo=require_mihomo
    )
    if strict and not result.supported:
        raise Unsupported("host is not supported: " + "; ".join(result.reasons))
    return result


__all__ = [
    "MIHOMO_COMMAND",
    "MIHOMO_MISSING_REASON",
    "OPTIONAL_COMMANDS",
    "PlatformCheck",
    "REQUIRED_COMMANDS",
    "SUPPORTED_ARCH",
    "SUPPORTED_SYSTEM",
    "collect_facts",
    "evaluate",
    "preflight",
]
