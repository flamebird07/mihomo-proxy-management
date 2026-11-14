"""``mpm-switch``: both directions end with a working machine (host-free)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from mpm_common.executor import FakeExecutor, Result
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
    """

    def __init__(
        self,
        *,
        connected: bool = False,
        stubborn: bool = False,
        listeners: tuple[int, ...] = (),
    ) -> None:
        super().__init__()
        self.connected = connected
        self.stubborn = stubborn
        self.listeners = tuple(listeners)
        self.events: list[str] = []
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

    def _nc_list_text(self) -> str:
        state = "Connected" if self.connected else "Disconnected"
        return (
            "Available network connection services in the current set:\n"
            f'* ({state})    "{VPN}" [VPN:com.Tommy.Nextin]\n'
        )

    def _apply(self, argv) -> None:
        if "stop" in argv:
            if not self.stubborn:
                self.connected = False
        elif "start" in argv:
            self.connected = True

    def run(self, argv, **kwargs):
        if argv[:2] == ["scutil", "--nc"]:
            action = argv[2] if len(argv) > 2 else ""
            self.events.append(" ".join(argv))
            if action == "list":
                return self._result(argv, self._nc_list_text())
            self._apply(argv)
            return self._result(argv)
        if argv[:2] == ["launchctl", "asuser"]:
            self.events.append(" ".join(argv))
            if argv[6:8] == ["scutil", "--nc"] and len(argv) > 8 and argv[8] == "list":
                return self._result(argv, self._nc_list_text())
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


def _ctx(tmp_path, *, connected=False, stubborn=False, euid=501, listeners=()):
    executor = VpnExecutor(
        connected=connected, stubborn=stubborn, listeners=listeners
    )
    ctx = make_context(tmp_path, executor=executor, geteuid=lambda: euid)
    ctx.environ["MPM_VPN_NAME"] = VPN
    return ctx


def _root_ctx(tmp_path, *, connected=False, stubborn=False, listeners=()):
    ctx = _ctx(
        tmp_path,
        connected=connected,
        stubborn=stubborn,
        euid=0,
        listeners=listeners,
    )
    LaunchdHarness(ctx.executor)
    return ctx


def _marker_writer(marker: str, calls: list[str] | None = None):
    def runner(command: str) -> int:
        if calls is not None:
            calls.append(command)
        Path(marker).write_text("ok\n", encoding="utf-8")
        return 0

    return runner


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
    monkeypatch.setattr(
        switch.lifecycle,
        "start",
        lambda ctx_arg, quiet=False: lifecycle.Report(
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
        lambda ctx_arg, quiet=False: lifecycle.Report(
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

    def fake_start(ctx_arg, quiet=False):
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
    prepare_install(ctx)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    network = FakeNetworkSetup()
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    ctx.executor.set("networksetup", network)

    def fake_start(ctx_arg, *, quiet=False, mode=None):
        # Simulate the privileged phase: core up, proxy pointed at mixed.
        ctx_arg.launchd().bootstrap()
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
