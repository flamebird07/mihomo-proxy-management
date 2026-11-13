"""Controller readiness, provider health and node liveness.

Reuses the *semantics* of ``linux/python/mpm/controller.py`` without importing
it (the macOS package is shipped on its own):

* one transport function, so no call site can forget the ``Authorization``
  header;
* empty secret -> :class:`FailClosed` (never an unauthenticated call);
* non-2xx -> :class:`ControllerError`; the response body is never echoed,
  because ``delay()`` puts a probe URL in the query string;
* every message that leaves this module is sanitised.

The result model matches the Linux lifecycle: ``OK / DEGRADED / FAILED /
INACTIVE``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

from .errors import FailClosed, NotReady
from .sanitize import HOST, describe_url

CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORT = 9090
BASE_URL = f"http://{CONTROLLER_HOST}:{CONTROLLER_PORT}"

USER_AGENT = "mihomo-proxy-management/3.0"

HEALTH_OK = "OK"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_FAILED = "FAILED"
HEALTH_INACTIVE = "INACTIVE"


class ControllerError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# (method, url, headers, body, timeout) -> (status, raw bytes)
Transport = Callable[[str, str, dict[str, str], Any, float], tuple[int, bytes]]


@dataclass
class ControllerClient:
    """Authenticated mihomo REST client with an injectable transport."""

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

    def _urlopen_transport(
        self, method: str, url: str, headers: dict[str, str], body: Any, timeout: float
    ) -> tuple[int, bytes]:
        data = None
        if body is not None:
            data = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise ControllerError(f"controller unreachable: {HOST.text(str(exc.reason))}") from exc
        except OSError as exc:
            raise ControllerError(f"controller unreachable: {HOST.exception(exc)}") from exc

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
        _, payload = self.request("GET", f"/providers/proxies/{quote(name, safe='')}")
        return payload if isinstance(payload, dict) else {}

    def healthcheck(self, name: str) -> None:
        self.request("GET", f"/providers/proxies/{quote(name, safe='')}/healthcheck")

    def delay(self, name: str, *, url: str, timeout: float = 8.0) -> int | None:
        _, payload = self.request(
            "GET",
            f"/proxies/{quote(name, safe='')}/delay?timeout={int(timeout * 1000)}&url={quote(url, safe='')}",
            timeout=timeout + 2,
        )
        if isinstance(payload, dict) and "delay" in payload:
            try:
                return int(payload["delay"])
            except (TypeError, ValueError):
                return None
        return None


# ---- health ---------------------------------------------------------------


@dataclass
class HealthReport:
    state: str
    reasons: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == HEALTH_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reasons": [HOST.text(reason) for reason in self.reasons],
            "data": _safe(self.data),
        }


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return HOST.text(value)
    return value


def readiness(
    client: ControllerClient,
    *,
    attempts: int = 20,
    interval: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Authenticated ``/version`` probe; an unauthenticated probe is never tried."""
    last_error = ""
    for _ in range(max(1, attempts)):
        try:
            payload = client.version()
            version = str(payload.get("version") or "")
            if version:
                return version
            last_error = "empty /version response"
        except (ControllerError, FailClosed) as exc:
            last_error = HOST.text(str(exc))[:120]
        sleeper(interval)
    raise NotReady(f"authenticated readiness check failed ({last_error})")


def _node_alive_flags(client: ControllerClient) -> dict[str, bool]:
    """Per-node liveness as ``/proxies`` reports it (missing -> absent)."""
    flags: dict[str, bool] = {}
    try:
        payload = client.proxies()
    except (ControllerError, FailClosed):
        return flags
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        return flags
    for name, info in proxies.items():
        if isinstance(info, dict) and "alive" in info:
            flags[str(name)] = bool(info["alive"])
    return flags


