"""Deadline-bounded controller health (macOS start gate).

The 2026-11-14 outage produced ``provider healthcheck failed: controller
unreachable: TimeoutError``: ``/providers/proxies/<p>/healthcheck`` only
answers after every node was tested, so the client's 5 s default timed out and
was misreported as an unreachable controller.

``evaluate_with_deadline`` triggers the healthcheck with a short timeout and
then polls for ``alive_count >= 1`` until an absolute deadline.  Every check
here is host-free: the controller is a transport function and the clock is a
fake whose ``sleep`` advances time, so nothing really sleeps.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
_COMMON = str(ROOT / "common" / "python")
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)

from mpm_common.controller_health import (  # noqa: E402  (path bootstrap above)
    ControllerClient,
    ControllerError,
    HEALTH_DEGRADED,
    HEALTH_FAILED,
    HEALTH_INACTIVE,
    HEALTH_OK,
    evaluate,
    evaluate_with_deadline,
)

# Runtime canary: never a credential-shaped literal in the repository.
SECRET = "canary-" + secrets.token_hex(8)


class _Clock:
    """Deterministic monotonic clock whose ``sleep`` advances time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def _client(
    *,
    version_ok: bool = True,
    healthcheck_error: type[BaseException] | None = TimeoutError,
    alive_sequence: list[int | None] | None = None,
    nodes: int = 2,
    visible: bool = True,
    proxies_500: bool = False,
    captured: list | None = None,
) -> ControllerClient:
    """Controller fake: ``alive_sequence[i]`` is the live-node count of round i.

    ``None`` means "liveness unknown" (no ``alive`` flag anywhere), which must
    never be read as healthy.  ``proxies_500`` makes ``/proxies`` answer HTTP
    500 while ``/providers/proxies`` keeps working.
    """
    sequence = list(alive_sequence if alive_sequence is not None else [nodes])
    state = {"round": 0}

    def alive_for_round() -> int | None:
        index = min(max(state["round"] - 1, 0), len(sequence) - 1)
        return sequence[index]

    def transport(method, url, headers, body, timeout):
        path = urlsplit(url).path
        if captured is not None:
            captured.append({"path": path, "timeout": timeout})
        if path == "/version":
            if not version_ok:
                raise ControllerError("connection refused")
            return (200, json.dumps({"version": "1.19.0"}).encode())
        if path.endswith("/healthcheck"):
            if healthcheck_error is not None:
                raise healthcheck_error("healthcheck request timed out")
            return (204, b"")
        if path == "/providers/proxies":
            state["round"] += 1
            alive = alive_for_round()
            if not visible:
                return (200, json.dumps({"providers": {}}).encode())
            if alive is None:
                entries = [{"name": f"node-{index}"} for index in range(nodes)]
            else:
                entries = [
                    {"name": f"node-{index}", "alive": index < alive}
                    for index in range(nodes)
                ]
            payload = {"providers": {"subscription": {"all": entries}}}
            return (200, json.dumps(payload).encode())
        if path == "/proxies":
            if proxies_500:
                return (500, b"")
            alive = alive_for_round()
            if alive is None:
                proxies: dict = {}
            else:
                proxies = {
                    f"node-{index}": {"alive": index < alive}
                    for index in range(nodes)
                }
            return (200, json.dumps({"proxies": proxies}).encode())
        return (404, b"")

    return ControllerClient(secret=SECRET, transport=transport)


def _evaluate(client, clock, **kwargs):
    options = {
        "deadline_seconds": 3.0,
        "poll_interval": 1.0,
        "sleeper": clock.sleep,
        "clock": clock,
    }
    options.update(kwargs)
    return evaluate_with_deadline(client, **options)


