"""``mpm-switch``: both directions end with a working machine (host-free)."""

from __future__ import annotations

import inspect
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from mpm_common.errors import ExitCode
from mpm_common.executor import Executor, FakeExecutor, Result
from mpm_macos import conflict as conflict_mod
from mpm_macos import lifecycle, probe, switch

from macos_test_support import (
    FakeNetworkSetup,
    LaunchdHarness,
    make_context,
    prepare_install,
)

pytestmark = pytest.mark.macos

VPN = "Nextin"


class VpnExecutor(FakeExecutor):
    """Stateful ``scutil --nc`` / ``launchctl asuser`` / port fake.

    The VPN state is shared between the user session and the ``asuser`` view
    (there is only one VPN); the dedicated root-blind tests below override
    what a plain root ``scutil --nc list`` may see.

    ``on_demand`` drives ``scutil --nc show``: ``False``/``True`` render the
    flag, ``None`` makes the command fail with scutil's "No service", and
    ``"invalid"`` renders the real macOS output for a profile whose
    NetworkExtension is gone (``(Invalid)`` + ``No "VPN" configuration
    available``) with a ``Disconnected`` status.
    ``reconnect_polls`` makes the VPN come back by itself after that many
    ``--nc list`` polls following a stop (Nextin's Always On VPN behaviour).
    ``status_provider`` / ``status_states`` drive ``scutil --nc status``: with
    neither set the command answers nothing and the list's state field is used.
    """

    def __init__(
        self,
        *,
        connected: bool = False,
        stubborn: bool = False,
        listeners: tuple[int, ...] = (),
        on_demand: bool | None | str = False,
        reconnect_polls: int | None = None,
        status_provider=None,
        status_states=None,
    ) -> None:
        super().__init__()
        self.connected = connected
        self.stubborn = stubborn
        self.listeners = tuple(listeners)
        self.on_demand = on_demand
        self.reconnect_polls = reconnect_polls
        self.status_provider = status_provider
        self.status_states = list(status_states or [])
        self.start_calls = 0
        #: service name the fake reports (``MPM_VPN_NAME`` is per-context)
        self.vpn_name = VPN
        self.events: list[str] = []
        self._stopped = False
        self._polls_after_stop = 0
        self.set("route -n get default", (0, "interface: en0\n", ""))
        self.set("ifconfig", (0, "", ""))
        self.set("lsof -nP", self._lsof)

    def _result(self, argv, text=""):
        return Result(tuple(argv), 0, text, "")

    def _lsof(self, argv):
        port = int(argv[2].split(":", 1)[1])
        if port in self.listeners:
            return (0, "p4242\ncmihomo\n", "")
        return (1, "", "")

    def _status(self, argv):
        if self.on_demand == "invalid":
            return self._result(argv, "Disconnected\n")
        if self.status_provider is not None:
            return self._result(argv, self.status_provider() + "\n")
        if self.status_states:
            state = (
                self.status_states.pop(0)
                if len(self.status_states) > 1
                else self.status_states[0]
            )
            return self._result(argv, state + "\n")
        # nothing to say: the caller falls back to the list's state field
        return self._result(argv)

    def _nc_list_text(self) -> str:
        if self.connected:
            state = "Connected"
        elif self.on_demand == "invalid":
            state = "Invalid"
        else:
            state = "Disconnected"
        return (
            "Available network connection services in the current set:\n"
            f'* ({state})    "{self.vpn_name}" [VPN:com.Tommy.Nextin]\n'
        )

    def _on_demand_text(self) -> str:
        value = "TRUE" if self.on_demand else "FALSE"
        return (
            "SCNetworkReachability information:\n"
            f'  "{self.vpn_name}" [VPN:com.Tommy.Nextin]\n'
            f"  OnDemandEnabled : {value}\n"
            "  OnDemandRules : [ {Action: Connect} ]\n"
        )

    def _show(self, argv):
        if self.on_demand is None:
            return Result(tuple(argv), 1, "", "No service\n")
        if self.on_demand == "invalid":
            return self._result(
                argv,
                '  (Invalid)        8B3C0C1A-4472-448A-95B6-5E0D9056D019 VPN '
                '(com.Tommy.Nextin) "Nextin"  [VPN:com.Tommy.Nextin]\n'
                'No "VPN" configuration available\n',
            )
        return self._result(argv, self._on_demand_text())

    def _list(self, argv):
        if self._stopped and self.reconnect_polls is not None and not self.connected:
            self._polls_after_stop += 1
            if self._polls_after_stop > self.reconnect_polls:
                self.connected = True
        return self._result(argv, self._nc_list_text())

    def _apply(self, argv) -> None:
        if "stop" in argv:
            if not self.stubborn:
                self.connected = False
                self._stopped = True
                self._polls_after_stop = 0
        elif "start" in argv:
            self.start_calls += 1
            self.connected = True

    def run(self, argv, **kwargs):
        if argv[:2] == ["scutil", "--nc"]:
            action = argv[2] if len(argv) > 2 else ""
            self.events.append(" ".join(argv))
            if action == "list":
                return self._list(argv)
            if action == "show":
                return self._show(argv)
            if action == "status":
                return self._status(argv)
            self._apply(argv)
            return self._result(argv)
        if argv[:2] == ["launchctl", "asuser"]:
            self.events.append(" ".join(argv))
            if argv[6:8] == ["scutil", "--nc"] and len(argv) > 8:
                action = argv[8]
                if action == "list":
                    return self._list(argv)
                if action == "show":
                    return self._show(argv)
                if action == "status":
                    return self._status(argv)
            self._apply(argv)
            return self._result(argv)
        return super().run(argv, **kwargs)


class RootBlindExecutor(VpnExecutor):
    """A root process cannot see the user session: plain list is always empty."""

    def run(self, argv, **kwargs):
        if argv[:3] == ["scutil", "--nc", "list"]:
            self.events.append(" ".join(argv))
            return self._result(
                argv, "Available network connection services in the current set:\n"
            )
        return super().run(argv, **kwargs)


def _ctx(
    tmp_path,
    *,
    connected=False,
    stubborn=False,
    euid=501,
    listeners=(),
    on_demand=False,
    reconnect_polls=None,
    status_provider=None,
    status_states=None,
):
    executor = VpnExecutor(
        connected=connected,
        stubborn=stubborn,
        listeners=listeners,
        on_demand=on_demand,
        reconnect_polls=reconnect_polls,
        status_provider=status_provider,
        status_states=status_states,
    )
    ctx = make_context(tmp_path, executor=executor, geteuid=lambda: euid)
    ctx.environ["MPM_VPN_NAME"] = VPN
    return ctx


def _root_ctx(tmp_path, **kwargs):
    ctx = _ctx(tmp_path, euid=0, **kwargs)
    LaunchdHarness.attached(ctx.executor)
    return ctx


def _marker_writer(marker: str, calls: list[str] | None = None):
    def runner(command: str) -> int:
        if calls is not None:
            calls.append(command)
        Path(marker).write_text("ok\n", encoding="utf-8")
        return 0

    return runner


#: Commands that change state.  A read-only report may run none of them.
_WRITE_COMMAND_PREFIXES = (
    "scutil --nc start",
    "scutil --nc stop",
    "launchctl bootstrap",
    "launchctl bootout",
    "launchctl kickstart",
    "launchctl enable",
    "launchctl disable",
)


def _all_commands(executor) -> list[str]:
    """Everything the executor ran, from ``calls`` and from ``events``.

    ``VpnExecutor`` records ``scutil``/``launchctl asuser`` in ``events``
    instead of ``calls``, so both have to be inspected.
    """
    commands = [" ".join(call.argv) for call in executor.calls]
    commands.extend(executor.events)
    return commands


def _write_commands(executor) -> list[str]:
    """Commands that start/stop a VPN, (un)load a job or change the proxy."""
    writes: list[str] = []
    for command in _all_commands(executor):
        parts = command.split()
        if parts[:2] == ["launchctl", "asuser"] and len(parts) > 6:
            parts = parts[6:]  # drop ``asuser <uid> sudo -u <user>``
            command = " ".join(parts)
        if any(command.startswith(prefix) for prefix in _WRITE_COMMAND_PREFIXES):
            writes.append(command)
        elif parts[:1] == ["networksetup"] and len(parts) > 1 and parts[1].startswith("-set"):
            writes.append(command)
    return writes


