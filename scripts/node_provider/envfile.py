"""KEY=VALUE env-file reader (self-contained copy/rewrite).

Contract matches the project convention:
  * UTF-8, optional BOM;
  * blank lines and lines starting with '#' are ignored;
  * paired single/double quotes around the value are stripped;
  * no variable expansion, no escape processing;
  * duplicate key or unclosed quote is an error (fail closed).

Also resolves the Bitable app_token, which has no built-in default.

Secret values are returned to the caller but never logged here.
"""

from __future__ import annotations

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
