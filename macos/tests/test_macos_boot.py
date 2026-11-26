"""The boot entry: wait for a usable machine, then the same safe switch.

All fakes are injected (``stat``/``route``/``scutil``/``launchctl``/probe) and
the clock never sleeps, so nothing here touches the host, and no test needs a
reboot.
"""

from __future__ import annotations

import io
import os
import plistlib
import stat

import pytest

from mpm_common.errors import ExitCode, FailClosed
from mpm_common.executor import FakeExecutor
from mpm_macos import boot, context, lifecycle, plist, preflight, probe, switch

from macos_test_support import (
    BOOT_LABEL,
    BOOT_PLIST_TEMPLATE,
    FakeController,
    LaunchdHarness,
    _target_label,
    assert_posix_mode,
    darwin_facts,
    launchd_facts,
    make_context,
    prepare_install,
)
from test_macos_switch import TeardownVpn, _root_ctx

pytestmark = pytest.mark.macos

CONSOLE = boot.ConsoleUser(name="tester", uid=501)


def _console_ready(_executor=None):
    return CONSOLE


def _route_ready(_executor=None) -> bool:
    return True


def _route_down(_executor=None) -> bool:
    return False


def _start_ok(_ctx, *, quiet=False, **kwargs):
    """A start that succeeds without touching launchd (the switch is under test)."""
    return lifecycle.Report(
        command="start", status="OK", data={"new_utun_interfaces": ["utun7"]}
    )


# ---- console user ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tester 501\n", boot.ConsoleUser("tester", 501)),
        ("root 0\n", None),
        ("_mbsetupuser 248\n", None),
        ("loginwindow 255\n", None),
        ("", None),
        ("garbage\n", None),
        ("tester notanumber\n", None),
    ],
)
def test_parse_console_owner(text, expected):
    assert boot.parse_console_owner(text) == expected


def test_console_owner_reads_dev_console_and_skips_the_login_window():
    executor = FakeExecutor()
    executor.set("stat -f", (0, "root 0\n", ""))
    assert boot.console_owner(executor) is None

    executor.set("stat -f", (0, "tester 501\n", ""))
    assert boot.console_owner(executor) == CONSOLE

    executor.set("stat -f", (1, "", "stat: /dev/console: No such file"))
    assert boot.console_owner(executor) is None


def test_console_timeout_has_zero_side_effects(tmp_path):
    """No login user: no mihomo, no Nextin start - the login window is not a session."""
    ctx = _root_ctx(tmp_path, connected=True)

    report = boot.boot_switch(
        ctx, console_reader=lambda _executor: None, route_reader=_route_ready
    )

    assert report.status == "FAILED"
    assert report.exit_code == int(ExitCode.NOT_READY)
    assert report.data["console_wait"]["ok"] is False
    assert "network_wait" not in report.data, "the network is not probed before a user exists"
    assert "console user not ready" in " ".join(report.warnings)
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert not any("--nc start" in event for event in ctx.executor.events)
    assert not any("--nc stop" in event for event in ctx.executor.events)
    assert ctx.executor.connected is True


# ---- default route --------------------------------------------------------


def test_no_default_route_never_starts_anything(tmp_path):
    """A machine without a route cannot start a TUN stack: wait, then do nothing."""
    ctx = _root_ctx(tmp_path, connected=True)

    report = boot.boot_switch(
        ctx, console_reader=_console_ready, route_reader=_route_down
    )

    assert report.status == "FAILED"
    assert report.exit_code == int(ExitCode.NOT_READY)
    assert report.data["network_wait"]["ok"] is False
    assert "default route not ready" in " ".join(report.warnings)
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert not any("--nc start" in event for event in ctx.executor.events)
    assert not any("--nc stop" in event for event in ctx.executor.events)
    assert ctx.executor.connected is True


def test_network_becoming_ready_continues_the_switch(tmp_path, monkeypatch):
    """The wait is a wait, not a bail-out: the switch runs once the route is up."""
    ctx = _root_ctx(tmp_path, connected=False)
    calls = {"count": 0}

    def route_later(_executor=None) -> bool:
        calls["count"] += 1
        return calls["count"] >= 3

    monkeypatch.setattr(switch.lifecycle, "start", _start_ok)

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=route_later,
        probe_runner=lambda: probe.ProbeReport(ok=True),
    )

    assert report.status == "OK", report.warnings
    assert report.data["network_wait"]["ok"] is True
    assert report.data["console_user"]["uid"] == 501
    assert calls["count"] >= 3
    assert report.data["stop_wait"]["ok"] is True
    assert any("--nc stop" in event for event in ctx.executor.events)


# ---- the switch itself is the switch module's flow ------------------------