# ---- user phase: password first ------------------------------------------


def test_switch_mihomo_password_first_does_not_stop_nextin(tmp_path):
    """The user phase only opens the dialog: Nextin is left untouched."""
    ctx = _ctx(tmp_path, connected=True)
    marker = str(tmp_path / "marker")
    commands: list[str] = []

    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "OK"
    assert ctx.executor.connected is True, "the user phase must not touch Nextin"
    assert not any("--nc stop" in event for event in ctx.executor.events)
    assert len(commands) == 1
    # the wrapper carries every explicit value and the fail-safe rollback
    assert "switch mihomo" in commands[0]
    assert "--uid 501" in commands[0]
    assert "--user tester" in commands[0]
    assert f"--vpn-name {shlex.quote(VPN)}" in commands[0]
    assert f"--marker {shlex.quote(marker)}" in commands[0]
    assert "switch rollback" in commands[0]
    assert "[ -f " + shlex.quote(marker) + " ] ||" in commands[0]


def test_password_cancel_never_touches_nextin(tmp_path):
    """A cancelled dialog returns non-zero and Nextin was never disconnected."""
    ctx = _ctx(tmp_path, connected=True)
    marker = str(tmp_path / "marker")

    report = switch.switch_to_mihomo(
        ctx,
        privileged=lambda command: 1,
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert ctx.executor.connected is True
    assert not any("--nc stop" in event for event in ctx.executor.events)
    assert not any("--nc start" in event for event in ctx.executor.events)


def test_user_phase_osascript_exception_reconnects_nextin_only_when_mihomo_down(tmp_path):
    """An exception is treated like a failed dialog, never like "all fine"."""

    def boom(command: str) -> int:
        raise RuntimeError("osascript exploded")

    down = _ctx(tmp_path / "down", connected=False)
    report = switch.switch_to_mihomo(
        down,
        privileged=boom,
        uid=501,
        user="tester",
        marker=str(tmp_path / "down.marker"),
    )
    assert report.status == "FAILED"
    assert down.executor.connected is True, "Nextin must come back when mihomo is down"

    up = _ctx(tmp_path / "up", connected=False, listeners=(7890,))
    report = switch.switch_to_mihomo(
        up,
        privileged=boom,
        uid=501,
        user="tester",
        marker=str(tmp_path / "up.marker"),
    )
    assert report.status == "FAILED"
    assert up.executor.connected is False, "never create a second stack"
    assert any("may still be running" in warning for warning in report.warnings)
    assert any("mpm-switch nextin" in warning for warning in report.warnings)


# ---- privileged hop: no timeout -------------------------------------------
#
# One ``osascript`` covers the password dialog *and* the whole root phase, so
# it runs with ``timeout=None``: the executor must never kill the authorized
# process.  A static sum of per-command deadlines cannot follow the real call
# chain, and a wrong sum kills the dialog and leaves an orphan root behind
# (2026-11-15 12:57).  Every other command keeps the executor's 60 s default.

UNSET = object()


class RecordingExecutor(FakeExecutor):
    """FakeExecutor that records each call's argv and its ``timeout`` argument.

    ``UNSET`` means the caller passed no ``timeout``, i.e. the executor's own
    default (60 s) applies; ``None`` means an explicit "no deadline".
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.recorded: list[tuple[list[str], object]] = []

    def run(self, argv, **kwargs):
        self.recorded.append((list(argv), kwargs.get("timeout", UNSET)))
        return super().run(argv, **kwargs)

    def timeout_for(self, *prefix_tokens: str) -> object:
        prefix = " ".join(prefix_tokens)
        for argv, timeout in self.recorded:
            joined = " ".join(argv)
            if joined == prefix or joined.startswith(prefix + " "):
                return timeout
        return UNSET


class TimeoutRecorder:
    """Delegating wrapper that records the ``timeout`` of every executor call."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.recorded: list[tuple[list[str], object]] = []

    def run(self, argv, **kwargs):
        self.recorded.append((list(argv), kwargs.get("timeout", UNSET)))
        return self.inner.run(argv, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class TimingOutExecutor(FakeExecutor):
    """Executor that raises ``subprocess.TimeoutExpired`` for ``osascript``."""

    def run(self, argv, **kwargs):
        if argv[:1] == ["osascript"]:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        return super().run(argv, **kwargs)


def test_run_privileged_never_gives_osascript_a_deadline():
    """The authorized process is never killed, so no orphan root can appear."""
    executor = RecordingExecutor()

    code = switch.run_privileged(executor, "/opt/mpm/bin/mpm-switch", "switch", "mihomo")

    assert code == 0
    assert executor.find("osascript") is not None
    assert executor.timeout_for("osascript") is None, "timeout=None: never killed"


def test_executor_default_deadline_is_unchanged():
    """Only the dialog is unbounded; a wedged local command still times out."""
    default = inspect.signature(Executor.run).parameters["timeout"].default
    assert default == 60.0

    executor = RecordingExecutor()
    switch.vpn_is_connected(executor, VPN)
    assert executor.timeout_for("scutil", "--nc", "list") is UNSET


def test_executor_forwards_no_deadline_to_subprocess(monkeypatch):
    """``timeout=None`` reaches ``subprocess.run`` unchanged ("no timeout")."""
    seen: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["timeout"] = kwargs.get("timeout", UNSET)
        return Completed()

    monkeypatch.setattr("mpm_common.executor.subprocess.run", fake_run)
    result = Executor().run(["osascript", "-e", ""], timeout=None)

    assert result.returncode == 0
    assert seen["timeout"] is None


def test_switch_flow_only_unbounds_the_dialog(tmp_path):
    """Every command the user phase runs keeps the executor's 60 s default."""
    ctx = _ctx(tmp_path, connected=True)
    recorder = TimeoutRecorder(ctx.executor)
    ctx.executor = recorder
    ctx.executor.set("osascript", (0, "", ""))
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")

    report = switch.switch_to_mihomo(
        ctx,
        uid=501,
        user="tester",
        marker=str(marker),
    )

    assert report.status == "OK"
    assert recorder.recorded, "the flow must actually run commands"
    for argv, timeout in recorder.recorded:
        if argv[:1] == ["osascript"]:
            assert timeout is None, argv
        else:
            assert timeout is UNSET, (argv, timeout)


def test_run_privileged_reports_a_timeout_defensively():
    """"Killed" is not "cancelled": the root child may have kept running."""
    reported = FakeExecutor()
    reported.set("osascript", (switch.TIMEOUT_EXIT_CODE, "timeout", "Timed out"))
    with pytest.raises(switch.PrivilegedTimeout):
        switch.run_privileged(reported, "/cli", "switch", "mihomo")

    raised = TimingOutExecutor()
    with pytest.raises(switch.PrivilegedTimeout):
        switch.run_privileged(raised, "/cli", "switch", "mihomo")


def test_user_phase_timeout_never_starts_nextin_while_mihomo_may_run(tmp_path):
    """A reported timeout may leave a running core behind: no second stack.

    The root phase can finish (marker written) *before* the timeout is
    reported, so the marker must not be read as the "refused, nothing
    happened" verdict.
    """
    ctx = _ctx(tmp_path, connected=False, listeners=(7890,))
    ctx.executor.set("osascript", (switch.TIMEOUT_EXIT_CODE, "timeout", "Timed out"))
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")

    report = switch.switch_to_mihomo(
        ctx,
        uid=501,
        user="tester",
        marker=str(marker),
    )

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert ctx.executor.connected is False, "never create a second stack"
    assert _write_commands(ctx.executor) == []
    assert any("特权阶段超时，状态未知" in warning for warning in report.warnings), report.warnings
    assert any("mpm-switch nextin" in warning for warning in report.warnings), report.warnings
    assert not any("refused" in warning for warning in report.warnings), report.warnings
    assert report.data["privileged_timeout"]["mihomo_may_run"] is True
    assert not marker.exists(), "the stale marker is removed"


def test_user_phase_timeout_with_loaded_launchd_job_does_not_start_nextin(tmp_path):
    """A loaded job may restart at any moment: that is "may be running" too."""
    ctx = _ctx(tmp_path, connected=False)
    harness = LaunchdHarness(ctx.executor)
    harness.loaded = True
    harness.running = False
    ctx.executor.set("osascript", (switch.TIMEOUT_EXIT_CODE, "timeout", "Timed out"))

    report = switch.switch_to_mihomo(
        ctx,
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )

    assert report.status == "FAILED"
    assert ctx.executor.connected is False
    assert _write_commands(ctx.executor) == []
    assert report.data["privileged_timeout"]["launchd_loaded"] is True
    assert any("may be running" in warning for warning in report.warnings)


def test_user_phase_timeout_writes_nothing_even_when_mihomo_looks_down(tmp_path):
    """Fail closed: a "looks down" snapshot is not proof the orphan has finished.

    The root child may still be stopping Nextin, waiting out the stable window
    or about to bootstrap mihomo, so the user phase must not start Nextin (nor
    stop anything) on a read-only guess - it reports and points at
    ``mpm-switch nextin``.
    """
    ctx = _ctx(tmp_path, connected=False)

    report = switch.switch_to_mihomo(
        ctx,
        privileged=lambda command: switch.TIMEOUT_EXIT_CODE,
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )

    assert report.status == "FAILED"
    assert report.exit_code == int(ExitCode.FAIL_CLOSED)
    assert ctx.executor.connected is False, "a timeout must not touch Nextin"
    assert _write_commands(ctx.executor) == [], _write_commands(ctx.executor)
    assert not any("已自动切回" in message for message in report.messages)
    assert any("特权阶段超时，状态未知" in warning for warning in report.warnings)
    assert any("mpm-switch nextin" in warning for warning in report.warnings)
    # read-only diagnostics are still reported
    data = report.data["privileged_timeout"]
    assert data["mihomo_may_run"] is False
    assert data["wrote_nothing"] is True
    assert data["nextin_connected"] is False


def test_user_phase_timeout_with_nextin_still_connected_writes_nothing(tmp_path):
    """An already connected Nextin is only *reported*, never re-started."""
    ctx = _ctx(tmp_path, connected=True)

    report = switch.switch_to_mihomo(
        ctx,
        privileged=lambda command: switch.TIMEOUT_EXIT_CODE,
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )

    assert report.status == "FAILED"
    assert ctx.executor.connected is True
    assert _write_commands(ctx.executor) == []
    assert report.data["privileged_timeout"]["nextin_connected"] is True


def test_vpn_name_passed_to_root_phase(tmp_path, monkeypatch):
    """``MPM_VPN_NAME`` reaches root through an explicit, quoted argument."""
    ctx = _ctx(tmp_path, connected=True)
    ctx.environ["MPM_VPN_NAME"] = "Corp VPN"
    marker = str(tmp_path / "marker")
    commands: list[str] = []

    switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )
    assert f"--vpn-name {shlex.quote('Corp VPN')}" in commands[0]

    root = _root_ctx(tmp_path / "root", connected=True)
    # the fake must report the configured service name: the stop wait looks the
    # name up in ``scutil --nc list`` and "unknown" is never "disconnected"
    root.executor.vpn_name = "Corp VPN"
    monkeypatch.setattr(
        switch.lifecycle,
        "start",
        lambda ctx_arg, quiet=False, **kwargs: lifecycle.Report(
            command="start", status="OK", data={"new_utun_interfaces": ["utun7"]}
        ),
    )
    report = switch.switch_to_mihomo_root(
        root,
        uid=501,
        user="tester",
        vpn_name="Corp VPN",
        probe_runner=lambda: probe.ProbeReport(ok=True),
    )

    assert report.status == "OK"
    assert any(
        "scutil --nc stop Corp VPN" in event and event.startswith("launchctl asuser 501")
        for event in root.executor.events
    )


# ---- root phase: every failure rolls back --------------------------------


def test_root_conflict_check_exception_rolls_back(tmp_path):
    """A broken detector must roll back instead of returning early."""
    ctx = _root_ctx(tmp_path, connected=False)

    def exploded():
        raise RuntimeError("scutil exploded")

    ctx.conflict_detector = exploded

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert ctx.executor.count("launchctl", "disable") >= 1
    assert ctx.executor.connected is True, "rollback must end with Nextin connected"
    assert any(
        "launchctl asuser 501 sudo -u tester scutil --nc start" in event
        for event in ctx.executor.events
    )


def test_root_phase_still_connected_aborts_before_start(tmp_path):
    ctx = _root_ctx(tmp_path, connected=True, stubborn=True)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert ctx.executor.connected is True


def test_root_phase_disconnects_nextin_as_the_user(tmp_path, monkeypatch):
    ctx = _root_ctx(tmp_path, connected=True)
    monkeypatch.setattr(
        switch.lifecycle,
        "start",
        lambda ctx_arg, quiet=False, **kwargs: lifecycle.Report(
            command="start", status="OK", data={"new_utun_interfaces": ["utun7"]}
        ),
    )

    report = switch.switch_to_mihomo_root(
        ctx, uid=501, user="tester", probe_runner=lambda: probe.ProbeReport(ok=True)
    )

    assert report.status == "OK"
    assert ctx.executor.connected is False
    assert any(
        event.startswith("launchctl asuser 501 sudo -u tester scutil --nc stop")
        for event in ctx.executor.events
    )


def test_root_phase_polls_vpn_state_via_asuser(tmp_path, monkeypatch):
    """Root-phase start/stop polling must read the user session, never root's list.

    ``RootBlindExecutor`` makes plain ``scutil --nc list`` (what root sees)
    permanently empty; only the ``launchctl asuser`` list carries the real
    state.  A stubborn VPN that root cannot disconnect must therefore keep
    reporting Connected and abort the switch instead of racing into a
    dual-stack start.
    """
    started: list[str] = []

    def fake_start(ctx_arg, quiet=False, **kwargs):
        started.append("start")
        return lifecycle.Report(
            command="start", status="OK", data={"new_utun_interfaces": ["utun7"]}
        )

    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    # Happy path: the asuser list reflects the disconnect and every list
    # query in the root phase goes through asuser.
    ctx = _root_ctx(tmp_path / "ok", connected=True)
    report = switch.switch_to_mihomo_root(
        ctx, uid=501, user="tester", probe_runner=lambda: probe.ProbeReport(ok=True)
    )
    assert report.status == "OK"
    assert ctx.executor.connected is False
    assert len(started) == 1, "the happy path starts mihomo exactly once"
    list_events = [event for event in ctx.executor.events if "--nc list" in event]
    assert list_events, "the root phase must poll the VPN state"
    assert all(
        event.startswith("launchctl asuser") for event in list_events
    ), "root's own scutil --nc list is blind and must not drive decisions"

    # Root-blind path: plain list says "nothing connected" while the user
    # session still shows Connected -> the switch must refuse to start mihomo.
    blind = _root_ctx(tmp_path / "blind", connected=True, stubborn=True)
    blind.executor = RootBlindExecutor(connected=True, stubborn=True)
    LaunchdHarness(blind.executor)

    report = switch.switch_to_mihomo_root(blind, uid=501, user="tester")

    assert report.status == "FAILED"
    assert len(started) == 1, "mihomo must not start while the asuser list shows Connected"
    assert blind.executor.count("launchctl", "bootstrap") == 0


def test_rollback_refuses_nextin_when_disable_readback_false(tmp_path):
    """``disable`` returning 0 is not enough: the override must read back true."""
    ctx = _root_ctx(tmp_path, connected=False)
    ctx.launchd().bootstrap()  # job loaded and running
    original = ctx.executor.run

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[:2] == ["launchctl", "print-disabled"]:
            # the command succeeds but reports no override: unknown, not "safe"
            return Result(tuple(argv), 0, "disabled services = {\n}\n", "")
        return result

    ctx.executor.run = run

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.connected is False
    assert not any("--nc start" in event for event in ctx.executor.events)
    assert any("mpm-switch nextin" in warning for warning in report.warnings)


def test_rollback_refuses_nextin_when_orphan_core_detected(tmp_path):
    """A leftover *project* mihomo on a project port blocks the Nextin connection.

    The listener is identified as ours by its executable path, not by its
    process name: identity comes from ``ps`` (see ``conflict.py``).
    """
    ctx = _root_ctx(tmp_path, connected=False, listeners=(7890,))
    ctx.executor.set("ps -o comm=", (0, conflict_mod.PROJECT_EXECUTABLE + "\n", ""))

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.connected is False
    assert not any("--nc start" in event for event in ctx.executor.events)
    assert any("mpm-switch nextin" in warning for warning in report.warnings)


def test_rollback_allows_nextin_when_proxytunnel_owns_7890(tmp_path):
    """Nextin's ``ProxyTunnel`` on 7890 is foreign, not a mihomo orphan.

    The stop is fully confirmed (launchd stopped, disable read back true);
    the port owner runs a non-project executable, so the rollback may start
    Nextin instead of leaving the user offline.
    """
    ctx = _root_ctx(tmp_path, connected=False)

    def proxy_lsof(argv):
        port = int(argv[2].split(":", 1)[1])
        if port == 7890:
            return (0, "p5001\ncProxyTunnel\n", "")
        return (1, "", "")

    ctx.executor.set("lsof -nP", proxy_lsof)
    ctx.executor.set(
        "ps -o comm=",
        (0, "/Applications/Nextin.app/Contents/MacOS/ProxyTunnel\n", ""),
    )
    ctx.executor.set("pgrep -f", (1, "", ""))  # no project core process

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "OK"
    assert ctx.executor.connected is True
    assert any("--nc start" in event for event in ctx.executor.events)


def test_existing_nextin_utun_not_orphan(tmp_path):
    """A 198.19/16 utun with no project core process is not a mihomo orphan."""
    ctx = _root_ctx(tmp_path, connected=False)
    ctx.executor.set(
        "ifconfig",
        (
            0,
            "utun9: flags=8051<UP,POINTOPOINT,RUNNING>\n"
            "\tinet 198.19.0.1 --> 198.19.0.1 netmask 0xffff0000\n",
            "",
        ),
    )
    ctx.executor.set("pgrep -f", (1, "", ""))  # no project mihomo process

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "OK"
    assert ctx.executor.connected is True
    # the utun identity was actually checked before it was dismissed
    assert ctx.executor.find("pgrep", "-f") is not None


def test_unknown_identity_with_confirmed_stop_allows_nextin_with_warning(tmp_path):
    """An unidentifiable listener does not block a fully confirmed stop.

    launchd stop confirmed + disable read back true + no project executable
    running: the leftover cannot be attributed to mihomo, so the rollback
    proceeds with a WARN instead of leaving the machine offline.
    """
    ctx = _root_ctx(tmp_path, connected=False)

    def unknown_lsof(argv):
        port = int(argv[2].split(":", 1)[1])
        if port == 7890:
            return (0, "p7777\ncunknown\n", "")
        return (1, "", "")

    ctx.executor.set("lsof -nP", unknown_lsof)
    ctx.executor.set("ps -o comm=", (1, "", ""))  # identity cannot be resolved
    ctx.executor.set("pgrep -f", (1, "", ""))     # no project executable running

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "OK"
    assert ctx.executor.connected is True
    assert "cannot be identified" in ctx.err.getvalue()
    assert "[WARN]" in ctx.err.getvalue()


def test_project_mihomo_still_listening_blocks_nextin(tmp_path):
    """A listener running the project executable is a real orphan: no Nextin."""
    ctx = _root_ctx(tmp_path, connected=False)

    def project_lsof(argv):
        port = int(argv[2].split(":", 1)[1])
        if port == 7890:
            return (0, "p4242\ncmihomo\n", "")
        return (1, "", "")

    ctx.executor.set("lsof -nP", project_lsof)
    ctx.executor.set("ps -o comm=", (0, conflict_mod.PROJECT_EXECUTABLE + "\n", ""))

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.connected is False
    assert not any("--nc start" in event for event in ctx.executor.events)
    assert any("mpm-switch nextin" in warning for warning in report.warnings)


def test_stop_unloaded_still_confirms_disabled_and_stopped(tmp_path):
    """Even an unloaded job gets the full structured confirmation."""
    ctx = _root_ctx(tmp_path, connected=False)

    report = lifecycle.stop(ctx, quiet=True)

    assert report.status == "UNCHANGED"
    outcome = report.data["stop_outcome"]
    assert outcome["disabled"] is True
    assert outcome["disabled_confirmed"] is True
    assert outcome["stopped"] is True
    assert outcome["orphan_free"] is True
    assert outcome["safe"] is True


def test_core_may_be_running_unknown_failure_assumes_running(tmp_path):
    """An unknown lsof/ifconfig failure is "may be running", never "safe"."""
    ctx = _ctx(tmp_path, connected=False)  # no listeners configured
    original = ctx.executor.run

    def lsof_fails(argv, **kwargs):
        if argv[:1] == ["lsof"]:
            return Result(tuple(argv), 2, "", "lsof exploded")
        return original(argv, **kwargs)

    ctx.executor.run = lsof_fails
    assert switch.core_may_be_running(ctx.executor) is True

    def ifconfig_fails(argv, **kwargs):
        if argv[:1] == ["ifconfig"]:
            return Result(tuple(argv), 1, "", "ifconfig exploded")
        return original(argv, **kwargs)

    ctx.executor.run = ifconfig_fails
    assert switch.core_may_be_running(ctx.executor) is True

    ctx.executor.run = original
    assert switch.core_may_be_running(ctx.executor) is False


def test_rollback_subcommand_is_idempotent(tmp_path):
    ctx = _root_ctx(tmp_path, connected=False)

    first = switch.rollback(ctx, uid=501, user="tester")
    second = switch.rollback(ctx, uid=501, user="tester")

    assert first.status == "OK"
    assert second.status == "OK"
    assert ctx.executor.connected is True
    assert ctx.executor.count("launchctl", "disable") >= 2


def test_rollback_does_not_connect_nextin_while_core_runs(tmp_path, monkeypatch):
    """An unsafe stop is never followed by ``scutil --nc start``."""
    from mpm_common.errors import NotReady

    ctx = _root_ctx(tmp_path, connected=False)

    def stop_fails(ctx_arg, *, quiet=False):
        raise NotReady("the core is still running after launchctl bootout")

    monkeypatch.setattr(switch.lifecycle, "stop", stop_fails)

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.connected is False, "no VPN start while mihomo may run"
    assert not any("--nc start" in event for event in ctx.executor.events)


def test_rollback_restores_proxy_snapshot(tmp_path, monkeypatch):
    executor = VpnExecutor(connected=False)
    ctx = make_context(tmp_path, executor=executor, geteuid=lambda: 0)
    harness = LaunchdHarness(ctx.executor)
    harness.layout = ctx.layout
    prepare_install(ctx)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    network = FakeNetworkSetup()
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    ctx.executor.set("networksetup", network)

    def fake_start(ctx_arg, *, quiet=False, mode=None, health_guard=None):
        # Simulate the privileged phase: core up, proxy pointed at mixed.
        ctx_arg.launchd().bootstrap()
        if health_guard is not None:
            health_guard()
        snapshot = ctx_arg.proxy_client().snapshot()
        from mpm_macos.network_proxy import save_snapshot

        save_snapshot(ctx_arg.layout.network_backup, snapshot)
        ctx_arg.proxy_client().apply(snapshot)
        return lifecycle.Report(command="start", status="OK")

    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = switch.switch_to_mihomo_root(
        ctx,
        uid=501,
        user="tester",
        probe_runner=lambda: probe.ProbeReport(ok=False, reason="all foreign probes failed"),
    )

    assert report.status == "FAILED"
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert network.state["Wi-Fi"]["web"]["port"] == 8080
    assert not (tmp_path / "Library" / "Application Support" / "mihomo-proxy-management"
                / "state" / "network-proxy-backup.json").exists()
    assert harness.loaded is False
    assert harness.disabled is True
    assert executor.connected is True, "rollback must end with Nextin connected"
    assert "已自动切回 Nextin" in ctx.out.getvalue()


# ---- shell wrapper --------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None,
    reason="the password dialog runs a POSIX shell",
)
def test_shell_wrapper_runs_rollback_without_marker(tmp_path):
    """No marker (crash / SIGKILL) means: run the idempotent rollback."""
    cli = tmp_path / "mpm cli"  # a space proves the shlex quoting
    log = tmp_path / "calls.log"
    cli.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$*" in\n'
        "  *rollback*) exit 0 ;;\n"
        "esac\n"
        "exit 3\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    marker = tmp_path / "marker"
    command = switch.shell_wrapper(
        str(cli),
        ["switch", "mihomo", "--user", "tester", "--uid", "501", "--marker", str(marker)],
        marker=str(marker),
        rollback_arguments=["switch", "rollback", "--user", "tester", "--uid", "501"],
    )

    # The refusal code must short-circuit the marker test, in that order.
    assert f'[ "$rc" -eq {switch.REFUSED_EXIT_CODE} ] || [ -f ' in command

    completed = subprocess.run(
        ["/bin/sh", "-c", command], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 3, "the original verdict must survive"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("switch mihomo")
    assert lines[1].startswith("switch rollback")

    # With the success marker present the rollback must not run.
    log.write_text("", encoding="utf-8")
    marker.write_text("ok\n", encoding="utf-8")
    subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=False)
    assert log.read_text(encoding="utf-8").splitlines() == [lines[0]]


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None,
    reason="the password dialog runs a POSIX shell",
)
def test_shell_wrapper_skips_the_rollback_on_a_refusal_code(tmp_path):
    """A refusal exit code means "nothing to undo", even without a marker.

    This is the write-failure path: the root phase could not create the marker,
    so only the exit code can keep the wrapper from running the rollback.
    """
    cli = tmp_path / "mpm-cli"
    log = tmp_path / "calls.log"
    cli.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        f"exit {switch.REFUSED_EXIT_CODE}\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    marker = tmp_path / "marker"
    command = switch.shell_wrapper(
        str(cli),
        ["switch", "mihomo", "--marker", str(marker)],
        marker=str(marker),
        rollback_arguments=["switch", "rollback"],
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", command], capture_output=True, text=True, check=False
    )

    assert completed.returncode == switch.REFUSED_EXIT_CODE
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "a refusal must not be followed by a rollback"
    assert lines[0].startswith("switch mihomo")


