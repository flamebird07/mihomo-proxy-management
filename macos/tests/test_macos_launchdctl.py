"""``launchctl`` adapter: argv construction, parsing and fixed-label scope."""

from __future__ import annotations

import pytest

from mpm_common.errors import FailClosed
from mpm_common.executor import FakeExecutor
from mpm_macos import launchdctl

from macos_test_support import LaunchdHarness

pytestmark = pytest.mark.macos

LABEL = "com.mihomo-proxy-management.core"
PLIST = "/Library/LaunchDaemons/com.mihomo-proxy-management.core.plist"

RUNNING = f"""system/{LABEL} = {{
\tactive count = 1
\tpath = {PLIST}
\ttype = LaunchDaemon
\tstate = running
\tprogram = /usr/bin/python3
\tpid = 4321
\tlast exit code = 0
}}
"""

STOPPED = f"""system/{LABEL} = {{
\tpath = {PLIST}
\tstate = not running
\tlast exit code = 0
}}
"""


def test_parse_running():
    state = launchdctl.parse_print(RUNNING)
    assert state.loaded is True
    assert state.running is True
    assert state.pid == 4321
    assert state.last_exit == 0
    assert state.state_text == "running"
    assert state.program == "/usr/bin/python3"


def test_parse_stopped_but_loaded():
    state = launchdctl.parse_print(STOPPED)
    assert state.loaded is True
    assert state.running is False
    assert state.last_exit == 0


def test_parse_not_found():
    state = launchdctl.parse_print(
        'Bad request.\nCould not find service "com.mihomo-proxy-management.core" in domain for system',
        returncode=113,
    )
    assert state.loaded is False
    assert state.running is False


def test_parse_nonzero_exit_is_not_loaded():
    state = launchdctl.parse_print(RUNNING, returncode=1)
    assert state.loaded is False


def _launchd(executor, *, attempts=3):
    return launchdctl.Launchd(
        executor,
        plist=PLIST,
        sleeper=lambda _s: None,
        attempts=attempts,
        interval=0,
    )


def test_bootstrap_uses_domain_and_plist():
    executor = FakeExecutor()
    harness = LaunchdHarness(executor)
    service = _launchd(executor)
    service.bootstrap()
    call = executor.find("launchctl", "bootstrap")
    assert call is not None
    assert call.argv == ["launchctl", "bootstrap", "system", PLIST]
    assert harness.loaded is True


def test_bootstrap_is_idempotent_when_loaded():
    executor = FakeExecutor()
    harness = LaunchdHarness(executor)
    harness.loaded = True
    service = _launchd(executor)
    service.bootstrap()
    assert executor.count("launchctl", "bootstrap") == 0


def test_bootout_targets_the_fixed_label():
    executor = FakeExecutor()
    harness = LaunchdHarness(executor)
    harness.loaded = True
    harness.running = True
    service = _launchd(executor)
    service.bootout()
    call = executor.find("launchctl", "bootout")
    assert call is not None
    assert call.argv == ["launchctl", "bootout", f"system/{LABEL}"]
    assert harness.loaded is False


def test_bootout_is_idempotent_when_not_loaded():
    executor = FakeExecutor()
    LaunchdHarness(executor)
    service = _launchd(executor)
    service.bootout()
    assert executor.count("launchctl", "bootout") == 0


def test_kickstart_targets_the_fixed_label():
    executor = FakeExecutor()
    harness = LaunchdHarness(executor)
    harness.loaded = True
    harness.running = False
    service = _launchd(executor)
    service.kickstart()
    call = executor.find("launchctl", "kickstart")
    assert call is not None
    assert call.argv == ["launchctl", "kickstart", "-k", f"system/{LABEL}"]
    assert harness.running is True


def test_print_argv_is_fixed():
    executor = FakeExecutor()
    LaunchdHarness(executor)
    service = _launchd(executor)
    service.state()
    call = executor.find("launchctl", "print")
    assert call is not None
    assert call.argv == ["launchctl", "print", f"system/{LABEL}"]


def test_wait_running_times_out_without_touching_the_host():
    executor = FakeExecutor()
    harness = LaunchdHarness(executor)
    harness.loaded = True
    harness.running = False
    service = _launchd(executor, attempts=2)
    assert service.wait_running() is False


def test_foreign_label_is_refused():
    with pytest.raises(FailClosed):
        launchdctl.Launchd(FakeExecutor(), plist=PLIST, label="com.example.other")


def test_bootstrap_failure_is_reported(monkeypatch):
    executor = FakeExecutor()
    LaunchdHarness(executor)
    executor.set("launchctl bootstrap", (1, "", "Operation not permitted"))
    service = _launchd(executor)
    with pytest.raises(FailClosed):
        service.bootstrap()


def test_parse_disabled_reads_the_label_entry():
    text = (
        "disabled services = {\n"
        '\t"com.example.other" => true\n'
        f'\t"{LABEL}" => disabled\n'
        "}\n"
    )
    assert launchdctl.parse_disabled(text) is True
    assert launchdctl.parse_disabled('disabled services = {\n}\n') is False
    assert launchdctl.parse_disabled(f'disabled services = {{\n\t"{LABEL}" => false\n}}\n') is False


def test_enable_and_disable_use_the_fixed_label():
    executor = FakeExecutor()
    harness = LaunchdHarness(executor)
    service = _launchd(executor)

    service.enable()
    assert harness.disabled is False
    assert executor.find("launchctl", "enable").argv == [
        "launchctl",
        "enable",
        f"system/{LABEL}",
    ]

    service.disable()
    assert harness.disabled is True
    assert executor.find("launchctl", "disable").argv == [
        "launchctl",
        "disable",
        f"system/{LABEL}",
    ]
    assert service.is_disabled() is True


def test_disable_failure_is_reported():
    executor = FakeExecutor()
    LaunchdHarness(executor)
    executor.set("launchctl disable", (1, "", "Operation not permitted"))
    service = _launchd(executor)
    with pytest.raises(FailClosed):
        service.disable()
