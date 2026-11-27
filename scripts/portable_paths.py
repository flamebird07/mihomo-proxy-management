"""Windows deployment paths that stay safe on POSIX hosts.

The Windows bundle hardcodes ``C:\\mihomo\\...``.  On macOS/Linux a drive-letter
path has no special meaning, so it is just a relative filename: a default used
for writing created a literal ``C:\\mihomo\\logs\\...`` file in the current
directory (2026-11-22).  These helpers keep the Windows layout on Windows and
root the same tail under ``$MPM_HOME`` (default ``~/.local/state/mihomo``)
elsewhere.  ``C:\\Users\\<name>\\...`` maps under the current home directory.

Credential-file resolution (:func:`default_env_file`), used when a script is
invoked without an explicit ``--env-file``:

1. On Windows, ``<install dir>\\subscription.env`` when that file already holds
   a ``FEISHU_APP_ID`` key (the bootstrap writes it there).  The install dir is
   ``MIHOMO_DIR`` when that names a directory, otherwise ``C:\\mihomo``.
2. Otherwise ``~/.hermes/.env`` (``%USERPROFILE%`` on Windows, ``$HOME`` on
   POSIX) — the pre-bootstrap location that must keep working.

An explicit ``--env-file`` always wins; the macOS daemons pass one and are not
affected.  The same order is mirrored by ``node_provider.envfile`` and
``node_ops.envfile`` (the two packages never import each other).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Mapping

HOME_ENV = "MPM_HOME"
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")

#: Fallback Windows install directory when MIHOMO_DIR is unset/invalid.
WINDOWS_INSTALL_DIR = r"C:\mihomo"
INSTALL_DIR_ENV = "MIHOMO_DIR"
LEGACY_ENV_RELATIVE = (".hermes", ".env")
FEISHU_MARKER_KEY = "FEISHU_APP_ID"


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


def windows_install_dir(environ: Mapping[str, str] | None = None) -> str:
    """The Windows install directory: ``MIHOMO_DIR`` if it names a dir, else ``C:\\mihomo``."""
    env = os.environ if environ is None else environ
    raw = (env.get(INSTALL_DIR_ENV) or "").strip()
    if raw and os.path.isdir(raw):
        return raw
    return WINDOWS_INSTALL_DIR


def legacy_env_file() -> str:
    """The pre-bootstrap credentials file: ``~/.hermes/.env`` for the current user."""
    return os.path.join(os.path.expanduser("~"), *LEGACY_ENV_RELATIVE)


def has_feishu_keys(path: str) -> bool:
    """True when ``path`` has a non-comment ``FEISHU_APP_ID=`` line.

    Never raises: a missing/unreadable file or a decode failure is just False.
    """
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                key, sep, _ = stripped.partition("=")
                if sep and key.strip() == FEISHU_MARKER_KEY:
                    return True
    except (OSError, UnicodeError):
        return False
    return False


def default_env_file(*, os_name: str | None = None, install_dir: str | None = None,
                     legacy: str | None = None) -> str:
    """Resolve the credentials env file when no ``--env-file`` was given.

    See the module docstring for the order.  ``os_name``/``install_dir``/
    ``legacy`` are injectable so POSIX tests can exercise the Windows branch.
    """
    if os_name is None:
        os_name = os.name
    if install_dir is None:
        install_dir = windows_install_dir()
    if legacy is None:
        legacy = legacy_env_file()
    if os_name == "nt" and install_dir:
        candidate = os.path.join(install_dir, "subscription.env")
        if has_feishu_keys(candidate):
            return candidate
    return legacy


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