# ---- switch: nextin -------------------------------------------------------


def test_switch_nextin_stops_mihomo_then_connects_vpn(tmp_path):
    ctx = _ctx(tmp_path, connected=False)
    privileged: list[str] = []
    events: list[str] = []

    def privileged_runner(command: str) -> int:
        privileged.append(command)
        events.append("privileged")
        return 0

    def probe_runner():
        events.append("probe")
        return probe.ProbeReport(ok=True)

    report = switch.switch_to_nextin(
        ctx,
        privileged=privileged_runner,
        probe_runner=probe_runner,
        uid=501,
        user="tester",
    )

    assert report.status == "OK"
    assert len(privileged) == 1
    assert privileged[0].endswith("stop")
    assert ctx.executor.connected is True
    assert events[0] == "privileged"
    assert events[-1] == "probe"
    assert any("launchctl asuser 501 sudo -u tester scutil --nc start" in event for event in ctx.executor.events)


def test_switch_nextin_failure_to_connect_is_reported(tmp_path):
    ctx = _ctx(tmp_path, connected=False)
    # ``launchctl asuser`` is handled by VpnExecutor; make it refuse here.
    class _NoConnect(VpnExecutor):
        def run(self, argv, **kwargs):
            if argv[:2] == ["launchctl", "asuser"]:
                return Result(tuple(argv), 1, "", "not permitted")
            return super().run(argv, **kwargs)

    ctx.executor = _NoConnect(connected=False)

    report = switch.switch_to_nextin(ctx, privileged=lambda command: 0, uid=501, user="tester")

    assert report.status == "FAILED"
    assert any("could not connect" in warning for warning in report.warnings)


