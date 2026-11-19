"""KEY=VALUE env-file reader.

Contract (matches the existing project convention):
  * UTF-8, optional BOM;
  * blank lines and lines starting with '#' are ignored;
  * paired single/double quotes around the value are stripped;
  * no variable expansion, no escape processing;
  * a duplicate key or an unclosed quote is an error (fail closed).

Secrets are returned to the caller but never logged by this module.
"""

from __future__ import annotations

from typing import Dict


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
