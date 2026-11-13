"""Platform, architecture and privilege facts - every value injectable.

Production code never scatters ``sys.platform`` / ``platform.system()`` /
``os.geteuid()`` through business modules: it takes a :class:`PlatformFacts`
instead.  Tests build one directly, so Darwin/Linux/Windows branches are
covered on any host without touching the real machine.

Windows is deliberately *not* treated as root: ``os.geteuid`` does not exist
there, and the safe default is "no root privileges" rather than ``0``.
"""

from __future__ import annotations

import os
import platform as platform_mod
import shutil
from dataclasses import dataclass, field
from typing import Callable

SUPPORTED_SYSTEMS = ("darwin", "linux", "windows")

# Canonical architecture names.  Everything is normalised to these four.
ARCH_ALIASES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "arm64e": "arm64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
}

MACOS_MIN_SUPPORTED_MAJOR = 12


def normalize_arch(machine: str) -> str:
    """``arm64``/``aarch64`` -> ``arm64``; unknown values are returned lowercased."""
    value = (machine or "").strip().lower()
    return ARCH_ALIASES.get(value, value or "unknown")


def normalize_system(system: str) -> str:
    value = (system or "").strip().lower()
    return value or "unknown"


def _real_euid() -> int:
    """-1 when the platform has no ``geteuid`` (Windows) - never 0."""
    return getattr(os, "geteuid", lambda: -1)()


@dataclass(frozen=True)
class PlatformFacts:
    """Everything preflight and the lifecycle need to know about the host."""

    system: str = "unknown"  # darwin | linux | windows
    machine: str = "unknown"  # normalised: arm64 | x86_64 | x86 | unknown
    raw_machine: str = ""
    is_root: bool = False
    commands: dict[str, str | None] = field(default_factory=dict)
    release: str = ""
    clock: Callable[[], float] | None = None

    @property
    def is_darwin(self) -> bool:
        return self.system == "darwin"

    @property
    def is_windows(self) -> bool:
        return self.system == "windows"

    def command(self, name: str) -> str | None:
        """Absolute path of ``name`` when it was found, else ``None``."""
        return self.commands.get(name)

    def has_command(self, name: str) -> bool:
        return bool(self.commands.get(name))

    def release_major(self) -> int:
        """Best-effort major version of the OS release (0 when unknown)."""
        head = (self.release or "").split(".", 1)[0]
        try:
            return int(head)
        except (TypeError, ValueError):
            return 0

    def to_dict(self) -> dict[str, object]:
        return {
            "system": self.system,
            "arch": self.machine,
            "raw_machine": self.raw_machine,
            "root": self.is_root,
            "release": self.release,
            "commands": {name: bool(path) for name, path in sorted(self.commands.items())},
        }


def detect(
    *,
    system: str | None = None,
    machine: str | None = None,
    is_root: bool | None = None,
    which: Callable[[str], str | None] | None = None,
    release: str | None = None,
    commands: tuple[str, ...] = ("launchctl", "networksetup", "mihomo", "caffeinate", "ifconfig", "route"),
) -> PlatformFacts:
    """Read host facts once; every input can be overridden (tests do)."""
    raw_machine = machine if machine is not None else platform_mod.machine()
    lookup = which or shutil.which
    resolved = {name: lookup(name) for name in commands}
    if release is None:
        release = platform_mod.release() if (system or platform_mod.system()).lower() == "darwin" else ""
    return PlatformFacts(
        system=normalize_system(system if system is not None else platform_mod.system()),
        machine=normalize_arch(raw_machine),
        raw_machine=raw_machine,
        is_root=(is_root if is_root is not None else _real_euid() == 0),
        commands=resolved,
        release=release or "",
    )


def current_user_is_root() -> bool:
    """Safe root probe for code that has no injected facts (Windows: False)."""
    return _real_euid() == 0


__all__ = [
    "ARCH_ALIASES",
    "MACOS_MIN_SUPPORTED_MAJOR",
    "PlatformFacts",
    "SUPPORTED_SYSTEMS",
    "current_user_is_root",
    "detect",
    "normalize_arch",
    "normalize_system",
]
