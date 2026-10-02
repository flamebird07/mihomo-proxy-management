"""Single controller client (section 9) - every call is authenticated.

Rules enforced here:

* one transport function, so no call site can forget the ``Authorization``
  header
* empty secret -> :class:`FailClosed` (never a bare/unauthenticated call)
* non-2xx -> :class:`ControllerError`, which the CLI turns into a non-zero
  exit; there is deliberately no unauthenticated retry
* request/response/error text always goes through the sanitizer
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote
from dataclasses import dataclass, field
from http.client import HTTPResponse
from typing import Any, Callable

from .errors import FailClosed
from .sanitize import HOST, describe_url

CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORT = 9090
BASE_URL = f"http://{CONTROLLER_HOST}:{CONTROLLER_PORT}"

USER_AGENT = "mihomo-proxy-management/3.0"


class ControllerError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


Transport = Callable[[str, str, dict[str, str], Any, float], tuple[int, bytes]]


@dataclass
class Controller:
    """Authenticated mihomo REST client.

    ``transport`` is injectable so tests can assert on headers without opening
    a socket.  ``OpenerDirector``-style injection is intentionally narrow: the
    transport receives method, url, headers, body and timeout.
    """

    secret: str
    base_url: str = BASE_URL
    timeout: float = 5.0
    transport: Transport | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.secret = (self.secret or "").strip()
        HOST.register(self.secret)
        if not self.secret:
            raise FailClosed(
                "controller secret is empty: refusing to talk to the API unauthenticated"
            )

    # ---- transport -----------------------------------------------------
    def _urlopen_transport(
        self, method: str, url: str, headers: dict[str, str], body: Any, timeout: float
    ) -> tuple[int, bytes]:
        data = None
        if body is not None:
            data = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                assert isinstance(response, HTTPResponse)
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise ControllerError(f"controller unreachable: {HOST.text(str(exc.reason))}") from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        if not path.startswith("/"):
            raise FailClosed("controller path must start with /")
        url = self.base_url.rstrip("/") + path
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        # record the endpoint without any query string (delay() carries a probe URL)
        self.calls.append({"method": method, "path": path.split("?", 1)[0], "headers": dict(headers)})
        transport = self.transport or self._urlopen_transport
        status, raw = transport(method, url, headers, body, self.timeout if timeout is None else timeout)
        if status < 200 or status >= 300:
            # *Every* non-2xx is an error.  A 3xx is not a successful document:
            # if it were returned as one, whatever the server echoed in the body
            # - including the health-check URL that delay() puts in the query
            # string, raw or percent-encoded - would be handed to the caller.
            # The sanitizer cannot recognise a percent-encoded value, so the
            # body is never used and only the endpoint appears in the message.
            endpoint = path.split("?", 1)[0]
            raise ControllerError(
                f"controller returned HTTP {status} for {method} {HOST.text(endpoint)}",
                status=status,
            )
        payload: Any = None
        if raw:
            text = raw.decode("utf-8", errors="replace")
            try:
                payload = json.loads(text)
            except ValueError:
                payload = HOST.text(text)[:400]
        return status, payload

    # ---- endpoints -----------------------------------------------------
    def version(self) -> dict[str, Any]:
        _, payload = self.request("GET", "/version")
        return payload if isinstance(payload, dict) else {}

    def proxies(self) -> dict[str, Any]:
        _, payload = self.request("GET", "/proxies")
        return payload if isinstance(payload, dict) else {}

    def providers(self) -> dict[str, Any]:
        _, payload = self.request("GET", "/providers/proxies")
        return payload if isinstance(payload, dict) else {}

    def provider(self, name: str) -> dict[str, Any]:
        _, payload = self.request("GET", f"/providers/proxies/{_quote(name)}")
        return payload if isinstance(payload, dict) else {}

    def healthcheck(self, name: str) -> None:
        self.request("GET", f"/providers/proxies/{_quote(name)}/healthcheck")

    def refresh_provider(self, name: str, *, timeout: float = 60.0) -> None:
        """Blocking provider refresh; failures propagate (never silent)."""
        self.request("PUT", f"/providers/proxies/{_quote(name)}", body={}, timeout=timeout)

    def delay(self, group: str, *, url: str, timeout: float = 8.0) -> int | None:
        _, payload = self.request(
            "GET",
            f"/proxies/{_quote(group)}/delay?timeout={int(timeout * 1000)}&url={_quote(url)}",
            timeout=timeout + 2,
        )
        if isinstance(payload, dict) and "delay" in payload:
            try:
                return int(payload["delay"])
            except (TypeError, ValueError):
                return None
        return None


def _quote(value: str) -> str:
    return quote(value, safe="")


def base_url_of(host: str = CONTROLLER_HOST, port: int = CONTROLLER_PORT) -> str:
    return describe_url(f"http://{host}:{port}")


__all__ = [
    "BASE_URL",
    "CONTROLLER_HOST",
    "CONTROLLER_PORT",
    "Controller",
    "ControllerError",
    "base_url_of",
]