def test_switch_nextin_does_not_connect_while_mihomo_runs(tmp_path):
    """A failed stop must not produce two stacks."""
    ctx = _ctx(tmp_path, connected=False, listeners=(9090,))

    report = switch.switch_to_nextin(
        ctx, privileged=lambda command: 1, uid=501, user="tester"
    )

    assert report.status == "FAILED"
    assert ctx.executor.connected is False
    assert any("may still be running" in warning for warning in report.warnings)


def test_status_reports_who_is_in_charge(tmp_path):
    ctx = _ctx(tmp_path, connected=True)
    LaunchdHarness(ctx.executor)

    report = switch.status(ctx)

    assert report.status == "OK"
    assert report.data["vpn_connected"][0]["name"] == VPN
    assert report.data["mihomo"]["disabled"] is True
    assert any("Nextin" in message for message in report.messages)


# ---- Always On VPN (On Demand) preflight ----------------------------------


def test_on_demand_true_refuses_before_stop_and_dialog(tmp_path):
    """Always On VPN (On Demand) means the VPN reconnects right after the stop."""
    ctx = _ctx(tmp_path, connected=True, on_demand=True)
    marker = str(tmp_path / "marker")
    commands: list[str] = []

    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert commands == [], "the password dialog must not be opened"
    assert ctx.executor.count("osascript") == 0
    assert not any("--nc stop" in event for event in ctx.executor.events)
    assert ctx.executor.connected is True, "the VPN must be left untouched"
    assert report.data["on_demand"] == "enabled"
    text = " ".join(report.warnings)
    assert (
        "请在 Nextin App 里关闭 Always On VPN（scutil 显示为 On Demand）后再切换" in text
    )
    assert "disable Always On VPN (On Demand) in the Nextin app first" in text
    assert not os.path.exists(marker)