def test_boot_waits_for_the_nextin_teardown_before_starting_mihomo(
    tmp_path, monkeypatch
):
    """Nextin's ProxyTunnel lingers: the boot entry waits exactly like ``mpm-switch``."""
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor = TeardownVpn(
        states=["connected", "disconnecting", "disconnected"], listener_polls=2
    )
    LaunchdHarness(ctx.executor)
    ctx.conflict_detector = None  # the real detector: a listener on 7890 is a conflict

    listening_at_start: list[bool] = []

    def fake_start(ctx_arg, quiet=False, **kwargs):
        listening_at_start.append(ctx_arg.executor.listening)
        return _start_ok(ctx_arg)

    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=True),
    )

    assert report.status == "OK", report.warnings
    assert listening_at_start == [False], "mihomo starts only after ProxyTunnel exited"
    assert report.data["stop_wait"]["ok"] is True
    assert report.data["stop_wait"]["state"] == "disconnected"
    assert ctx.executor.port_polls > 2
    assert any(
        event.startswith("launchctl asuser 501 sudo -u tester scutil --nc stop")
        for event in ctx.executor.events
    )


def test_probe_failure_rolls_back_to_nextin(tmp_path, monkeypatch):
    """A failed switch ends with Nextin connected and the core stopped+disabled."""
    ctx = _root_ctx(tmp_path, connected=False)
    harness = LaunchdHarness(ctx.executor)

    def fake_start(ctx_arg, quiet=False, **kwargs):
        ctx_arg.launchd().bootstrap()  # the core is up before the probe fails
        return _start_ok(ctx_arg)

    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=False, reason="drill: forced failure"),
    )

    assert report.status == "FAILED"
    assert harness.loaded is False
    assert harness.disabled is True, "the core must not come back at the next boot"
    assert ctx.executor.connected is True, "rollback must end with Nextin connected"
    assert ctx.executor.count("launchctl", "bootstrap") == 1


def test_drill_fail_is_forwarded_to_the_root_phase(tmp_path, monkeypatch):
    """``--drill-fail`` / ``MPM_PROBE_FORCE_FAIL`` reach the switch unchanged."""
    ctx = _root_ctx(tmp_path, connected=False)
    seen: dict = {}

    def fake_root(ctx_arg, **kwargs):
        seen.update(kwargs)
        return lifecycle.Report(command="switch", status="OK")

    monkeypatch.setattr(switch, "switch_to_mihomo_root", fake_root)

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        force_fail=True,
    )

    assert report.status == "OK"
    assert seen["force_fail"] is True
    assert seen["uid"] == 501
    assert seen["user"] == "tester"
    assert seen["vpn_name"] == "Nextin"


def test_on_demand_true_refuses_and_touches_nothing(tmp_path):
    """Always On VPN (On Demand): refuse at boot too, before any stop."""
    ctx = _root_ctx(tmp_path, connected=True, on_demand=True)

    report = boot.boot_switch(
        ctx, console_reader=_console_ready, route_reader=_route_ready
    )

    assert report.status == "FAILED"
    assert report.data["on_demand"] == "enabled"
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert not any("--nc stop" in event for event in ctx.executor.events)
    assert not any("--nc start" in event for event in ctx.executor.events)
    assert ctx.executor.connected is True
    assert "Always On VPN" in " ".join(report.warnings)


def test_already_in_charge_is_idempotent(tmp_path):
    """mihomo running + Nextin down: succeed without bouncing the core."""
    ctx = _root_ctx(tmp_path, connected=False)
    harness = LaunchdHarness(ctx.executor)
    harness.loaded = True
    harness.running = True

    report = boot.boot_switch(
        ctx, console_reader=_console_ready, route_reader=_route_ready
    )

    assert report.status == "OK"
    assert report.data["already_in_charge"] is True
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert ctx.executor.count("launchctl", "bootout") == 0
    assert ctx.executor.count("launchctl", "kickstart") == 0
    assert not any("--nc stop" in event for event in ctx.executor.events)


def test_a_running_core_with_nextin_up_still_runs_the_switch(tmp_path, monkeypatch):
    """Two stacks is not "already in charge": the full flow (and teardown) runs."""
    ctx = _root_ctx(tmp_path, connected=True)
    harness = LaunchdHarness(ctx.executor)
    harness.loaded = True
    harness.running = True
    monkeypatch.setattr(switch.lifecycle, "start", _start_ok)

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=True),
    )

    assert report.status == "OK", report.warnings
    assert report.data["already_in_charge"] is False
    assert any("--nc stop" in event for event in ctx.executor.events)
    assert ctx.executor.connected is False


# ---- GPT exit after the switch ------------------------------------------


def _make_valid_provider(ctx) -> None:
    os.makedirs(ctx.layout.providers, exist_ok=True)
    with open(ctx.layout.valid_provider_file, "w", encoding="utf-8") as handle:
        handle.write("proxies:\n  - name: US fast\n    type: ss\n")


def test_boot_picks_a_verified_gpt_exit_after_the_switch(tmp_path, monkeypatch):
    """The selector defaults to AUTO-FOREIGN; boot must run the verified pick."""
    ctx = _root_ctx(tmp_path, connected=True)
    _make_valid_provider(ctx)
    monkeypatch.setattr(switch.lifecycle, "start", _start_ok)
    calls: list[str] = []

    def gpt(layout, *, say):
        calls.append(layout.valid_provider_file)
        say("gpt pick ran")
        return "US fast"

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=True),
        gpt_runner=gpt,
    )

    assert report.status == "OK", report.warnings
    assert calls == [ctx.layout.valid_provider_file]
    assert any("GPT exit verified: US fast" in message for message in report.messages)
    assert not any("--nc start" in event for event in ctx.executor.events)


