"""Runner exit-code semantics and the ``caffeinate -i`` opt-in."""

from __future__ import annotations

import os
import signal

import pytest

from mpm_macos import runner
from mpm_macos.conflict import ConflictReport
from mpm_macos.locking import FileLock

from macos_test_support import FakeFlock, FakePopen

pytestmark = pytest.mark.macos


def _no_conflict() -> ConflictReport:
    return ConflictReport(ok=True)


def test_exit_code_mapping():
    assert runner.exit_code_for(0) == 0
    assert runner.exit_code_for(3) == 3
    assert runner.exit_code_for(-9) == 137  # SIGKILL -> 128 + 9
    assert runner.exit_code_for(-15) == 143  # SIGTERM -> 128 + 15


def test_build_argv_plain():
    assert runner.build_argv("/opt/mihomo", "/var/state") == [
        "/opt/mihomo",
        "-d",
        "/var/state",
    ]


def test_build_argv_with_config():
    assert runner.build_argv("/opt/mihomo", "/var/state", config="/var/state/config.yaml") == [
        "/opt/mihomo",
        "-d",
        "/var/state",
        "-f",
        "/var/state/config.yaml",
    ]


def test_build_argv_caffeinate_uses_only_dash_i():
    argv = runner.build_argv("/opt/mihomo", "/var/state", caffeinate=True)
    assert argv[0] == runner.CAFFEINATE_PATH
    assert argv[1] == "-i"
    assert argv[2:] == ["/opt/mihomo", "-d", "/var/state"]
    assert argv.count("-i") == 1
    assert "-s" not in argv
    assert "-u" not in argv


def _binary(tmp_path):
    binary = tmp_path / "mihomo"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


def _run(tmp_path, returncode, *, flock=None, **kwargs):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    kwargs.setdefault("conflict_checker", _no_conflict)
    return runner.run(
        binary=str(_binary(tmp_path)),
        state_dir=str(state),
        core_lock=str(tmp_path / "run" / "core.lock"),
        popen=lambda *args, **kw: FakePopen(returncode),
        install_signal_handlers=False,
        # injected: the real flock is POSIX-only and the tests run on Windows too
        flock=flock if flock is not None else FakeFlock(),
        **kwargs,
    )


def test_clean_exit_is_zero(tmp_path):
    assert _run(tmp_path, 0) == 0


def test_non_zero_exit_is_preserved(tmp_path):
    assert _run(tmp_path, 3) == 3


def test_signal_death_becomes_128_plus_signal(tmp_path):
    assert _run(tmp_path, -9) == 137
    assert _run(tmp_path, -15) == 143


def test_runner_signal_exit_code_mapping(tmp_path):
    """The mapping runs after the handler restore, not instead of it."""
    assert runner.exit_code_for(-9) == 137
    assert runner.exit_code_for(-15) == 143
    assert _run(tmp_path, -9) == 137

    if os.name == "nt":
        pytest.skip("POSIX signals are not emulated on Windows")

    class _Killed(FakePopen):
        def wait(self, timeout=None):
            # the runner installed its handler: this is an explicit stop
            os.kill(os.getpid(), signal.SIGTERM)
            return -15

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    code = runner.run(
        binary=str(_binary(tmp_path)),
        state_dir=str(state),
        core_lock=str(tmp_path / "run" / "core.lock"),
        popen=lambda *args, **kw: _Killed(-15),
        install_signal_handlers=True,
        flock=FakeFlock(),
        conflict_checker=_no_conflict,
    )
    assert code == 0, "an explicit SIGTERM is a clean stop, not a crash"


def test_busy_core_lock_returns_tempfail(tmp_path):
    fake = FakeFlock()
    lock = FileLock(str(tmp_path / "run" / "core.lock"), label="core", flock=fake)
    lock.acquire()
    try:
        assert _run(tmp_path, 0, flock=fake) == runner.EXIT_TEMPFAIL
    finally:
        lock.release()


def test_missing_binary_is_a_config_error(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    code = runner.run(
        binary=str(tmp_path / "missing-mihomo"),
        state_dir=str(state),
        core_lock=str(tmp_path / "core.lock"),
        popen=lambda *args, **kw: FakePopen(0),
        install_signal_handlers=False,
        flock=FakeFlock(),
        conflict_checker=_no_conflict,
    )
    assert code == runner.EXIT_CONFIG


def test_runner_exits_zero_without_spawn_when_conflict(tmp_path):
    """launchd must not retry: a conflict is a *successful* (0) exit."""
    spawned: list = []
    code = runner.run(
        binary=str(_binary(tmp_path)),
        state_dir=str(tmp_path / "state"),
        core_lock=str(tmp_path / "run" / "core.lock"),
        popen=lambda *args, **kw: spawned.append(args) or FakePopen(0),
        install_signal_handlers=False,
        flock=FakeFlock(),
        conflict_checker=lambda: ConflictReport(
            ok=False, reasons=["外部 VPN 已连接：Nextin"]
        ),
    )
    assert code == 0
    assert spawned == []


def test_runner_treats_a_broken_check_as_conflict(tmp_path):
    spawned: list = []

    def broken() -> ConflictReport:
        raise RuntimeError("scutil exploded")

    code = runner.run(
        binary=str(_binary(tmp_path)),
        state_dir=str(tmp_path / "state"),
        core_lock=str(tmp_path / "run" / "core.lock"),
        popen=lambda *args, **kw: spawned.append(args) or FakePopen(0),
        install_signal_handlers=False,
        flock=FakeFlock(),
        conflict_checker=broken,
    )
    assert code == 0
    assert spawned == []


def test_skip_conflict_check_still_spawns(tmp_path):
    spawned: list = []
    code = runner.run(
        binary=str(_binary(tmp_path)),
        state_dir=str(tmp_path / "state"),
        core_lock=str(tmp_path / "run" / "core.lock"),
        popen=lambda *args, **kw: spawned.append(args) or FakePopen(0),
        install_signal_handlers=False,
        flock=FakeFlock(),
        skip_conflict_check=True,
    )
    assert code == 0
    assert len(spawned) == 1
