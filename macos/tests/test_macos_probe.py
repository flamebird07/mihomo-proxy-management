"""Post-switch probe: real HTTP status semantics, wall-clock deadline, fake-ip."""

from __future__ import annotations

import pytest

from mpm_common.executor import FakeExecutor
from mpm_macos import probe

pytestmark = pytest.mark.macos

BAIDU = "https://www.baidu.com"
GOOGLE = "https://www.google.com/generate_204"
CHATGPT = "https://chatgpt.com/cdn-cgi/trace"
CURSOR = "https://cursor.com"
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"


def _executor(
    codes: dict,
    *,
    remote: dict | None = None,
    trace: str = "loc=JP\n",
    routes: dict | None = None,
) -> FakeExecutor:
    remote = remote or {}
    routes = routes or {}

    def handler(argv):
        url = argv[-1]
        if url == TRACE:
            return (0, trace, "")
        code = codes.get(url, 0)
        ip = remote.get(url, "203.0.113.9")
        return (0, f"{code:03d} {ip}\n", "")

    executor = FakeExecutor()
    executor.set("curl", handler)
    executor.set(
        "route -n get",
        lambda argv: (0, f"interface: {routes.get(argv[-1], 'en0')}\n", ""),
    )
    return executor


def _run(executor, **kwargs):
    kwargs.setdefault("max_rounds", 1)
    kwargs.setdefault("sleeper", lambda _s: None)
    return probe.run_probe(executor, **kwargs)


def test_probe_domestic_fail_triggers_rollback():
    report = _run(
        _executor({BAIDU: 500, GOOGLE: 204, CHATGPT: 200, CURSOR: 200})
    )
    assert report.ok is False
    assert "baidu" in report.reason


def test_probe_all_foreign_fail_triggers_rollback():
    report = _run(
        _executor({BAIDU: 200, GOOGLE: 0, CHATGPT: 0, CURSOR: 0})
    )
    assert report.ok is False
    assert "foreign" in report.reason


def test_probe_partial_foreign_ok_passes():
    report = _run(
        _executor({BAIDU: 200, GOOGLE: 204, CHATGPT: 0, CURSOR: 0})
    )
    assert report.ok is True
    assert report.rounds == 1


def test_probe_force_fail_env_triggers_rollback(monkeypatch):
    monkeypatch.setenv("MPM_PROBE_FORCE_FAIL", "1")
    report = _run(_executor({BAIDU: 200, GOOGLE: 204, CHATGPT: 200, CURSOR: 200}))
    assert report.ok is False
    assert "drill" in report.reason


def test_probe_fake_ip_remote_is_not_success():
    """A 198.18.x answer routed to Nextin's utun is not mihomo's TUN."""
    report = _run(
        _executor(
            {BAIDU: 200, GOOGLE: 204, CHATGPT: 0, CURSOR: 0},
            remote={GOOGLE: "198.18.5.5"},
            routes={"198.18.5.5": "utun3"},
        ),
        mihomo_utuns=("utun7",),
    )
    assert report.ok is False
    google = next(item for item in report.items if item.name == "google")
    assert google.ok is False
    assert "fake-ip" in google.reason


def test_probe_fake_ip_via_mihomo_utun_is_success():
    """A fake-ip answer carried by this start's new utun is a real success.

    macOS TUN mode can take over through more specific routes while
    ``route get default`` still shows the physical link, so the verdict must
    come from ``route -n get <remote_ip>`` and the before/after utun diff.
    """
    report = _run(
        _executor(
            {BAIDU: 200, GOOGLE: 204, CHATGPT: 200, CURSOR: 200},
            remote={GOOGLE: "198.19.0.1"},
            routes={"198.19.0.1": "utun7"},
        ),
        mihomo_utuns=("utun7",),
    )
    assert report.ok is True


class _Clock:
    """Deterministic monotonic clock whose ``sleep`` advances time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def test_probe_deadline_is_wall_clock_60s():
    """Four slow targets per round must not stretch 60 s into minutes."""
    clock = _Clock()
    limits: list[float] = []

    def handler(argv):
        limit = float(argv[argv.index("--max-time") + 1])
        limits.append(limit)
        clock.now += min(7.0, limit)  # a real request never exceeds its -m
        return (0, "000 203.0.113.9\n", "")

    executor = FakeExecutor()
    executor.set("curl", handler)
    executor.set("route -n get", (0, "interface: en0\n", ""))

    report = probe.run_probe(
        executor, window=60.0, interval=5.0, sleeper=clock.sleep, clock=clock
    )

    assert report.ok is False
    assert clock.now <= 60.0 + 1e-9, "the deadline is wall clock, not a round count"
    assert report.rounds == 2
    assert len(limits) == 8, "no request may start after the deadline"
    assert max(limits) <= 8
    assert min(limits) < 8, "the last request must be capped by the remaining window"


def test_probe_last_request_never_exceeds_deadline():
    """Fractional budgets, and no request below the minimum window remains."""
    clock = _Clock()
    limits: list[float] = []

    def handler(argv):
        limit = float(argv[argv.index("--max-time") + 1])
        assert clock.now + limit <= 9.5 + 1e-9, "a request may overrun the deadline"
        limits.append(limit)
        clock.now += limit  # a real request consumes at most its -m
        return (0, "000 203.0.113.9\n", "")

    executor = FakeExecutor()
    executor.set("curl", handler)
    executor.set("route -n get", (0, "interface: en0\n", ""))

    report = probe.run_probe(
        executor,
        window=9.5,
        interval=5.0,
        sleeper=lambda _s: None,
        clock=clock,
        max_rounds=1,
    )

    assert report.ok is False
    assert clock.now <= 9.5 + 1e-9
    # 9.5 s window: baidu gets 8, google the exact 1.5 remainder (float,
    # never rounded up to 2), then the window is gone.
    assert limits == [8.0, 1.5]
    deadline_items = [
        item for item in report.items if item.reason == "probe deadline reached"
    ]
    assert [item.name for item in deadline_items] == ["chatgpt", "cursor"]


def test_probe_skips_requests_below_the_minimum_budget():
    """With only 0.4 s left, the request is not started at all."""
    readings = iter([0.0, 8.0, 8.6, 8.6])
    clock = lambda: next(readings)  # noqa: E731

    executor = _executor({BAIDU: 0, GOOGLE: 0, CHATGPT: 0, CURSOR: 0})
    before = len(executor.calls)

    items = probe.probe_once(executor, clock=clock, deadline=9.0)

    assert sum(1 for call in executor.calls[before:] if call.argv[0] == "curl") == 2
    for item in items:
        if item.name in ("chatgpt", "cursor"):
            assert item.status == 0
            assert item.reason == "probe deadline reached"
        else:
            assert item.reason != "probe deadline reached"


def test_curl_status_parses_code_and_remote_ip():
    status, remote = probe.curl_status(
        _executor({BAIDU: 204}, remote={BAIDU: "203.0.113.7"}), BAIDU
    )
    assert status == 204
    assert remote == "203.0.113.7"


def test_exit_location_reads_the_trace():
    assert probe.exit_location(_executor({}, trace="ip=203.0.113.1\nloc=SG\n")) == "SG"
