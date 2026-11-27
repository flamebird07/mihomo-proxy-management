"""Deploy credentials: where they come from and how they are masked (stdlib only).

The deploy tool needs exactly three values from the user - the Bitable address
and an app id/secret pair with read/write access to it.  Each value is looked
up in the environment first, then in a JSON object piped on stdin, then through
an interactive prompt (hidden for the secret).  Values never appear in argv,
and :class:`Redactor` keeps them out of every line that gets logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional, Sequence, TextIO

BASE_URL_KEY = "MPM_FEISHU_BASE_URL"
#: An alias of ``BASE_URL_KEY`` for callers that only have the app_token.
TOKEN_KEY = "MPM_FEISHU_BITABLE_APP_TOKEN"
APP_ID_KEY = "MPM_FEISHU_APP_ID"
APP_SECRET_KEY = "MPM_FEISHU_APP_SECRET"
STDIN_FIELDS = ("feishu_url", "app_id", "app_secret")
#: Key reported for each stdin field that stayed empty, in field order.
_FIELD_KEYS = (BASE_URL_KEY, APP_ID_KEY, APP_SECRET_KEY)
MASK = "<redacted>"


@dataclass(frozen=True)
class Credentials:
    """What the user handed over.  ``feishu_url`` is the raw address (a URL or
    a bare token) exactly as given; parsing it is ``feishu_url``'s job."""

    feishu_url: str
    app_id: str
    app_secret: str = field(repr=False)


class CredentialsError(Exception):
    """A credential problem that is safe to print: kinds and key names only."""

    def __init__(self, kind: str, missing: Sequence[str] = ()) -> None:
        if kind == "missing":
            message = ("missing credentials: " + ", ".join(missing)
                       + " (set env vars, pipe JSON on stdin, or run interactively)")
        else:
            message = f"credentials on stdin are unusable: {kind}"
        super().__init__(message)
        self.kind = kind
        self.missing = tuple(missing)


def _pick(*candidates: Optional[str]) -> str:
    for candidate in candidates:
        value = (candidate or "").strip()
        if value:
            return value
    return ""


def read_stdin_values(stream: Optional[TextIO]) -> dict:
    """The JSON object piped on stdin, reduced to a flat ``{field: value}``.

    Unknown keys are ignored; blank values are dropped.  Anything that is not
    one JSON object with string fields is ``stdin_invalid`` - the message never
    echoes the input, which may hold a mistyped secret.
    """
    if stream is None:
        return {}
    try:
        text = stream.read()
    except (ValueError, OSError):  # a closed or undecodable stream
        raise CredentialsError("stdin_invalid") from None
    if not text or not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        raise CredentialsError("stdin_invalid") from None
    if not isinstance(payload, dict):
        raise CredentialsError("stdin_invalid")
    values: dict = {}
    for name in STDIN_FIELDS:
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise CredentialsError("stdin_invalid")
        value = value.strip()
        if value:
            values[name] = value
    return values


def resolve_credentials(environ: Optional[Mapping[str, str]] = None,
                        stdin_values: Optional[Mapping[str, str]] = None,
                        prompt: Optional[Callable[[str], str]] = None) -> Credentials:
    """Collect the three values: environment > stdin > prompt, per field.

    ``prompt(field)`` is called only for a field that is still empty and at
    most once per field; an empty answer counts as missing.  Whatever it raises
    (KeyboardInterrupt, EOFError ...) propagates to the caller.
    """
    environ = environ or {}
    stdin_values = stdin_values or {}

    def collect(field: str, *keys: str) -> str:
        value = _pick(*(environ.get(key) for key in keys), stdin_values.get(field))
        if not value and prompt is not None:
            value = _pick(prompt(field))
        return value

    values = {
        "feishu_url": collect("feishu_url", BASE_URL_KEY, TOKEN_KEY),
        "app_id": collect("app_id", APP_ID_KEY),
        "app_secret": collect("app_secret", APP_SECRET_KEY),
    }
    missing = tuple(key for field, key in zip(STDIN_FIELDS, _FIELD_KEYS)
                    if not values[field])
    if missing:
        raise CredentialsError("missing", missing)
    return Credentials(feishu_url=values["feishu_url"], app_id=values["app_id"],
                       app_secret=values["app_secret"])


def mask(value: str, keep: int = 4) -> str:
    """Enough of ``value`` to recognise it in a log line, never all of it."""
    if not value:
        return ""
    if len(value) <= keep + 5:
        return MASK
    return f"{value[:keep]}…{value[-3:]}"


class Redactor:
    """Scrubs registered secrets from a line before it is logged."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: Optional[str]) -> None:
        # A very short string would be replaced everywhere and say nothing.
        if not secret or len(secret) < 4 or secret in self._secrets:
            return
        self._secrets.append(secret)

    def text(self, line: str) -> str:
        """``line`` with every registered secret replaced; longer ones first so
        that overlapping values (a URL and the token inside it) leave no stub."""
        text = str(line)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, MASK)
        return text