def _advancing_client(
    clock: _Clock,
    *,
    version_cost: float = 0.0,
    version_times_out: bool = False,
    version_fails_after_poll: int | None = None,
    trigger_cost: float = 0.0,
    poll_cost: float = 0.0,
    provider_fails_after: int | None = None,
    provider_status: int = 500,
    proxies_fails_after: int | None = None,
    proxies_status: int = 500,
    alive_sequence: list[int | None] | None = None,
    nodes: int = 2,
    captured: list | None = None,
) -> ControllerClient:
    """Controller fake whose requests *consume* the fake clock.

    A request never runs past the ``timeout`` it was handed, so a caller can
    prove that ``deadline_seconds`` is a total budget: nothing is sent with
    more time than is left, and nothing is sent once the budget is gone.
    ``captured`` records ``(path, timeout, time at issue)``.

    ``provider_fails_after`` makes ``/providers/proxies`` answer
    ``provider_status`` from that poll round on (0 = every round),
    ``proxies_fails_after`` does the same for ``/proxies``, and
    ``version_fails_after_poll`` makes ``/version`` fail once that many polls
    have completed: the two halves of the "provider endpoint broken vs
    controller gone" distinction.
    """
    sequence = list(alive_sequence if alive_sequence is not None else [nodes])
    state = {"round": 0}

    def spend(path: str, cost: float, timeout: float) -> None:
        if captured is not None:
            captured.append({"path": path, "timeout": timeout, "at": clock.now})
        clock.advance(min(cost, timeout))

    def transport(method, url, headers, body, timeout):
        path = urlsplit(url).path
        if path == "/version":
            spend("/version", version_cost, timeout)
            if version_times_out:
                raise TimeoutError("version request timed out")
            if (
                version_fails_after_poll is not None
                and state["round"] >= version_fails_after_poll
            ):
                raise ControllerError("connection refused")
            return (200, json.dumps({"version": "1.19.0"}).encode())
        if path.endswith("/healthcheck"):
            spend("/healthcheck", trigger_cost, timeout)
            raise TimeoutError("healthcheck request timed out")
        if path == "/providers/proxies":
            spend("/providers/proxies", poll_cost, timeout)
            index = min(state["round"], len(sequence) - 1)
            failing = (
                provider_fails_after is not None
                and state["round"] >= provider_fails_after
            )
            state["round"] += 1
            if failing:
                return (provider_status, b"")
            alive = sequence[index]
            if alive is None:
                entries = [{"name": f"node-{i}"} for i in range(nodes)]
            else:
                entries = [
                    {"name": f"node-{i}", "alive": i < alive} for i in range(nodes)
                ]
            payload = {"providers": {"subscription": {"all": entries}}}
            return (200, json.dumps(payload).encode())
        if path == "/proxies":
            spend("/proxies", 0.0, timeout)
            if (
                proxies_fails_after is not None
                and state["round"] - 1 >= proxies_fails_after
            ):
                return (proxies_status, b"")
            alive = sequence[min(max(state["round"] - 1, 0), len(sequence) - 1)]
            proxies = (
                {}
                if alive is None
                else {f"node-{i}": {"alive": i < alive} for i in range(nodes)}
            )
            return (200, json.dumps({"proxies": proxies}).encode())
        return (404, b"")

    return ControllerClient(secret=SECRET, transport=transport)


def test_healthcheck_timeout_counts_as_triggered_and_polling_wins():
    """A blocking healthcheck is not "unreachable" when a node is alive."""
    clock = _Clock()
    report = _evaluate(_client(alive_sequence=[0, 1]), clock)

    assert report.state == HEALTH_OK, report.reasons
    assert report.data["healthcheck"] == "triggered"
    assert report.data["alive_count"] == 1
    assert report.data["rounds"] == 2
    assert clock.now == pytest.approx(1.0)
    assert clock.sleeps == [1.0], "one poll interval, no real sleep"


def test_healthcheck_uses_a_short_timeout_and_polls_with_the_default():
    captured: list = []
    clock = _Clock()
    report = _evaluate(
        _client(captured=captured),
        clock,
        # A budget larger than every default, so each request keeps its own
        # timeout; the deadline itself is covered by the tests below.
        deadline_seconds=60.0,
        healthcheck_timeout=2.0,
    )

    assert report.state == HEALTH_OK
    timeouts = {item["path"]: item["timeout"] for item in captured}
    assert timeouts["/version"] == 5.0
    assert timeouts["/providers/proxies/subscription/healthcheck"] == 2.0
    assert timeouts["/providers/proxies"] == 5.0