def test_on_demand_false_allows_the_switch(tmp_path):
    ctx = _ctx(tmp_path, connected=True, on_demand=False)
    marker = str(tmp_path / "marker")
    commands: list[str] = []

    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "OK"
    assert len(commands) == 1
    assert ctx.executor.connected is True, "the user phase still never stops the VPN"


def test_unreadable_on_demand_refuses(tmp_path):
    """A flag that cannot be read may well be on: fail closed."""
    ctx = _ctx(tmp_path, connected=True, on_demand=None)
    marker = str(tmp_path / "marker")
    commands: list[str] = []

    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "FAILED"
    assert commands == []
    assert not any("--nc stop" in event for event in ctx.executor.events)
    assert ctx.executor.connected is True
    assert report.data["on_demand"] == "unknown"
    text = " ".join(report.warnings)
    assert "cannot read the Always On VPN (On Demand) state" in text
    assert "disable Always On VPN (On Demand)" in text


def test_missing_service_without_vpn_may_proceed(tmp_path):
    """scutil "No service" and nothing connected: nothing can reconnect."""
    ctx = _ctx(tmp_path, connected=False, on_demand=None)
    marker = str(tmp_path / "marker")
    commands: list[str] = []

    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "OK"
    assert len(commands) == 1
    assert ctx.executor.connected is False, "the user phase never starts the VPN either"


def test_invalid_service_configuration_without_a_vpn_may_proceed(tmp_path):
    """Nextin is only a node source: an (Invalid) profile cannot reconnect."""
    ctx = _ctx(tmp_path, connected=False, on_demand="invalid")
    marker = str(tmp_path / "marker")
    commands: list[str] = []

    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "OK", report.warnings
    assert len(commands) == 1
    assert ctx.executor.connected is False


def test_root_rollback_does_not_retry_an_invalid_nextin_configuration(
    tmp_path, monkeypatch
):
    """An unusable profile is not reconnected (and does not spin the deadline)."""
    ctx = _root_ctx(tmp_path, connected=False, on_demand="invalid")

    def fake_start(ctx_arg, *, quiet=False, mode=None, health_guard=None):
        return lifecycle.Report(command="start", status="OK", data={})

    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = switch.switch_to_mihomo_root(
        ctx,
        uid=501,
        user="tester",
        probe_runner=lambda: probe.ProbeReport(ok=False, reason="drill"),
    )

    assert report.status == "FAILED"
    assert any("--nc stop" in event for event in ctx.executor.events), (
        "the gate must allow the switch, not refuse before the teardown"
    )
    assert not any("--nc start" in event for event in ctx.executor.events)
    assert ctx.executor.connected is False
    assert "has no usable VPN configuration" in ctx.err.getvalue()



    """An unknown service must not excuse a connected VPN."""
    ctx = _ctx(tmp_path, connected=True, on_demand=None)
    ctx.executor.events.clear()
    # The service name lookup fails while another VPN is up.
    original = ctx.executor._nc_list_text

    def other_vpn():
        return (
            "Available network connection services in the current set:\n"
            '* (Connected)    "Corp VPN" [VPN:com.example.corp]\n'
        )

    ctx.executor._nc_list_text = other_vpn
    commands: list[str] = []
    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(str(tmp_path / "marker"), commands),
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )
    ctx.executor._nc_list_text = original

    assert report.status == "FAILED"
    assert commands == []