def test_boot_gpt_pick_failure_is_a_warning_not_a_rollback(tmp_path, monkeypatch):
    ctx = _root_ctx(tmp_path, connected=True)
    _make_valid_provider(ctx)
    monkeypatch.setattr(switch.lifecycle, "start", _start_ok)

    def gpt(_layout, *, say):
        raise RuntimeError("controller unreachable")

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=True),
        gpt_runner=gpt,
    )

    assert report.status == "OK"
    assert any("GPT exit selection failed" in warning for warning in report.warnings)
    assert not any("--nc start" in event for event in ctx.executor.events)


def test_boot_does_not_pick_gpt_without_a_valid_provider(tmp_path, monkeypatch):
    """No node-valid pool: GPT is the regional fallback, there is no selector."""
    ctx = _root_ctx(tmp_path, connected=True)
    monkeypatch.setattr(switch.lifecycle, "start", _start_ok)
    called: list[bool] = []

    def gpt(_layout, *, say):  # pragma: no cover - must never be called
        called.append(True)
        return "US fast"

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=True),
        gpt_runner=gpt,
    )

    assert report.status == "OK"
    assert called == []


def test_already_in_charge_does_not_restart_the_gpt_pick(tmp_path):
    """A kickstart over a working core keeps its zero-work contract."""
    ctx = _root_ctx(tmp_path, connected=False)
    _make_valid_provider(ctx)
    harness = LaunchdHarness(ctx.executor)
    harness.loaded = True
    harness.running = True
    called: list[bool] = []

    def gpt(_layout, *, say):  # pragma: no cover - must never be called
        called.append(True)
        return "US fast"

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        gpt_runner=gpt,
    )

    assert report.status == "OK"
    assert report.data["already_in_charge"] is True
    assert called == []


# ---- deadlines ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 10.0), ("10000", 900.0), ("30", 30.0), ("abc", 180.0), ("", 180.0)],
)
def test_deadline_seconds_is_clamped(raw, expected):
    environ = {} if raw == "" else {boot.CONSOLE_DEADLINE_ENV: raw}
    assert boot.deadline_seconds(environ) == expected


def test_clamp_deadline_matches_the_env_var_range():
    assert boot.clamp_deadline(None) is None
    assert boot.clamp_deadline(-5.0) == 10.0
    assert boot.clamp_deadline(100000.0) == 900.0
    assert boot.clamp_deadline(42.0) == 42.0


def test_main_clamps_the_console_deadline_flag(tmp_path):
    """``--console-deadline 3`` waits 10 s, not 3 s (same clamp as the env)."""
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor.set("stat -f", (0, "root 0\n", ""))
    ctx.executor.set("route -n get default", (0, "interface: en0\n", ""))
    errors = io.StringIO()

    code = boot.main(
        ["--console-deadline", "3"],
        stdout=io.StringIO(),
        stderr=errors,
        context_factory=lambda: ctx,
    )

    assert code == int(ExitCode.NOT_READY)
    assert "deadline 10s" in errors.getvalue(), errors.getvalue()


def test_main_clamps_the_network_deadline_flag(tmp_path):
    """``--network-deadline 2000`` waits 900 s, not 2000 s."""
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor.set("stat -f", (0, "tester 501\n", ""))
    ctx.executor.set("route -n get default", (1, "", "no route"))
    errors = io.StringIO()

    code = boot.main(
        ["--network-deadline", "2000"],
        stdout=io.StringIO(),
        stderr=errors,
        context_factory=lambda: ctx,
    )

    assert code == int(ExitCode.NOT_READY)
    assert "deadline 900s" in errors.getvalue(), errors.getvalue()


def test_main_reports_a_missing_console_user(tmp_path):
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor.set("stat -f", (0, "root 0\n", ""))
    ctx.executor.set("route -n get default", (0, "interface: en0\n", ""))

    code = boot.main(
        [],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        context_factory=lambda: ctx,
    )

    assert code == int(ExitCode.NOT_READY)
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert not any("--nc start" in event for event in ctx.executor.events)


# ---- install / start / uninstall -----------------------------------------


def _installed_no_start(tmp_path):
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
    return ctx, harness


