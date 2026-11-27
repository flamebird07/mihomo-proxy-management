"""KEY=VALUE env-file reader.

Contract (matches the existing project convention):
  * UTF-8, optional BOM;
  * blank lines and lines starting with '#' are ignored;
  * paired single/double quotes around the value are stripped;
  * no variable expansion, no escape processing;
  * a duplicate key or an unclosed quote is an error (fail closed).

Also resolves the Bitable app_token, which has no built-in default.

Secrets are returned to the caller but never logged by this module.
"""

from __future__ import annotations

import os
from typing import Dict, Optional


class EnvFileError(Exception):
    """Raised when an env file is unreadable or malformed."""


def read_env_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            lines = handle.readlines()
    except (OSError, UnicodeError) as exc:
        raise EnvFileError(f"cannot read env file ({type(exc).__name__})") from None

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise EnvFileError("invalid env line (missing KEY=VALUE)")
        if key in values:
            raise EnvFileError(f"duplicate env key: {key}")
        value = value.strip()
        if value.startswith(("\"", "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise EnvFileError(f"unclosed quote for env key: {key}")
            value = value[1:-1]
        values[key] = value
    return values


def require(values: Dict[str, str], key: str) -> str:
    value = (values.get(key) or "").strip()
    if not value:
        raise EnvFileError(f"missing required env key: {key}")
    return value


WINDOWS_INSTALL_DIR = r"C:\mihomo"
INSTALL_DIR_ENV = "MIHOMO_DIR"
FEISHU_MARKER_KEY = "FEISHU_APP_ID"


def _windows_install_dir(environ=None) -> str:
    env = os.environ if environ is None else environ
    raw = (env.get(INSTALL_DIR_ENV) or "").strip()
    if raw and os.path.isdir(raw):
        return raw
    return WINDOWS_INSTALL_DIR


def _legacy_env_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".hermes", ".env")


def _has_feishu_keys(path: str) -> bool:
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


def default_env_file(*, os_name=None, install_dir=None, legacy=None) -> str:
    """Resolve the credentials env file when no ``--env-file`` was given.

    Mirror of :func:`portable_paths.default_env_file` (the packages never import
    each other): on Windows prefer a bootstrapped
    ``<install dir>/subscription.env`` carrying ``FEISHU_APP_ID``; otherwise
    fall back to ``~/.hermes/.env``.
    """
    if os_name is None:
        os_name = os.name
    if install_dir is None:
        install_dir = _windows_install_dir()
    if legacy is None:
        legacy = _legacy_env_file()
    if os_name == "nt" and install_dir:
        candidate = os.path.join(install_dir, "subscription.env")
        if _has_feishu_keys(candidate):
            return candidate
    return legacy


#: Process-environment key holding the Bitable app_token.  The table address
#: belongs to the deploying machine, so there is deliberately no default here.
APP_TOKEN_ENV_KEY = "FEISHU_BITABLE_APP_TOKEN"


def resolve_app_token(cli_token: Optional[str], env_file_values: Dict[str, str],
                      environ: Optional[Dict[str, str]]) -> str:
    """``--app-token`` > process env > env file; "" when absent everywhere.

    The returned value is a credential: never log or print it.
    """
    environ = environ or {}
    for candidate in (cli_token, environ.get(APP_TOKEN_ENV_KEY),
                      (env_file_values or {}).get(APP_TOKEN_ENV_KEY)):
        value = (candidate or "").strip()
        if value:
            return value
    return ""
