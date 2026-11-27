"""Parse the Bitable address a user hands to the deploy tool (pure, stdlib only).

A Bitable is shared either by its own URL (``https://<tenant>.feishu.cn/base/
<app_token>``) or, inside a wiki, by a node URL (``.../wiki/<node_token>``)
whose object token still has to be resolved through the API.  A bare app_token
is accepted too.  Nothing here talks to the network.  The token is masked in
logs: it grants nothing on its own, but it names the user's table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

#: Only ``feishu.cn`` (and its subdomains) is supported: the pipeline talks to
#: ``open.feishu.cn`` (``node_provider.feishu.BASE``), not to a Lark endpoint.
FEISHU_HOST_ROOTS = ("feishu.cn",)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_TABLE_RE = re.compile(r"^[A-Za-z0-9]{3,32}$")


@dataclass(frozen=True)
class ParsedBitableUrl:
    """``kind`` is ``base`` (token is the app_token), ``wiki`` (token is the
    wiki node token) or ``token`` (a bare app_token)."""

    kind: str
    token: str
    host: str = ""
    table_id: str = ""
    scheme: str = "https"

    def masked(self) -> str:
        """The address with its token masked - the only form safe to log."""
        if self.kind == "token":
            return mask_token(self.token)
        section = "wiki" if self.kind == "wiki" else "base"
        return f"{self.scheme}://{self.host}/{section}/{mask_token(self.token)}"


class FeishuUrlError(ValueError):
    """The address is unusable.

    ``str()`` names the ``kind`` (and the host) only - never the token or the
    raw input, which would otherwise end up in a terminal or a log.
    """

    def __init__(self, kind: str, host: str = "") -> None:
        message = f"invalid Feishu Bitable address: {kind}"
        if host:
            message += f" (host {host})"
        super().__init__(message)
        self.kind = kind
        self.host = host


def mask_token(token: str) -> str:
    """First 4 and last 3 characters around an ellipsis; short values vanish."""
    if len(token) <= 9:
        return "<redacted>"
    return f"{token[:4]}…{token[-3:]}"


def _is_feishu_host(host: str) -> bool:
    return any(host == root or host.endswith("." + root) for root in FEISHU_HOST_ROOTS)


def parse_bitable_url(raw: str) -> ParsedBitableUrl:
    """Validate a pasted Bitable address; raises :class:`FeishuUrlError`."""
    if raw is None or not raw.strip():
        raise FeishuUrlError("empty")
    text = raw.strip()
    if _TOKEN_RE.fullmatch(text):
        return ParsedBitableUrl(kind="token", token=text)
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError:  # malformed netloc (brackets, port ...)
        raise FeishuUrlError("not_a_url") from None
    if not scheme or not host:
        raise FeishuUrlError("not_a_url")
    if scheme not in ("http", "https"):
        raise FeishuUrlError("unsupported_scheme", host)
    if not _is_feishu_host(host):
        raise FeishuUrlError("not_feishu_host", host)
    if has_userinfo:
        # An address carrying an account would put that account into logs.
        raise FeishuUrlError("has_userinfo", host)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0] not in ("base", "wiki"):
        raise FeishuUrlError("unknown_path", host)
    token = segments[1]
    if not _TOKEN_RE.fullmatch(token):
        raise FeishuUrlError("bad_token")
    table_id = ""
    values = parse_qs(parts.query).get("table") or []
    if values and _TABLE_RE.fullmatch(values[0]):
        table_id = values[0]
    return ParsedBitableUrl(kind=segments[0], token=token, host=host,
                            table_id=table_id, scheme=scheme)