def test_install_no_start_registers_the_boot_entry_and_keeps_the_core_disabled(tmp_path):
    ctx, harness = _installed_no_start(tmp_path)

    assert harness.disabled is True, "the core must stay off at boot"
    assert harness.boot_disabled is False, "the boot entry must be enabled"
    assert harness.boot_loaded is True, (
        "install registers the boot entry in the current launchd domain, so a "
        "later `launchctl kickstart` finds it without a reboot"
    )
    assert harness.loaded is False, "the core itself is never loaded by install"
    assert ctx.executor.count("launchctl", "kickstart") == 0, (
        "install must not run the switch while the operator watches"
    )
    assert not any(
        call.argv[:3] == ["scutil", "--nc", "stop"] for call in ctx.executor.calls
    ), "install must not stop Nextin"
    assert not any(
        call.argv[:3] == ["scutil", "--nc", "start"] for call in ctx.executor.calls
    ), "install must not start Nextin"
    assert not os.path.isfile(ctx.layout.boot_hold), (
        "the hold stamp must not outlive the install"
    )
    assert os.path.isfile(ctx.layout.boot_plist)
    assert ctx.executor.find("launchctl", "enable", f"system/{BOOT_LABEL}") is not None


def test_install_bootstraps_the_boot_entry_under_the_hold_stamp(tmp_path):
    """RunAtLoad starts the entry right after bootstrap: the stamp must be there.

    The whole point of the stamp is that the bootstrap-triggered entry run
    exits with zero side effects, so the stamp has to exist (0600) at the
    moment launchd loads the job and must be gone when install ends.
    """
    ctx = make_context(tmp_path)
    harness = LaunchdHarness(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    observed: list[tuple[bool, int | None]] = []
    original = harness._bootstrap

    def spy_bootstrap(argv):
        if _target_label(argv) == BOOT_LABEL:
            path = ctx.layout.boot_hold
            exists = os.path.isfile(path)
            mode = stat.S_IMODE(os.stat(path).st_mode) if exists else None
            observed.append((exists, mode))
        return original(argv)

    ctx.executor.set("launchctl bootstrap", spy_bootstrap)

    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert observed, "install must bootstrap the boot entry"
    assert all(exists for exists, _mode in observed), (
        "the hold stamp must exist while launchd starts the entry"
    )
    if os.name != "nt":
        assert all(mode == 0o600 for _exists, mode in observed)
    assert not os.path.isfile(ctx.layout.boot_hold), (
        "the next entry run (boot or kickstart) must perform the switch again"
    )


def test_install_waits_for_the_boot_entry_to_acknowledge_the_hold(tmp_path):
    """The async race Codex review boot3 flagged: loaded != "the entry ran".

    ``bootstrap`` only registers the job; the RunAtLoad process starts a
    moment later and needs time to reach the stamp check.  install must keep
    the stamp until that process has read it, written the ack and exited -
    only then may the stamp (and the ack) go.
    """
    ctx = make_context(tmp_path)
    harness = LaunchdHarness(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    seen = {"polls": 0, "stamp_polls": 0, "ack_poll": None}

    original = harness._print

    def spy_print(argv):
        answer = original(argv)
        if _target_label(argv) == BOOT_LABEL:
            if os.path.isfile(ctx.layout.boot_hold_ack):
                seen["ack_poll"] = seen["polls"]
            elif os.path.isfile(ctx.layout.boot_hold):
                seen["stamp_polls"] += 1
            seen["polls"] += 1
            if seen["polls"] >= 4:
                # the process finally reaches the stamp check
                harness.boot_entry_delay = 0
        return answer

    # bootstrap registered the job, but the entry process has not reached the
    # stamp check yet
    harness.boot_entry_delay = 10_000
    ctx.executor.set("launchctl print", spy_print)

    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert seen["ack_poll"] is not None, "install completed before the entry acked"
    assert seen["stamp_polls"] >= 3, (
        "the stamp must survive every poll until the entry has acknowledged it"
    )
    assert harness.boot_entry_runs >= 1
    assert not os.path.isfile(ctx.layout.boot_hold)
    assert not os.path.isfile(ctx.layout.boot_hold_ack)


def test_install_fails_closed_when_the_entry_never_acknowledges(tmp_path):
    """No ack before the deadline: bootout + cleanup, no kickstartable leftover."""
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    ctx.environ[lifecycle.BOOT_ACK_DEADLINE_ENV] = "5"
    # the RunAtLoad process is up but never reaches the stamp check
    harness.boot_entry_delay = 10**9

    with pytest.raises(FailClosed):
        lifecycle.install(ctx, quiet=True, skip_start=True)

    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert harness.boot_loaded is False, (
        "install must not leave the job registered: a later kickstart would run "
        "the switch with no hold stamp"
    )
    assert harness.boot_running is False
    assert not os.path.isfile(ctx.layout.boot_hold), (
        "a leftover stamp would turn every future boot entry into a no-op forever"
    )
    assert not os.path.isfile(ctx.layout.boot_hold_ack)


def test_install_keeps_the_hold_when_the_timeout_bootout_fails(tmp_path):
    """Codex review boot4: an ack timeout with a failing bootout keeps the stamp.

    The RunAtLoad entry is up but has not reached the stamp check yet.  If
    the emergency bootout fails and the ``finally`` removed the stamp anyway,
    that entry would perform the whole network switch behind a failed
    install.  Fail closed instead: keep the hold stamp (and the ack) so the
    entry exits with zero side effects once it reaches the stamp check.
    """
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    ctx.environ[lifecycle.BOOT_ACK_DEADLINE_ENV] = "5"
    # the RunAtLoad process is up but never reaches the stamp check
    harness.boot_entry_delay = 10**9

    def failing_bootout(argv):
        return (1, "", "launchctl bootout failed: 5: Input/output error")

    ctx.executor.set("launchctl bootout", failing_bootout)

    with pytest.raises(FailClosed) as excinfo:
        lifecycle.install(ctx, quiet=True, skip_start=True)

    message = str(excinfo.value)
    assert "did not acknowledge the hold stamp" in message
    assert "bootout failed" in message
    assert "KEPT on disk" in message, message
    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert harness.boot_loaded is True, (
        "a failed bootout must not be treated as a confirmed unload"
    )
    assert harness.boot_running is True
    assert harness.boot_entry_runs == 0, (
        "the still-starting entry must never get past the stamp check"
    )
    assert os.path.isfile(ctx.layout.boot_hold), (
        "the stamp must be kept so the entry exits without switching the network"
    )


def test_install_keeps_the_hold_when_the_bootout_does_not_confirm_the_unload(tmp_path):
    """A bootout exit code alone is not proof: the unload must be read back.

    ``launchctl print`` still reporting the job loaded means the job may not
    be gone, so the stamp must stay until the print shows not loaded /
    "service not found".
    """
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    ctx.environ[lifecycle.BOOT_ACK_DEADLINE_ENV] = "5"
    harness.boot_entry_delay = 10**9

    def lying_bootout(argv):
        # reports success but the job stays registered in the harness
        return (0, "", "")

    ctx.executor.set("launchctl bootout", lying_bootout)

    with pytest.raises(FailClosed) as excinfo:
        lifecycle.install(ctx, quiet=True, skip_start=True)

    message = str(excinfo.value)
    assert "still reports loaded" in message
    assert "KEPT on disk" in message, message
    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert harness.boot_loaded is True
    assert harness.boot_entry_runs == 0
    assert os.path.isfile(ctx.layout.boot_hold), (
        "without a confirmed unload the stamp must stay"
    )


def test_install_keeps_the_hold_when_the_unload_cannot_be_read_back(tmp_path):
    """Codex review boot5: a print that fails after the bootout keeps the stamp.

    The emergency bootout returned success, but the follow-up
    ``launchctl print`` raises.  The unload is then *not* confirmed; reading
    it as "safe to clean up" (the old ``finally`` did) would remove the hold
    while the job may still be loaded and an entry may still be starting.
    Fail closed instead: keep the hold (and the ack).
    """
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    ctx.environ[lifecycle.BOOT_ACK_DEADLINE_ENV] = "5"
    # the RunAtLoad process is up but never reaches the stamp check
    harness.boot_entry_delay = 10**9

    state = {"booted_out": False}
    real_print = harness._print
    real_bootout = harness._bootout

    def flaky_print(argv):
        if state["booted_out"] and _target_label(argv) == BOOT_LABEL:
            raise OSError("launchctl print: could not talk to launchd")
        return real_print(argv)

    def spying_bootout(argv):
        result = real_bootout(argv)
        state["booted_out"] = True
        return result

    ctx.executor.set("launchctl print", flaky_print)
    ctx.executor.set("launchctl bootout", spying_bootout)

    with pytest.raises(FailClosed) as excinfo:
        lifecycle.install(ctx, quiet=True, skip_start=True)

    message = str(excinfo.value)
    assert "did not acknowledge the hold stamp" in message
    assert "the unload could not be confirmed" in message
    assert "KEPT on disk" in message, message
    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert os.path.isfile(ctx.layout.boot_hold), (
        "an unreadable read-back is not a confirmed unload: the stamp must stay"
    )


def test_install_does_not_trust_a_stale_ack(tmp_path):
    """An ack left by an interrupted earlier install must not end this wait."""
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    ctx.environ[lifecycle.BOOT_ACK_DEADLINE_ENV] = "5"
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    boot.write_hold_ack(ctx.layout)  # stale ack from an earlier install
    # this install's entry exits without ever acknowledging the new stamp
    harness.boot_entry_acks = False

    with pytest.raises(FailClosed):
        lifecycle.install(ctx, quiet=True, skip_start=True)

    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert not os.path.isfile(ctx.layout.boot_hold_ack)


def test_install_replaces_an_already_registered_boot_entry(tmp_path):
    """A re-install boots out the stale definition before bootstrap."""
    ctx, harness = _installed_no_start(tmp_path)
    harness.boot_loaded = True  # e.g. registered by a previous install or boot
    ctx.executor.calls.clear()

    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert harness.boot_loaded is True, "the new definition is registered again"
    commands = [" ".join(call.argv) for call in ctx.executor.calls]
    bootout_at = next(
        index
        for index, text in enumerate(commands)
        if text.startswith("launchctl bootout system/" + BOOT_LABEL)
    )
    bootstrap_at = next(
        index
        for index, text in enumerate(commands)
        if text.startswith("launchctl bootstrap system")
    )
    assert bootout_at < bootstrap_at, (
        "the stale definition must be removed before the new one loads"
    )
    assert not os.path.isfile(ctx.layout.boot_hold)


def test_reinstall_does_not_mistake_the_old_entry_ack_for_the_new_run(tmp_path):
    """Codex review boot6: a stale ack must not satisfy the new definition's wait.

    The old RunAtLoad entry reaches the stamp check while this install still
    holds the stamp, writes its ack, and only afterwards is booted out - so
    the stale ack survives its own bootout.  When the new definition is
    registered, launchd reports "loaded, not running" before the RunAtLoad
    process has spawned.  The wait must not read the stale ack as this run's
    confirmation: the new entry must spawn while the hold is still in place
    and write its own ack before the stamp is removed, or the hold-less
    window would let that run perform the whole switch.
    """
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    ctx.environ[lifecycle.BOOT_ACK_DEADLINE_ENV] = "5"
    os.makedirs(ctx.layout.state_dir, exist_ok=True)

    # Old job loaded; its entry is up and reaches the stamp check at the
    # first boot ``launchctl print`` - the one install itself makes to
    # decide whether a bootout is needed - so it writes an ack under the
    # hold before the bootout ends it.
    harness.boot_loaded = True
    harness.boot_running = True

    spawn = {"prints_left": None, "ran": False, "hold_at_spawn": None}
    real_print = harness._print
    real_bootstrap = harness._bootstrap

    def late_spawn_print(argv):
        result = real_print(argv)
        if spawn["prints_left"] is not None and _target_label(argv) == BOOT_LABEL:
            if spawn["prints_left"] > 0:
                # launchd has registered the job but has not spawned the
                # RunAtLoad process yet: the observation above already
                # reported "loaded, not running".
                spawn["prints_left"] -= 1
            else:
                # the process spawns now and runs to completion
                spawn["prints_left"] = None
                spawn["ran"] = True
                spawn["hold_at_spawn"] = os.path.isfile(ctx.layout.boot_hold)
                harness._run_boot_entry()  # acks iff the hold exists, then exits
        return result

    def late_spawn_bootstrap(argv):
        result = real_bootstrap(argv)
        if _target_label(argv) == BOOT_LABEL:
            harness.boot_running = False  # registered, but not spawned yet
            spawn["prints_left"] = 1  # spawns at the second observation
        return result

    ctx.executor.set("launchctl print", late_spawn_print)
    ctx.executor.set("launchctl bootstrap", late_spawn_bootstrap)

    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert spawn["ran"], (
        "the new entry must spawn during the wait - the stale ack the old "
        "entry wrote before its bootout must not end the wait early"
    )
    assert spawn["hold_at_spawn"], (
        "the new entry must spawn while the hold stamp is still in place; "
        "an early wait satisfied by the stale ack would have removed the "
        "hold and opened an unprotected switch window"
    )
    assert not os.path.isfile(ctx.layout.boot_hold)
    assert not os.path.isfile(ctx.layout.boot_hold_ack)


def test_reinstall_takes_over_a_kept_hold_and_never_opens_a_hold_less_window(tmp_path):
    """Codex review boot5: a kept hold is taken over, never deleted-then-recreated.

    An earlier install timed out and could not confirm the bootout, so it
    kept the stamp; the old job is still loaded and its RunAtLoad entry is
    still starting.  Deleting the stamp before creating a new one opens a
    hold-less window in which that entry performs the whole switch.  Every
    ``launchctl`` call that targets the boot-switch job must therefore observe
    the *original* stamp file still in place (calls on other jobs - the core
    disable, the Feishu sync registration - cannot trigger the boot entry and
    are outside this invariant).
    """
    ctx = make_context(tmp_path)
    harness = LaunchdHarness.attached(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    ctx.environ[lifecycle.BOOT_ACK_DEADLINE_ENV] = "5"
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    descriptor = os.open(
        ctx.layout.boot_hold, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    os.close(descriptor)
    original = os.stat(ctx.layout.boot_hold)
    # the old job is still loaded, its RunAtLoad entry is up but has not
    # reached the stamp check yet
    harness.boot_loaded = True
    harness.boot_running = True

    problems: list[str] = []
    real_run = ctx.executor.run

    def checked_run(argv, **kwargs):
        if argv and argv[0] == "launchctl" and BOOT_LABEL in " ".join(argv):
            if not os.path.isfile(ctx.layout.boot_hold):
                problems.append("launchctl ran without the hold: " + " ".join(argv))
            else:
                current = os.stat(ctx.layout.boot_hold)
                if (current.st_dev, current.st_ino) != (
                    original.st_dev,
                    original.st_ino,
                ):
                    problems.append(
                        "the kept hold was deleted and recreated before: "
                        + " ".join(argv)
                    )
        return real_run(argv, **kwargs)

    ctx.executor.run = checked_run  # type: ignore[method-assign]

    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert not problems, problems
    assert harness.boot_entry_runs >= 1, (
        "the entry ran under the hold and acknowledged it"
    )
    assert not os.path.isfile(ctx.layout.boot_hold), (
        "after the acknowledged run the stamp goes away as usual"
    )


def test_boot_hold_stamp_makes_the_entry_exit_without_side_effects(tmp_path):
    """A boot entry started under the hold stamp acks, does nothing, succeeds."""
    ctx = _root_ctx(tmp_path, connected=True)
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    descriptor = os.open(
        ctx.layout.boot_hold, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    os.close(descriptor)

    report = boot.boot_switch(
        ctx, console_reader=_console_ready, route_reader=_route_ready
    )

    assert report.status == "OK"
    assert report.data["boot_hold"] is True
    assert report.data["boot_hold_ack"] is True
    assert os.path.isfile(ctx.layout.boot_hold_ack), (
        "the entry must ack the hold so install can tell this run happened"
    )
    assert_posix_mode(ctx.layout.boot_hold_ack, 0o600)
    with open(ctx.layout.boot_hold_ack, "r", encoding="utf-8") as handle:
        assert handle.read() == boot.BOOT_HOLD_ACK_TEXT
    assert not ctx.executor.calls, "the hold exit must run zero commands"
    assert ctx.executor.connected is True


def test_a_kickstart_after_install_runs_the_full_switch(tmp_path, monkeypatch):
    """After install the job is loaded: a manual kickstart performs the switch."""
    ctx = _root_ctx(tmp_path, connected=False)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
    assert ctx.executor.find("launchctl", "bootstrap") is not None
    assert not os.path.isfile(ctx.layout.boot_hold)

    monkeypatch.setattr(switch.lifecycle, "start", _start_ok)
    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=True),
    )

    assert report.status == "OK", report.warnings
    assert any("--nc stop" in event for event in ctx.executor.events)
    assert report.data["stop_wait"]["ok"] is True
    assert not os.path.isfile(ctx.layout.boot_hold)


def test_boot_switch_runs_under_the_launchd_path_with_an_installed_binary(tmp_path):
    """Regression (2026-11-15 kickstart): minimal PATH + installed executable.

    The boot entry's facts must register the installed absolute path exactly
    like the CLI does, so preflight no longer refuses with "required command
    not found: mihomo" and the whole switch runs.
    """
    ctx = _root_ctx(tmp_path, connected=True)
    prepare_install(ctx)
    ctx.facts = context.register_binary(launchd_facts(), ctx.layout)
    ctx.controller_factory = lambda _secret: FakeController()
    observations = iter(
        [
            lifecycle.TunObservation(("en0",), "en0"),
            lifecycle.TunObservation(("en0", "utun7"), "utun7"),
        ]
    )
    ctx.tun_observer = lambda: next(observations)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    # A fresh boot-entry process (the kickstart) rebuilds its facts: now the
    # installed binary exists and wins over the install-source override.
    ctx.facts = context.register_binary(launchd_facts(), ctx.layout)
    assert ctx.facts.commands["mihomo"] == ctx.layout.binary
    assert preflight.evaluate(ctx.facts).supported is True

    report = boot.boot_switch(
        ctx,
        console_reader=_console_ready,
        route_reader=_route_ready,
        probe_runner=lambda: probe.ProbeReport(ok=True),
    )

    assert report.status == "OK", report.warnings
    assert not any("mihomo binary not available" in warning for warning in report.warnings)
    harness = LaunchdHarness.attached(ctx.executor)
    assert harness.running is True
    assert harness.disabled is True
    assert ctx.executor.connected is False


def test_boot_without_a_usable_binary_rolls_back_to_nextin(tmp_path):
    """Nothing installed and no override: fail closed, Nextin stays in charge."""
    ctx = _root_ctx(tmp_path, connected=True)
    # A stale PATH entry must not satisfy the runtime check.
    ctx.facts = context.register_binary(darwin_facts(), ctx.layout)
    assert "mihomo" not in ctx.facts.commands

    report = boot.boot_switch(
        ctx, console_reader=_console_ready, route_reader=_route_ready
    )

    assert report.status == "FAILED"
    assert report.exit_code == int(ExitCode.UNSUPPORTED)
    assert any(preflight.MIHOMO_MISSING_REASON in warning for warning in report.warnings)
    harness = LaunchdHarness.attached(ctx.executor)
    assert harness.loaded is False
    assert harness.disabled is True
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert ctx.executor.connected is True, "rollback must end with Nextin connected"


def test_the_installed_boot_plist_runs_the_boot_module(tmp_path):
    ctx, _harness = _installed_no_start(tmp_path)
    with open(ctx.layout.boot_plist, "rb") as handle:
        document = plistlib.load(handle)

    assert document["Label"] == BOOT_LABEL
    assert document["RunAtLoad"] is True
    assert "KeepAlive" not in document
    assert "mpm_macos.boot" in document["ProgramArguments"]
    assert document["StandardOutPath"].endswith("boot-switch.out.log")
    assert document["StandardErrorPath"].endswith("boot-switch.err.log")


def test_start_keeps_the_core_disabled_and_the_boot_entry_enabled(tmp_path):
    ctx, harness = _installed_no_start(tmp_path)
    observations = iter(
        [
            lifecycle.TunObservation(("en0",), "en0"),
            lifecycle.TunObservation(("en0", "utun7"), "utun7"),
        ]
    )
    ctx.tun_observer = lambda: next(observations)

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "OK", report.warnings
    assert harness.running is True
    assert harness.disabled is True
    assert harness.boot_disabled is False


def test_uninstall_boots_out_and_disables_the_boot_entry(tmp_path):
    ctx, harness = _installed_no_start(tmp_path)
    harness.boot_loaded = True
    harness.boot_running = True

    report = lifecycle.uninstall(ctx, quiet=True)

    assert report.status == "OK"
    assert harness.boot_disabled is True
    assert harness.boot_loaded is False
    assert not os.path.exists(ctx.layout.boot_plist)
    assert ctx.executor.find("launchctl", "bootout", f"system/{BOOT_LABEL}") is not None
    assert ctx.executor.find("launchctl", "disable", f"system/{BOOT_LABEL}") is not None


# ---- boot plist audit -----------------------------------------------------

BOOT_RENDER_ARGS = {
    "python": "/usr/bin/python3",
    "package_dir": "/usr/local/share/mihomo-proxy-management/python",
    "home_dir": "/Library/Application Support/mihomo-proxy-management",
    "config": "/Library/Application Support/mihomo-proxy-management/config.yaml",
    "binary": "/usr/local/libexec/mihomo-proxy-management/current/mihomo",
    "run_dir": "/var/run/mihomo-proxy-management",
    "log_out": "/Library/Application Support/mihomo-proxy-management/logs/boot-switch.out.log",
    "log_err": "/Library/Application Support/mihomo-proxy-management/logs/boot-switch.err.log",
}


def _rendered_boot() -> str:
    return plist.render_from_file(
        str(BOOT_PLIST_TEMPLATE), label=BOOT_LABEL, **BOOT_RENDER_ARGS
    )


def test_boot_plist_has_no_keepalive_and_no_forbidden_token():
    text = _rendered_boot()
    document = plistlib.loads(text.encode("utf-8"))

    assert document["Label"] == BOOT_LABEL
    assert document["RunAtLoad"] is True
    assert "KeepAlive" not in document, "a failing boot switch must not be retried"
    arguments = document["ProgramArguments"]
    assert arguments[0] == "/usr/bin/python3"
    assert "-m" in arguments
    assert "mpm_macos.boot" in arguments
    for token in ("sh", "bash", "zsh", "-c", "caffeinate"):
        assert token not in arguments

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, str):
            yield value

    values = " ".join(walk(document)).lower()
    for token in ("caffeinate", "://", "secret", "token", "password", "subscription"):
        assert token not in values
    assert "EnvironmentVariables" in document


def test_boot_plist_audit_does_not_flag_the_apple_dtd():
    """The Apple DTD URL lives in the DOCTYPE, not in a value: audit passes.

    ``audit_text`` scans the parsed document (values), not the raw XML, so the
    official ``http://www.apple.com/DTDs/...`` declaration is not a leak.
    """
    result = plist.audit_text(_rendered_boot(), label=BOOT_LABEL)
    assert result["label"] == BOOT_LABEL
    assert result["keep_alive"] is False
    assert "mpm_macos.boot" in result["program_arguments"]


def test_boot_plist_injects_the_absolute_installed_binary():
    document = plistlib.loads(_rendered_boot().encode("utf-8"))
    assert document["EnvironmentVariables"]["MPM_MIHOMO_BINARY"] == BOOT_RENDER_ARGS["binary"]


def test_boot_plist_audit_rejects_a_relative_binary_env_override():
    document = plistlib.loads(_rendered_boot().encode("utf-8"))
    document["EnvironmentVariables"]["MPM_MIHOMO_BINARY"] = "mihomo"
    with pytest.raises(FailClosed):
        plist.audit(document, label=BOOT_LABEL)


def test_audit_rejects_a_restart_policy_on_the_boot_entry():
    document = plistlib.loads(_rendered_boot().encode("utf-8"))

    document["KeepAlive"] = {"SuccessfulExit": False}
    with pytest.raises(FailClosed):
        plist.audit(document, label=BOOT_LABEL)

    document["KeepAlive"] = True
    with pytest.raises(FailClosed):
        plist.audit(document, label=BOOT_LABEL)

    document["KeepAlive"] = False
    assert plist.audit(document, label=BOOT_LABEL)["keep_alive"] is False


def test_audit_rejects_the_core_runner_in_the_boot_entry():
    document = plistlib.loads(_rendered_boot().encode("utf-8"))
    document["ProgramArguments"][2] = "mpm_macos.runner"

    with pytest.raises(FailClosed):
        plist.audit(document, label=BOOT_LABEL)


def test_the_boot_template_cannot_be_rendered_as_the_core_job():
    with pytest.raises(FailClosed):
        plist.render_from_file(
            str(BOOT_PLIST_TEMPLATE), label="com.mihomo-proxy-management.core", **BOOT_RENDER_ARGS
        )