def _provider_nodes(info: dict[str, Any]) -> list[tuple[str, bool | None]]:
    raw = info.get("all") or info.get("vehicle") or info.get("proxies") or []
    nodes: list[tuple[str, bool | None]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            name = str(item.get("name") or "")
            alive = bool(item["alive"]) if "alive" in item else None
        else:
            name = str(item)
            alive = None
        if name:
            nodes.append((name, alive))
    return nodes


def provider_summary(
    client: ControllerClient, entries: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Node count *and* live-node count per provider.

    ``alive_count`` is ``None`` when neither the provider entry nor
    ``/proxies`` carries liveness: that is treated as unhealthy (fail closed),
    never as "assume fine".
    """
    if entries is None:
        payload = client.providers()
        entries = payload.get("providers") if isinstance(payload, dict) else {}
    entries = entries if isinstance(entries, dict) else {}
    flags = _node_alive_flags(client)
    summary: dict[str, dict[str, Any]] = {}
    for name, info in entries.items():
        if not isinstance(info, dict):
            continue
        nodes = _provider_nodes(info)
        known = [flags[node] if node in flags else alive for node, alive in nodes]
        known = [flag for flag in known if flag is not None]
        if not known and isinstance(info.get("alive"), list):
            alive_names = {str(item) for item in info["alive"]}
            known = [node in alive_names for node, _alive in nodes]
        alive_count = sum(1 for flag in known if flag) if known else None
        summary[str(name)] = {
            "node_count": len(nodes),
            "alive_count": alive_count,
            "updated_at": str(info.get("updatedAt") or ""),
        }
    return summary


def evaluate(
    client: ControllerClient | None,
    *,
    provider: str = "subscription",
    mode: str = "tun",
    running: bool = True,
    degraded_reasons: list[str] | None = None,
    healthcheck: bool = True,
) -> HealthReport:
    """One health verdict for the deployment.

    ``running=False`` short-circuits to ``INACTIVE``: a stopped core is not a
    failure, but it is never reported as healthy either.
    """
    reasons = [HOST.text(reason) for reason in (degraded_reasons or [])]
    if not running:
        return HealthReport(HEALTH_INACTIVE, reasons + ["core is not running"], {"mode": mode})
    if client is None:
        return HealthReport(HEALTH_FAILED, reasons + ["controller client unavailable"], {"mode": mode})

    data: dict[str, Any] = {"mode": mode, "provider": provider}
    try:
        version = client.version()
    except (ControllerError, FailClosed) as exc:
        return HealthReport(
            HEALTH_FAILED, reasons + [f"controller unreachable: {HOST.text(str(exc))[:120]}"], data
        )
    data["version"] = str(version.get("version") or "unknown")

    if healthcheck:
        try:
            client.healthcheck(provider)
        except (ControllerError, FailClosed) as exc:
            reasons.append(f"provider healthcheck failed: {HOST.text(str(exc))[:120]}")
            data["healthcheck"] = "failed"
            return HealthReport(HEALTH_FAILED, reasons, data)
        data["healthcheck"] = "ok"

    try:
        summary = provider_summary(client)
    except (ControllerError, FailClosed) as exc:
        return HealthReport(
            HEALTH_FAILED, reasons + [f"provider state unreadable: {HOST.text(str(exc))[:120]}"], data
        )

    info = summary.get(provider)
    data["providers"] = summary
    if not info:
        return HealthReport(HEALTH_FAILED, reasons + [f"provider '{provider}' is not visible"], data)

    node_count = int(info.get("node_count") or 0)
    alive = info.get("alive_count")
    data["node_count"] = node_count
    data["alive_count"] = alive
    if node_count == 0:
        return HealthReport(HEALTH_FAILED, reasons + [f"provider '{provider}' has no nodes"], data)
    if alive is None:
        return HealthReport(
            HEALTH_FAILED, reasons + [f"provider '{provider}' node liveness is unknown"], data
        )
    if int(alive) <= 0:
        return HealthReport(HEALTH_FAILED, reasons + ["no live proxy node is available"], data)

    if reasons:
        return HealthReport(HEALTH_DEGRADED, reasons, data)
    return HealthReport(HEALTH_OK, [], data)


def base_url_of(host: str = CONTROLLER_HOST, port: int = CONTROLLER_PORT) -> str:
    return describe_url(f"http://{host}:{port}")


__all__ = [
    "BASE_URL",
    "CONTROLLER_HOST",
    "CONTROLLER_PORT",
    "ControllerClient",
    "ControllerError",
    "HEALTH_DEGRADED",
    "HEALTH_FAILED",
    "HEALTH_INACTIVE",
    "HEALTH_OK",
    "HealthReport",
    "base_url_of",
    "evaluate",
    "provider_summary",
    "readiness",
]