def test_no_service_is_recognised_whatever_the_exit_code(tmp_path):
    """scutil's "No service" is the only exception that may proceed: read it always.

    It must not depend on scutil's return code, otherwise a build that reports
    the condition on stdout while exiting 0 would be read as "unreadable" and
    fail closed on a machine that has no such VPN at all.
    """

    class _ZeroExitNoService(VpnExecutor):
        def _show(self, argv):
            return Result(tuple(argv), 0, "No service\n", "")

    executor = _ZeroExitNoService(connected=False)
    ctx = make_context(tmp_path, executor=executor, geteuid=lambda: 501)
    ctx.environ["MPM_VPN_NAME"] = VPN

    state = switch.read_on_demand(executor, VPN)
    assert state.value is None
    assert state.service_missing is True

    marker = str(tmp_path / "marker")
    commands: list[str] = []
    report = switch.switch_to_mihomo(
        ctx,
        privileged=_marker_writer(marker, commands),
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "OK", report.warnings
    assert len(commands) == 1, "nothing can reconnect: the switch may proceed"


def _assert_no_system_side_effects(ctx, *, connected: bool) -> None:
    """A refusal that changed nothing must leave no command behind."""
    events = "\n".join(ctx.executor.events)
    assert "--nc stop" not in events, "the VPN must not be stopped"
    assert "--nc start" not in events, "the VPN must not be (re)started"
    for verb in ("bootstrap", "bootout", "kickstart", "disable", "enable"):
        assert ctx.executor.count("launchctl", verb) == 0, f"launchctl {verb} ran"
    assert ctx.executor.count("osascript") == 0, "no password dialog"
    assert ctx.executor.count("networksetup") == 0, "no proxy change"
    assert ctx.executor.count("mihomo") == 0, "the core was never launched"
    assert ctx.executor.connected is connected


def test_root_phase_on_demand_true_refuses_before_the_protected_flow(tmp_path, monkeypatch):
    """The root refusal returns directly: no rollback, no system command.

    Entering the generic rollback would run ``lifecycle.stop`` (bootout +
    persistent ``launchctl disable``), restore the proxy snapshot and try
    ``scutil --nc start`` - all needless for a refusal that changed nothing.
    """
    stops: list[str] = []

    def spy_stop(ctx_arg, **kwargs):
        stops.append("stop")
        return lifecycle.Report(command="stop", status="OK", data={})

    monkeypatch.setattr(switch.lifecycle, "stop", spy_stop)
    ctx = _root_ctx(tmp_path, connected=True, on_demand=True)
    marker = str(tmp_path / "marker")

    report = switch.switch_to_mihomo_root(
        ctx, uid=501, user="tester", marker=marker
    )

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert report.data["on_demand"] == "enabled"
    assert stops == [], "the refusal must not enter _root_rollback"
    _assert_no_system_side_effects(ctx, connected=True)
    assert os.path.exists(marker), (
        "the shell wrapper must skip its rollback: nothing was changed"
    )


def test_root_phase_unreadable_on_demand_refuses_before_the_protected_flow(
    tmp_path, monkeypatch
):
    """An unreadable flag fails closed the same way, with no side effects."""
    stops: list[str] = []

    def spy_stop(ctx_arg, **kwargs):
        stops.append("stop")
        return lifecycle.Report(command="stop", status="OK", data={})

    monkeypatch.setattr(switch.lifecycle, "stop", spy_stop)
    ctx = _root_ctx(tmp_path, connected=True, on_demand=None)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert report.data["on_demand"] == "unknown"
    assert stops == []
    _assert_no_system_side_effects(ctx, connected=True)
    assert "cannot read the Always On VPN (On Demand) state" in " ".join(
        report.warnings
    )


def test_root_refusal_survives_a_failed_marker_write(tmp_path, monkeypatch):
    """A refusal must not turn into a rollback when the marker cannot be written.

    The wrapper keys on :data:`switch.REFUSED_EXIT_CODE`, so the marker is only
    the *second* signal.  A read-only or vanished temp directory must still
    leave the machine untouched: no stop, no disable, no VPN start.
    """
    ctx = _root_ctx(tmp_path, connected=True, on_demand=True)
    marker = str(tmp_path / "marker")

    def broken_write(path):
        raise OSError("read-only directory")

    monkeypatch.setattr(switch, "_write_marker", broken_write)

    report = switch.switch_to_mihomo_root(
        ctx, uid=501, user="tester", marker=marker
    )

    assert report.status == "FAILED"
    assert report.exit_code == switch.REFUSED_EXIT_CODE
    assert not os.path.exists(marker)
    _assert_no_system_side_effects(ctx, connected=True)
    assert any(
        "could not write the refusal marker" in warning for warning in report.warnings
    ), report.warnings


def test_user_phase_reports_a_root_refusal_without_recovering(tmp_path):
    """The dedicated code means "nothing was touched": do not start Nextin."""
    ctx = _ctx(tmp_path, connected=False, on_demand=False)

    report = switch.switch_to_mihomo(
        ctx,
        privileged=lambda command: switch.REFUSED_EXIT_CODE,
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )

    assert report.status == "FAILED"
    assert report.exit_code == int(ExitCode.FAIL_CLOSED)
    _assert_no_system_side_effects(ctx, connected=False)
    text = " ".join(report.warnings)
    assert "refused before touching anything" in text
    assert "Always On VPN" in text


def test_user_phase_treats_a_refusal_marker_as_a_refusal(tmp_path):
    """``do shell script`` flattens the code, so the marker is the fallback."""
    ctx = _ctx(tmp_path, connected=False, on_demand=False)
    marker = str(tmp_path / "marker")

    def refused_with_marker(command: str) -> int:
        Path(marker).write_text("ok\n", encoding="utf-8")
        return 1

    report = switch.switch_to_mihomo(
        ctx,
        privileged=refused_with_marker,
        uid=501,
        user="tester",
        marker=marker,
    )

    assert report.status == "FAILED"
    assert report.exit_code == int(ExitCode.FAIL_CLOSED)
    _assert_no_system_side_effects(ctx, connected=False)


def test_user_phase_root_refusal_keeps_a_connected_nextin_untouched(tmp_path):
    """A refused root phase reports failure and does not start Nextin again."""
    ctx = _ctx(tmp_path, connected=True, on_demand=False)
    commands: list[str] = []

    def refused(command: str) -> int:
        commands.append(command)
        return 1

    report = switch.switch_to_mihomo(
        ctx,
        privileged=refused,
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )

    assert report.status == "FAILED"
    assert len(commands) == 1, "the dialog ran exactly once"
    _assert_no_system_side_effects(ctx, connected=True)


# ---- stable disconnect window ---------------------------------------------


def test_stable_window_detects_a_reconnect_and_skips_mihomo(tmp_path):
    """A VPN that comes back by itself must never be raced by mihomo."""
    ctx = _root_ctx(tmp_path, connected=True, reconnect_polls=2)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert any(
        "reconnected within 4s after stop (Always On VPN / On Demand?)" in warning
        for warning in report.warnings
    ), report.warnings
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert ctx.executor.connected is True, "the rollback brings the VPN back"


def test_stable_window_polls_the_whole_period(tmp_path, monkeypatch):
    """The stop must be followed by a full quiet window, polled <= 0.5 s."""
    ctx = _root_ctx(tmp_path, connected=True)
    monkeypatch.setattr(
        switch.lifecycle,
        "start",
        lambda ctx_arg, quiet=False, **kwargs: lifecycle.Report(
            command="start", status="OK", data={"new_utun_interfaces": ["utun7"]}
        ),
    )

    report = switch.switch_to_mihomo_root(
        ctx, uid=501, user="tester", probe_runner=lambda: probe.ProbeReport(ok=True)
    )

    assert report.status == "OK"
    polls = [event for event in ctx.executor.events if "--nc list" in event]
    assert len(polls) >= 9, "4 s at <= 0.5 s per poll plus the stop confirmation"
    assert all(event.startswith("launchctl asuser") for event in polls)
    assert ctx.clock.sleeps, "the window uses the injected sleeper"
    assert max(ctx.clock.sleeps) <= 0.5 + 1e-9


def test_reconnect_after_mihomo_start_rolls_back(tmp_path, monkeypatch):
    """The post-start guard runs before any controller call."""

    def fake_start(ctx_arg, *, quiet=False, mode=None, health_guard=None):
        ctx_arg.executor.connected = True  # Always On VPN brought Nextin back
        if health_guard is not None:
            health_guard()
        return lifecycle.Report(command="start", status="OK", data={})

    ctx = _root_ctx(tmp_path, connected=True)
    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert any(
        "reconnected after mihomo start" in warning for warning in report.warnings
    ), report.warnings
    assert ctx.executor.connected is True


def test_conflict_vpn_after_start_rolls_back(tmp_path, monkeypatch):
    """The conflict detector's VPN list is part of the post-start guard."""

    def fake_start(ctx_arg, *, quiet=False, mode=None, health_guard=None):
        if health_guard is not None:
            health_guard()
        return lifecycle.Report(command="start", status="OK", data={})

    ctx = _root_ctx(tmp_path, connected=True)
    ctx.conflict_detector = lambda: conflict_mod.ConflictReport(
        ok=True, vpn_connected=[conflict_mod.VpnConnection(name=VPN)]
    )
    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert any(
        "external VPN connected after mihomo start" in warning
        for warning in report.warnings
    ), report.warnings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 3.0), ("9", 5.0), ("4.5", 4.5), ("abc", 4.0), ("", 4.0)],
)
def test_stable_disconnect_seconds_is_clamped(raw, expected):
    environ = {} if raw == "" else {"MPM_STABLE_DISCONNECT_SECONDS": raw}
    assert switch.stable_disconnect_seconds(environ) == expected


