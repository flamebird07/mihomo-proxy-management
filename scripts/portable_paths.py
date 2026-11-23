"""Windows deployment paths that stay safe on POSIX hosts.

The Windows bundle hardcodes ``C:\\mihomo\\...``.  On macOS/Linux a drive-letter
path has no special meaning, so it is just a relative filename: a default used
for writing created a literal ``C:\\mihomo\\logs\\...`` file in the current
directory (2026-11-22).  These helpers keep the Windows layout on Windows and
root the same tail under ``$MPM_HOME`` (default ``~/.local/state/mihomo``)
elsewhere.  ``C:\\Users\\<name>\\...`` maps under the current home directory.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

HOME_ENV = "MPM_HOME"
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def portable_path(raw: str) -> str:
    """Map a Windows absolute deployment path onto the current POSIX host."""
    if os.name == "nt" or not _WINDOWS_ABSOLUTE.match(raw):
        return raw
    tail = list(Path(raw.replace("\\", "/")).parts[1:])
    if len(tail) >= 3 and tail[0].lower() == "users":
        return str(Path.home().joinpath(*tail[2:]))
    base = Path(os.environ.get(HOME_ENV) or (Path.home() / ".local" / "state" / "mihomo"))
    if tail and tail[0].lower() == "mihomo":
        tail = tail[1:]
    return str(base.joinpath(*tail)) if tail else str(base)


def portable_core(raw: str) -> str:
    """Windows ``mihomo.exe`` default -> an executable that exists on POSIX."""
    if os.name == "nt" or not _WINDOWS_ABSOLUTE.match(raw):
        return raw
    env = os.environ.get("NODE_OPS_MIHOMO_CORE", "").strip()
    if env:
        return env
    for candidate in (shutil.which("mihomo"),
                      "/usr/local/libexec/mihomo-proxy-management/current/mihomo"):
        if candidate and os.path.isfile(candidate):
            return candidate
    return "mihomo"
