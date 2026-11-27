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
from typing import Any, Callable, Optional, Union
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


class DeadlineExceeded(ControllerError):
    """No time was left before the caller's deadline: nothing was sent.

    Raised by the helpers below when a request *would* start past the
    deadline, so a multi-request helper cannot stretch a caller's budget.
    """


# (method, url, headers, body, timeout) -> (status, raw bytes)
Transport = Callable[[str, str, dict[str, str], Any, float], tuple[int, bytes]]

#: A request timeout, or a callable re-evaluated before *each* request so a
#: multi-request helper follows a moving (deadline-derived) budget.
TimeoutSpec = Optional[Union[float, Callable[[], float]]]


def _timeout_for(spec: TimeoutSpec, default: float) -> float:
    """Resolve ``spec`` to a positive timeout; past the deadline -> raise."""
    value = spec() if callable(spec) else spec
    if value is None:
        value = default
    value = float(value)
    if value <= 0:
        raise DeadlineExceeded("health deadline expired before the request was sent")
    return value


def _default_timeout(client: Any) -> float:
    """The client's own request timeout (duck-typed clients may omit it)."""
    return float(getattr(client, "timeout", 5.0) or 5.0)


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
    def version(self, *, timeout: float | None = None) -> dict[str, Any]:
        _, payload = self.request("GET", "/version", timeout=timeout)
        return payload if isinstance(payload, dict) else {}

    def proxies(self, *, timeout: float | None = None) -> dict[str, Any]:
        _, payload = self.request("GET", "/proxies", timeout=timeout)
        return payload if isinstance(payload, dict) else {}

    def providers(self, *, timeout: float | None = None) -> dict[str, Any]:
        _, payload = self.request("GET", "/providers/proxies", timeout=timeout)
        return payload if isinstance(payload, dict) else {}

    def provider(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        _, payload = self.request(
            "GET", f"/providers/proxies/{quote(name, safe='')}", timeout=timeout
        )
        return payload if isinstance(payload, dict) else {}

    def healthcheck(self, name: str, *, timeout: float | None = None) -> None:
        """Ask the provider to test every node.

        ``timeout=None`` keeps the client's default (5 s).  The endpoint only
        answers once *all* nodes have been tested, so a caller that must not
        block passes a short timeout and treats the abort as "triggered".
        """
        self.request(
            "GET",
            f"/providers/proxies/{quote(name, safe='')}/healthcheck",
            timeout=timeout,
        )

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


def _node_alive_flags(
    client: ControllerClient,
    *,
    timeout: TimeoutSpec = None,
    errors: list[str] | None = None,
) -> dict[str, bool]:
    """Per-node liveness as ``/proxies`` reports it (missing -> absent).

    ``errors`` is an optional collector: when it is a list, a ``/proxies``
    failure is appended to it so a caller can distinguish "the endpoint is
    broken" from "the endpoint answered without liveness flags".  Without it
    (the default) the historical behaviour is unchanged: the flags are
    supplementary and a failure is simply empty.
    """
    flags: dict[str, bool] = {}
    try:
        payload = client.proxies(timeout=_timeout_for(timeout, _default_timeout(client)))
    except DeadlineExceeded:
        # Liveness flags are supplementary: the provider entry may carry them
        # itself, so an exhausted budget skips this request instead of losing
        # the whole summary.  No request is sent past the deadline, and this is
        # *our* budget - not a broken endpoint - so it is not recorded as a
        # provider error (there is no time left for a /version re-check either).
        return flags
    except (ControllerError, FailClosed) as exc:
        if errors is not None:
            errors.append(HOST.text(str(exc))[:120] or type(exc).__name__)
        return flags
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        if errors is not None:
            errors.append("/proxies returned no proxy table")
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
    client: ControllerClient,
    entries: dict[str, Any] | None = None,
    *,
    timeout: TimeoutSpec = None,
    errors: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Node count *and* live-node count per provider.

    ``alive_count`` is ``None`` when neither the provider entry nor
    ``/proxies`` carries liveness: that is treated as unhealthy (fail closed),
    never as "assume fine".

    ``timeout`` caps each request (``None`` keeps the client default).  A
    callable is re-evaluated before *each* of the two requests, so a summary
    that runs under a deadline cannot overrun it; once the budget is gone
    :class:`DeadlineExceeded` is raised instead of sending a doomed request.

    ``errors`` is passed through to :func:`_node_alive_flags`: with a list, a
    failing ``/proxies`` request is recorded there instead of vanishing, so the
    deadline path can report "provider state unreadable" rather than a
    misleading "liveness is unknown".
    """
    if entries is None:
        payload = client.providers(timeout=_timeout_for(timeout, _default_timeout(client)))
        entries = payload.get("providers") if isinstance(payload, dict) else {}
    entries = entries if isinstance(entries, dict) else {}
    flags = _node_alive_flags(client, timeout=timeout, errors=errors)
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


# ---- deadline-bounded health (macOS switch/start only) --------------------

#: Total time allowed for "trigger healthcheck, then wait for a live node".
HEALTH_DEADLINE_SECONDS = 45.0
#: The healthcheck endpoint answers only after every node was tested; aborting
#: it after this long still means "the test is running".
HEALTHCHECK_TRIGGER_TIMEOUT = 2.0
#: How often the provider summary is polled while waiting for a live node.
HEALTH_POLL_INTERVAL = 1.0


def _health_data(
    mode: str, provider: str, *, version: str = ""
) -> dict[str, Any]:
    data: dict[str, Any] = {"mode": mode, "provider": provider}
    if version:
        data["version"] = version
    return data


def _deadline_expired(seconds: float, stage: str) -> str:
    return f"health deadline of {seconds:g}s expired {stage}"


def evaluate_with_deadline(
    client: ControllerClient | None,
    *,
    provider: str = "subscription",
    mode: str = "tun",
    running: bool = True,
    degraded_reasons: list[str] | None = None,
    deadline_seconds: float = HEALTH_DEADLINE_SECONDS,
    healthcheck_timeout: float = HEALTHCHECK_TRIGGER_TIMEOUT,
    poll_interval: float = HEALTH_POLL_INTERVAL,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> HealthReport:
    """Health verdict that never blocks on the healthcheck endpoint.

    :func:`evaluate` sends ``GET /providers/proxies/<p>/healthcheck`` with the
    client's 5 s default and fails when that request times out.  On macOS the
    endpoint blocks until every node has been tested, so a slow subscription
    produced the bogus ``provider healthcheck failed: controller unreachable:
    TimeoutError``.  This variant:

    1. reads ``/version`` - the only real reachability test;
    2. *triggers* the healthcheck with a short timeout (default 2 s); a
       timeout/abort there counts as "triggered", not as "unreachable";
    3. polls ``/providers/proxies`` + ``/proxies`` until the provider reports
       ``alive_count >= 1``, and gives up at an absolute ``deadline_seconds``.

    ``deadline_seconds`` is a *total* budget: it starts at function entry, so
    ``/version``, the trigger and every poll request are charged to it.  Each
    request gets ``min(its own default, time left)``; once nothing is left the
    verdict is returned immediately instead of sending another request.

    The three failure classes keep distinct messages: an unreachable
    controller, an unreadable provider state, and a provider with no live
    node.  Only ``/version`` decides reachability: when a poll fails, the
    provider endpoint may simply be broken (HTTP 500, a timeout of *its own*
    long-running handler) while the controller is perfectly alive, so the
    verdict is not "unreachable" until a ``/version`` re-check fails too.  The
    re-check runs inside what is left of the same budget, so it can never
    stretch the deadline.  A failing ``/proxies`` counts as a failed poll too:
    it supplies the node liveness flags, so swallowing it would turn a broken
    endpoint into a bogus "liveness is unknown".
    """
    reasons = [HOST.text(reason) for reason in (degraded_reasons or [])]
    if not running:
        return HealthReport(HEALTH_INACTIVE, reasons + ["core is not running"], {"mode": mode})
    if client is None:
        return HealthReport(HEALTH_FAILED, reasons + ["controller client unavailable"], {"mode": mode})

    started = clock()
    budget = max(0.0, float(deadline_seconds))
    deadline = started + budget
    default_timeout = _default_timeout(client)

    def remaining() -> float:
        return deadline - clock()

    def request_timeout(preferred: float) -> float:
        """``min(preferred, time left)``; ``0`` means "do not send"."""
        return max(0.0, min(float(preferred), remaining()))

    version_timeout = request_timeout(default_timeout)
    if version_timeout <= 0:
        return HealthReport(
            HEALTH_FAILED,
            reasons + [_deadline_expired(budget, "before /version could be read")],
            _health_data(mode, provider),
        )
    try:
        version = client.version(timeout=version_timeout)
    except (ControllerError, FailClosed, TimeoutError, OSError) as exc:
        if remaining() <= 0:
            # The request was cut short by the budget, not refused: do not
            # blame the controller for a deadline we imposed ourselves.
            return HealthReport(
                HEALTH_FAILED,
                reasons + [_deadline_expired(budget, "while waiting for /version")],
                _health_data(mode, provider),
            )
        return HealthReport(
            HEALTH_FAILED,
            reasons + [f"controller unreachable: {HOST.text(str(exc))[:120]}"],
            _health_data(mode, provider),
        )
    data = _health_data(mode, provider, version=str(version.get("version") or "unknown"))

    trigger_timeout = request_timeout(healthcheck_timeout)
    if trigger_timeout <= 0:
        data["healthcheck"] = "skipped"
        return HealthReport(
            HEALTH_FAILED,
            reasons
            + [_deadline_expired(budget, "before the provider healthcheck could be triggered")],
            data,
        )
    try:
        client.healthcheck(provider, timeout=trigger_timeout)
        data["healthcheck"] = "ok"
    except (ControllerError, FailClosed, TimeoutError, OSError) as exc:
        # The endpoint answered nothing within the short timeout, or the
        # socket aborted: the provider test was still started.  /version
        # succeeded above, so this is not "controller unreachable".
        data["healthcheck"] = "triggered"
        data["healthcheck_error"] = HOST.text(str(exc))[:120]

    rounds = 0
    info: dict[str, Any] | None = None
    last_error = ""
    expired = False

    def controller_gone(round_no: int, provider_error: str) -> HealthReport | None:
        """Re-check ``/version``; a failed re-check means the controller is gone.

        ``/version`` is the only reachability test: a broken provider endpoint
        (or a broken ``/proxies``) while ``/version`` answers is *unreadable
        state*, not an unreachable controller.  The re-check is charged to what
        is left of the same budget, so it can never stretch the deadline.
        Returns the FAILED report for the unreachable case, ``None`` otherwise.
        """
        recheck_timeout = request_timeout(default_timeout)
        if recheck_timeout <= 0:
            return None
        try:
            client.version(timeout=recheck_timeout)
        except (ControllerError, FailClosed, TimeoutError, OSError) as version_exc:
            # The re-check failed too: the controller really is gone.
            data["rounds"] = round_no
            data["provider_error"] = provider_error
            return HealthReport(
                HEALTH_FAILED,
                reasons
                + ["controller unreachable: " + HOST.text(str(version_exc))[:120]],
                data,
            )
        return None

    while remaining() > 0:
        rounds += 1
        proxy_errors: list[str] = []
        try:
            # The callable is re-read before each request: the two requests of
            # one summary are both capped by what is left, never by a stale
            # budget measured at round start.
            summary = provider_summary(
                client,
                timeout=lambda: request_timeout(default_timeout),
                errors=proxy_errors,
            )
        except DeadlineExceeded:
            expired = True
            break
        except (ControllerError, FailClosed, TimeoutError, OSError) as exc:
            # A failing provider endpoint is *not* proof that the controller
            # is gone: ``/version`` is the only reachability test.  A provider
            # that keeps answering HTTP 500 while ``/version`` is fine must be
            # reported as unreadable state, never as an unreachable
            # controller (the 2026-11-14 misdiagnosis, one layer down).
            last_error = HOST.text(str(exc))[:120]
            failure = controller_gone(rounds, last_error)
            if failure is not None:
                return failure
            # /version answered: keep polling, the provider may recover.
        else:
            if proxy_errors:
                # ``/providers/proxies`` answered but ``/proxies`` did not: the
                # node liveness flags are missing, so this round cannot prove a
                # live node.  Recording the error here is the point - silently
                # returning an empty flag set turned a broken ``/proxies`` into
                # a bogus "liveness is unknown" verdict with no ``last_error``
                # and no ``/version`` re-check (review 11).
                last_error = proxy_errors[0]
                failure = controller_gone(rounds, last_error)
                if failure is not None:
                    return failure
            else:
                last_error = ""
                data["providers"] = summary
                info = summary.get(provider)
                if info is not None:
                    alive = info.get("alive_count")
                    data["node_count"] = int(info.get("node_count") or 0)
                    data["alive_count"] = alive
                    if alive is not None and int(alive) >= 1:
                        data["rounds"] = rounds
                        if reasons:
                            return HealthReport(HEALTH_DEGRADED, reasons, data)
                        return HealthReport(HEALTH_OK, [], data)
        left = remaining()
        if left <= 0:
            break
        sleeper(min(float(poll_interval), left))

    data["rounds"] = rounds
    elapsed = max(0.0, clock() - started)
    if info is None:
        if last_error:
            # Never read a single provider state, but /version never failed a
            # re-check either: the provider endpoint is broken, the controller
            # is not.  This is the "provider state unreadable" class.
            data["provider_error"] = last_error
            return HealthReport(
                HEALTH_FAILED,
                reasons + [f"provider state unreadable: {last_error}"],
                data,
            )
        if expired or rounds == 0:
            return HealthReport(
                HEALTH_FAILED,
                reasons + [_deadline_expired(budget, "before the provider state could be read")],
                data,
            )
        return HealthReport(
            HEALTH_FAILED,
            reasons + [f"provider '{provider}' is not visible after {elapsed:g}s"],
            data,
        )
    node_count = int(info.get("node_count") or 0)
    alive = info.get("alive_count")
    # A provider that was read once and then started failing keeps the last
    # *known* verdict ("no alive node"), but the staleness is stated too: the
    # error is not swallowed, and /version never failed its re-check, so this
    # is still not an unreachable controller.
    stale = (
        "; provider state unreadable after the last successful read: " + last_error
        if last_error
        else ""
    )
    if alive is None:
        return HealthReport(
            HEALTH_FAILED,
            reasons
            + [
                f"provider '{provider}' liveness is unknown after {elapsed:g}s "
                f"({node_count} nodes tested){stale}"
            ],
            data,
        )
    return HealthReport(
        HEALTH_FAILED,
        reasons
        + [
            f"no alive node in provider {provider} after {elapsed:g}s "
            f"({node_count} nodes tested){stale}"
        ],
        data,
    )


__all__ = [
    "BASE_URL",
    "CONTROLLER_HOST",
    "CONTROLLER_PORT",
    "ControllerClient",
    "ControllerError",
    "DeadlineExceeded",
    "HEALTHCHECK_TRIGGER_TIMEOUT",
    "HEALTH_DEADLINE_SECONDS",
    "HEALTH_DEGRADED",
    "HEALTH_FAILED",
    "HEALTH_INACTIVE",
    "HEALTH_OK",
    "HEALTH_POLL_INTERVAL",
    "HealthReport",
    "base_url_of",
    "evaluate",
    "evaluate_with_deadline",
    "provider_summary",
    "readiness",
]