def test_version_and_trigger_are_charged_to_the_same_deadline():
    """The budget starts at entry: a slow /version and trigger exhaust it."""
    clock = _Clock()
    captured: list = []
    client = _advancing_client(
        clock, version_cost=3.0, trigger_cost=2.0, captured=captured
    )

    report = _evaluate(client, clock, deadline_seconds=5.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == [
        "health deadline of 5s expired before the provider state could be read"
    ]
    assert report.data["rounds"] == 0
    assert clock.now == pytest.approx(5.0)
    assert clock.sleeps == [], "no poll may start after the budget is gone"
    # each request was capped by what was left, not by its own default
    assert [(item["path"], item["timeout"]) for item in captured] == [
        ("/version", 5.0),
        ("/healthcheck", 2.0),
    ]


def test_version_cut_short_by_the_deadline_is_not_reported_as_unreachable():
    """A /version we timed out ourselves is a deadline, not a dead controller."""
    clock = _Clock()
    client = _advancing_client(clock, version_cost=5.0, version_times_out=True)

    report = _evaluate(client, clock, deadline_seconds=5.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == ["health deadline of 5s expired while waiting for /version"]
    assert clock.sleeps == []


def test_last_poll_round_is_truncated_to_the_remaining_time():
    """A slow poll gets ``min(default, remaining)`` and never overruns."""
    clock = _Clock()
    captured: list = []
    client = _advancing_client(
        clock, poll_cost=2.5, alive_sequence=[0], captured=captured
    )

    report = _evaluate(client, clock, deadline_seconds=5.0, poll_interval=1.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == [
        "no alive node in provider subscription after 5s (2 nodes tested)"
    ]
    assert report.data["rounds"] == 2
    assert clock.now == pytest.approx(5.0), "the total budget is never exceeded"
    assert clock.sleeps == [1.0]
    polls = [item for item in captured if item["path"] == "/providers/proxies"]
    assert [item["timeout"] for item in polls] == pytest.approx([5.0, 1.5])
    for item in captured:
        assert item["at"] + item["timeout"] <= 5.0 + 1e-9


def test_deadline_expiry_without_a_live_node_reports_the_counts():
    clock = _Clock()
    report = _evaluate(_client(alive_sequence=[0]), clock, deadline_seconds=3.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == [
        "no alive node in provider subscription after 3s (2 nodes tested)"
    ]
    assert report.data["healthcheck"] == "triggered"
    # polls at t=0, 1, 2; at t=3 nothing is left, so no fourth round is sent
    assert report.data["rounds"] == 3
    assert clock.now == pytest.approx(3.0)


def test_unreachable_controller_is_reported_as_unreachable():
    """A failing ``/version`` is the only "controller unreachable" verdict."""
    clock = _Clock()
    report = _evaluate(_client(version_ok=False), clock)

    assert report.state == HEALTH_FAILED
    assert report.reasons[0].startswith("controller unreachable:")
    assert "healthcheck" not in report.data
    assert clock.sleeps == []


def test_provider_http_500_with_a_live_version_is_unreadable_state():
    """A broken provider endpoint is *not* an unreachable controller.

    The 2026-11-14 misdiagnosis one layer down: ``/providers/proxies`` keeps
    answering HTTP 500 while ``/version`` is fine.  Every failed poll must be
    re-checked against ``/version``; only a failed re-check may say
    "unreachable", otherwise the provider state is what cannot be read.
    """
    clock = _Clock()
    captured: list = []
    client = _advancing_client(clock, provider_fails_after=0, captured=captured)

    report = _evaluate(client, clock, deadline_seconds=3.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == [
        "provider state unreadable: "
        "controller returned HTTP 500 for GET /providers/proxies"
    ]
    versions = [item for item in captured if item["path"] == "/version"]
    assert len(versions) > 1, "a failed poll must be re-checked against /version"
    assert clock.now == pytest.approx(3.0)
    for item in captured:
        assert item["at"] + item["timeout"] <= 3.0 + 1e-9


def test_controller_dying_mid_poll_is_reported_unreachable():
    """The provider read worked, then both the poll *and* ``/version`` fail."""
    clock = _Clock()
    client = _advancing_client(
        clock,
        provider_fails_after=1,
        version_fails_after_poll=1,
        alive_sequence=[0],
    )

    report = _evaluate(client, clock, deadline_seconds=3.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons[0].startswith("controller unreachable: ")
    assert "connection refused" in report.reasons[0]
    assert report.data["provider_error"].startswith(
        "controller returned HTTP 500 for GET /providers/proxies"
    )
    assert clock.now < 3.0, "an unreachable controller is reported at once"


def test_a_read_provider_that_then_fails_is_not_reported_as_unreachable():
    """A stale but real verdict stays "no alive node", with the error stated."""
    clock = _Clock()
    client = _advancing_client(clock, provider_fails_after=1, alive_sequence=[0])

    report = _evaluate(client, clock, deadline_seconds=3.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons[0].startswith(
        "no alive node in provider subscription after 3s (2 nodes tested)"
    )
    assert (
        "provider state unreadable after the last successful read: "
        "controller returned HTTP 500 for GET /providers/proxies"
    ) in report.reasons[0]


def test_version_recheck_never_exceeds_the_total_deadline():
    """The re-check is charged to the same budget, never added on top of it."""
    clock = _Clock()
    captured: list = []
    client = _advancing_client(
        clock,
        poll_cost=1.5,
        provider_fails_after=0,
        version_cost=0.5,
        captured=captured,
    )

    report = _evaluate(client, clock, deadline_seconds=3.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons[0].startswith("provider state unreadable: ")
    assert clock.now == pytest.approx(3.0)
    for item in captured:
        assert item["at"] + item["timeout"] <= 3.0 + 1e-9
    recheck = [item for item in captured if item["path"] == "/version"][-1]
    assert recheck["at"] == pytest.approx(2.0)
    assert recheck["timeout"] == pytest.approx(1.0), "capped by what was left"


def test_unknown_liveness_at_the_deadline_is_not_healthy():
    clock = _Clock()
    report = _evaluate(_client(alive_sequence=[None]), clock, deadline_seconds=2.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == [
        "provider 'subscription' liveness is unknown after 2s (2 nodes tested)"
    ]


def test_missing_provider_is_reported_separately():
    clock = _Clock()
    report = _evaluate(_client(visible=False), clock, deadline_seconds=2.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == ["provider 'subscription' is not visible after 2s"]


def test_degraded_reasons_keep_the_verdict_degraded():
    clock = _Clock()
    report = _evaluate(_client(), clock, degraded_reasons=["TUN unavailable"])

    assert report.state == HEALTH_DEGRADED
    assert report.reasons == ["TUN unavailable"]
    assert clock.sleeps == [], "an alive provider needs no polling"


def test_a_stopped_core_is_inactive():
    clock = _Clock()
    report = evaluate_with_deadline(_client(), running=False, sleeper=clock.sleep, clock=clock)

    assert report.state == HEALTH_INACTIVE
    assert clock.sleeps == []


def test_no_client_is_failed():
    report = evaluate_with_deadline(None)
    assert report.state == HEALTH_FAILED


# ---- a broken ``/proxies`` is an error, not "liveness is unknown" ---------


def test_proxies_500_with_a_live_version_is_unreadable_state():
    """``/providers/proxies`` fine + ``/proxies`` 500 must not read as "unknown".

    ``/proxies`` carries the node liveness flags.  Swallowing its error turned
    a persistently broken endpoint into ``liveness is unknown`` with no
    ``last_error`` and no ``/version`` re-check (review 11): the verdict must
    name the unreadable provider state instead.
    """
    clock = _Clock()
    captured: list = []
    client = _advancing_client(
        clock, proxies_fails_after=0, captured=captured
    )

    report = _evaluate(client, clock, deadline_seconds=3.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons == [
        "provider state unreadable: controller returned HTTP 500 for GET /proxies"
    ]
    assert report.data["provider_error"].startswith(
        "controller returned HTTP 500 for GET /proxies"
    )
    versions = [item for item in captured if item["path"] == "/version"]
    assert len(versions) > 1, "a failing /proxies must be re-checked against /version"
    assert clock.now == pytest.approx(3.0)
    for item in captured:
        assert item["at"] + item["timeout"] <= 3.0 + 1e-9


def test_proxies_500_and_a_dead_version_is_unreachable():
    """When the ``/version`` re-check fails too, the controller really is gone."""
    clock = _Clock()
    client = _advancing_client(
        clock, proxies_fails_after=0, version_fails_after_poll=1
    )

    report = _evaluate(client, clock, deadline_seconds=3.0)

    assert report.state == HEALTH_FAILED
    assert report.reasons[0].startswith("controller unreachable: ")
    assert "connection refused" in report.reasons[0]
    assert report.data["provider_error"].startswith(
        "controller returned HTTP 500 for GET /proxies"
    )
    assert clock.now < 3.0, "an unreachable controller is reported at once"


def test_evaluate_still_treats_proxies_as_supplementary():
    """``evaluate()`` keeps its historical behaviour (no ``errors`` collector).

    A broken ``/proxies`` alone does not change the blocking verdict there: the
    provider entry may carry the liveness itself, and that path must not gain
    a new failure mode.
    """
    report = evaluate(_client(proxies_500=True), healthcheck=False)

    assert report.state == HEALTH_OK, report.reasons