# ---- switching back: only ``scutil --nc start`` ---------------------------


def test_switch_nextin_only_starts_the_vpn_and_records_on_demand(tmp_path):
    ctx = _ctx(tmp_path, connected=False, on_demand=True)

    report = switch.switch_to_nextin(
        ctx,
        privileged=lambda command: 0,
        probe_runner=lambda: probe.ProbeReport(ok=True),
        uid=501,
        user="tester",
    )

    assert report.status == "OK"
    assert ctx.executor.connected is True
    assert report.data["on_demand"] == "enabled"
    events = "\n".join(ctx.executor.events)
    assert "--nc start" in events
    assert "--nc set" not in events, "Always On VPN (On Demand) is never modified"
    assert "OnDemand" not in events


# ---- stop teardown: "not Connected" is not "disconnected" -----------------


class TeardownVpn(VpnExecutor):
    """The 2026-11-15 12:09:48 time series.

    ``scutil --nc status`` walks the given states; Nextin's ``ProxyTunnel``
    keeps 7890 for ``listener_polls`` port polls after the ``(Connected)`` flag
    is gone and only then exits.  ``pid_alive_polls`` makes the plugin process
    itself outlive the port release: ``ps -p 5001`` still answers for that many
    polls after the first one, which is the race one layer below the port.
    Once a ``start`` was issued the service is reported ``Connected`` (the
    rollback path must not spin).
    """

    def __init__(self, *, states, listener_polls, pid_alive_polls=0):
        super().__init__(connected=True)
        self.states = list(states)
        self.listener_polls = listener_polls
        self.pid_alive_polls = pid_alive_polls
        self.port_polls = 0
        self.pid_polls = 0
        self.listening = True
        self.pid_alive = True
        #: port polls already done when ``scutil --nc stop`` ran: the external
        #: listener PIDs must be recorded *before* the stop
        self.port_polls_at_stop: int | None = None

    def _apply(self, argv):
        if "stop" in argv and self.port_polls_at_stop is None:
            self.port_polls_at_stop = self.port_polls
        super()._apply(argv)

    def _status(self, argv):
        if self.start_calls:
            return self._result(argv, "Connected\n")
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return self._result(argv, state + "\n")

    def _lsof(self, argv):
        port = int(argv[2].split(":", 1)[1])
        if port == 7890:
            self.port_polls += 1
            if self.listening and self.port_polls > self.listener_polls:
                self.listening = False
        if self.listening and port == 7890:
            return (0, "p5001\ncProxyTunnel\n", "")
        return (1, "", "")

    def _ps(self, argv):
        if "pid=" in argv:
            self.pid_polls += 1
            if self.pid_alive and self.pid_polls > self.pid_alive_polls:
                self.pid_alive = False
            if self.pid_alive:
                return Result(tuple(argv), 0, "5001\n", "")
            return Result(tuple(argv), 1, "", "")
        return Result(
            tuple(argv),
            0,
            "/Applications/Nextin.app/Contents/MacOS/ProxyTunnel\n",
            "",
        )

    def run(self, argv, **kwargs):
        if argv[:1] == ["ps"]:
            return self._ps(argv)
        return super().run(argv, **kwargs)


def test_stop_waits_for_the_tunnel_plugin_before_the_conflict_check(
    tmp_path, monkeypatch
):
    """Connected gone -> Disconnecting -> ports released -> start.

    The ``(Connected)`` flag disappears ~0.3 s after the stop while
    ``ProxyTunnel`` still holds 7890; the 2026-11-15 rollback ran the conflict
    check inside that window and failed closed.  The stop is only finished when
    the state is ``Disconnected`` *and* nothing listens on 7890/9090.
    """
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor = TeardownVpn(
        states=["connected", "disconnecting", "disconnected", "disconnected"],
        listener_polls=2,
    )
    LaunchdHarness(ctx.executor)
    # the real detector: a listener on 7890 is a conflict, so mihomo may only
    # start once ProxyTunnel has exited
    ctx.conflict_detector = None

    at_start: list[bool] = []

    def fake_start(ctx_arg, quiet=False, **kwargs):
        at_start.append(ctx.executor.listening)
        return lifecycle.Report(
            command="start", status="OK", data={"new_utun_interfaces": ["utun7"]}
        )

    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = switch.switch_to_mihomo_root(
        ctx, uid=501, user="tester", probe_runner=lambda: probe.ProbeReport(ok=True)
    )

    assert report.status == "OK", report.warnings
    assert at_start == [False], "mihomo must start only after ProxyTunnel exited"
    assert report.data["stop_wait"]["ok"] is True
    assert report.data["stop_wait"]["state"] == "disconnected"
    assert report.data["stop_wait"]["leftover"] == []
    assert ctx.executor.port_polls > 2, "the ports were polled until they were free"


def test_stop_waits_for_the_tunnel_pid_after_the_port_is_released(
    tmp_path, monkeypatch
):
    """Port released but ``ProxyTunnel`` still alive -> keep waiting.

    A plugin that frees 7890 a moment before it exits would otherwise let the
    conflict check (and mihomo) run against a still-live network stack.  The
    PID recorded *before* the stop must be gone before the switch continues.
    """
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor = TeardownVpn(
        states=["disconnected"], listener_polls=1, pid_alive_polls=3
    )
    LaunchdHarness(ctx.executor)
    ctx.conflict_detector = None

    at_start: list[bool] = []

    def fake_start(ctx_arg, quiet=False, **kwargs):
        at_start.append(ctx.executor.pid_alive)
        return lifecycle.Report(
            command="start", status="OK", data={"new_utun_interfaces": ["utun7"]}
        )

    monkeypatch.setattr(switch.lifecycle, "start", fake_start)

    report = switch.switch_to_mihomo_root(
        ctx, uid=501, user="tester", probe_runner=lambda: probe.ProbeReport(ok=True)
    )

    assert report.status == "OK", report.warnings
    assert report.data["stop_wait"]["ok"] is True
    assert report.data["stop_wait"]["watched"] == [5001]
    assert ctx.executor.port_polls_at_stop == 1, (
        "the listener PID must be recorded before the stop command"
    )
    assert ctx.executor.pid_polls > 1, "the PID was polled until it exited"
    assert at_start == [False], "mihomo must start only after the plugin exited"


def test_stop_fails_closed_when_the_tunnel_pid_never_exits(tmp_path):
    """A listener PID that outlives the deadline is never ignored."""
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor = TeardownVpn(
        states=["disconnected"], listener_polls=1, pid_alive_polls=10**6
    )
    LaunchdHarness(ctx.executor)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    text = " ".join(report.warnings)
    assert "did not finish disconnecting" in text
    assert "pid 5001 still alive" in text


def test_stable_window_requires_disconnected_on_every_poll(tmp_path):
    """A teardown still in progress must not pass the quiet window.

    "Absent from the Connected list" is not "disconnected": a ``Stopping``
    read inside the window means the old session may still come back, so the
    switch fails closed without starting mihomo.
    """
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor = TeardownVpn(
        states=["disconnected", "disconnected", "stopping"], listener_polls=0
    )
    LaunchdHarness(ctx.executor)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert report.data["stable_window"]["ok"] is False
    assert report.data["stable_window"]["state"] == "stopping"
    text = " ".join(report.warnings)
    assert "not continuously disconnected" in text
    assert "stopping" in text


def test_stop_fails_closed_when_the_port_is_never_released(tmp_path):
    """A port still held after the deadline is never ignored."""
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor = TeardownVpn(states=["disconnected"], listener_polls=10**6)
    LaunchdHarness(ctx.executor)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    text = " ".join(report.warnings)
    assert "did not finish disconnecting" in text
    assert "ProxyTunnel" in text
    assert "7890" in text


def test_stop_reports_a_reconnect_that_happens_during_the_teardown(tmp_path):
    """A VPN that came back is a reconnect, not a slow teardown."""
    ctx = _root_ctx(tmp_path, connected=True)
    ctx.executor = TeardownVpn(
        states=["disconnected", "connected"], listener_polls=10**6
    )
    LaunchdHarness(ctx.executor)

    report = switch.switch_to_mihomo_root(ctx, uid=501, user="tester")

    assert report.status == "FAILED"
    assert ctx.executor.count("launchctl", "bootstrap") == 0
    assert any(
        "reconnected 0.5s after stop while waiting for the teardown to finish"
        in warning
        for warning in report.warnings
    ), report.warnings
    assert any("Always On VPN / On Demand?" in warning for warning in report.warnings)


# ---- start state machine: a skipped start is re-sent ----------------------


class StartIgnoredVpn(VpnExecutor):
    """``scutil --nc start`` is dropped while the old session tears down.

    2026-11-15 12:09:48: the start returned 0 and the service fell straight
    back to ``Disconnecting``; only the *next* start, sent after the service
    was ``Disconnected`` again, connected.  The fake walks that series:

    * before any start: ``Disconnecting`` for ``teardown_polls`` status reads,
      then ``Disconnected`` (so the first start has to wait for it);
    * the first ``ignored_starts`` starts are dropped and leave the service
      ``Disconnecting`` for ``stall_polls`` more reads (a start must never be
      re-sent into that);
    * a later start connects.

    ``start_after_status_calls`` records how many status reads had happened
    when each start was issued, so a test can prove both starts waited for
    ``Disconnected``.
    """

    def __init__(
        self,
        *,
        connected=True,
        teardown_polls=3,
        stall_polls=12,
        ignored_starts=1,
    ):
        super().__init__(connected=connected)
        self.teardown_polls = teardown_polls
        self.stall_polls = stall_polls
        self.ignored_starts = ignored_starts
        self.status_calls = 0
        self.start_after_status_calls: list[int] = []

    @property
    def _stall_until(self) -> int:
        """Status reads still reporting ``Disconnecting`` after a dropped start."""
        return self.teardown_polls + 1 + self.stall_polls

    def _status(self, argv):
        if self.start_calls > self.ignored_starts:
            return self._result(argv, "Connected\n")
        self.status_calls += 1
        limit = self.teardown_polls if not self.start_calls else self._stall_until
        if self.status_calls <= limit:
            return self._result(argv, "Disconnecting\n")
        return self._result(argv, "Disconnected\n")

    def _apply(self, argv):
        if "start" in argv:
            self.start_calls += 1
            self.start_after_status_calls.append(self.status_calls)
            if self.start_calls > self.ignored_starts:
                self.connected = True
        elif "stop" in argv:
            self.connected = False


def test_start_is_resent_when_the_first_one_is_ignored(tmp_path):
    """2026-11-15 12:09:48: the first start was dropped, the second worked.

    ``scutil --nc start`` returns 0 but nesessionmanager drops it; the service
    falls back to ``Disconnecting``.  The state machine must not re-send into
    that teardown: it waits for ``Disconnected`` again, and only then re-sends
    (5 s after the dropped start) and accepts the ``Connected`` read.
    """
    ctx = _ctx(tmp_path, connected=True)
    ctx.executor = StartIgnoredVpn(teardown_polls=3, stall_polls=12)

    report = switch.switch_to_nextin(
        ctx,
        privileged=lambda command: 0,
        probe_runner=lambda: probe.ProbeReport(ok=True),
        uid=501,
        user="tester",
    )

    assert report.status == "OK", report.warnings
    starts = [event for event in ctx.executor.events if "--nc start" in event]
    assert len(starts) == 2, starts
    assert all(
        event.startswith("launchctl asuser 501 sudo -u tester") for event in starts
    )
    issued = ctx.executor.start_after_status_calls
    assert issued[0] > 3, (
        "the first start must wait for Disconnected, not be sent into the teardown"
    )
    assert issued[1] > ctx.executor._stall_until, (
        "the re-sent start must wait for Disconnected too, not fire into the "
        "teardown the dropped start left behind"
    )
    assert ctx.executor.connected is True


def test_rollback_resends_a_start_that_was_skipped_during_the_teardown(tmp_path):
    """``_root_rollback`` uses the same start state machine."""
    ctx = _root_ctx(tmp_path, connected=False)
    ctx.executor = StartIgnoredVpn(teardown_polls=2, stall_polls=12)
    LaunchdHarness(ctx.executor)

    report = switch.rollback(ctx, uid=501, user="tester")

    assert report.status == "OK", report.warnings
    starts = [event for event in ctx.executor.events if "--nc start" in event]
    assert len(starts) == 2, starts
    assert ctx.executor.connected is True


def test_user_phase_recovery_resends_a_start_that_was_ignored(tmp_path):
    """The user phase's recovery path is the same state machine.

    A cancelled dialog leaves Nextin down; the recovery starts it in the
    *current* session (no ``asuser``) and must survive a dropped first start
    the same way the root phase does.
    """
    ctx = _ctx(tmp_path, connected=False)
    ctx.executor = StartIgnoredVpn(connected=False, teardown_polls=2, stall_polls=12)

    report = switch.switch_to_mihomo(
        ctx,
        privileged=lambda command: 1,
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )

    assert report.status == "FAILED"
    assert ctx.executor.connected is True, "Nextin must be back after the recovery"
    starts = [event for event in ctx.executor.events if "--nc start" in event]
    assert len(starts) == 2, starts
    assert all(event.startswith("scutil --nc start") for event in starts), (
        "the user phase runs in the user's own session, so no asuser wrapper"
    )


def test_on_demand_refusal_message_names_always_on_vpn(tmp_path):
    """The operator text names Always On VPN (On Demand) in both languages."""
    ctx = _ctx(tmp_path, connected=True, on_demand=True)

    report = switch.switch_to_mihomo(
        ctx,
        privileged=lambda command: 0,
        uid=501,
        user="tester",
        marker=str(tmp_path / "marker"),
    )

    assert report.exit_code == int(ExitCode.FAIL_CLOSED)
    assert switch.ON_DEMAND_REFUSAL.format(name=VPN) == (
        "请在 Nextin App 里关闭 Always On VPN（scutil 显示为 On Demand）后再切换 / "
        "disable Always On VPN (On Demand) in the Nextin app first"
    )
    assert any(
        switch.ON_DEMAND_REFUSAL.format(name=VPN) in warning
        for warning in report.warnings
    ), report.warnings
