"""The CLI serialises every state-changing command behind the lifecycle lock.

The lock is injected (``Context.flock``), so these tests exercise the real
acquire/refuse path on Windows and Linux too, without ``fcntl``.
"""

from __future__ import annotations

import io

import pytest

from mpm_common.errors import ExitCode
from mpm_macos import cli, lifecycle
from mpm_macos.locking import LOCK_EX, LOCK_NB, LOCK_UN, FileLock

from macos_test_support import (
    FakeController,
    FakeFlock,
    FakeNetworkSetup,
    LaunchdHarness,
    TunObservation,
    make_context,
    prepare_install,
)

pytestmark = pytest.mark.macos

MUTATING = ("install", "configure", "start", "stop", "restart", "uninstall")
READ_ONLY = ("preflight", "status", "health")


def _context(tmp_path, flock):
    ctx = make_context(tmp_path)
    ctx.flock = flock
    prepare_install(ctx)
    return ctx


def _run(ctx, argv, *, stdout=None, stderr=None):
    return cli.main(
        argv,
        stdout=stdout or io.StringIO(),
        stderr=stderr or io.StringIO(),
        context_factory=lambda _args: ctx,
    )


@pytest.mark.parametrize("command", MUTATING)
def test_mutating_command_refuses_a_concurrent_instance(tmp_path, command):
    flock = FakeFlock()
    ctx = _context(tmp_path, flock)
    holder = FileLock(ctx.layout.lifecycle_lock, label="lifecycle", flock=flock)
    holder.acquire()
    stderr = io.StringIO()
    try:
        code = _run(ctx, [command, "--root", str(tmp_path), "--quiet"], stderr=stderr)
    finally:
        holder.release()

    assert code == ExitCode.FAIL_CLOSED
    assert "lifecycle lock" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_command_does_not_take_the_lifecycle_lock(tmp_path, command):
    flock = FakeFlock()
    ctx = _context(tmp_path, flock)
    holder = FileLock(ctx.layout.lifecycle_lock, label="lifecycle", flock=flock)
    holder.acquire()
    stderr = io.StringIO()
    try:
        code = _run(ctx, [command, "--root", str(tmp_path)], stderr=stderr)
    finally:
        holder.release()

    assert "lifecycle lock" not in stderr.getvalue()
    assert code == 0


def test_install_acquires_the_lock_once_and_releases_it(tmp_path):
    """install -> start -> verify must not re-enter the lock (no deadlock)."""
    flock = FakeFlock()
    ctx = _context(tmp_path, flock)
    harness = LaunchdHarness(ctx.executor)
    network = FakeNetworkSetup()
    ctx.executor.set("networksetup", network)
    ctx.controller_factory = lambda _secret: FakeController()
    ctx.tun_observer = _sequence(
        TunObservation(("en0",), "en0"), TunObservation(("en0", "utun7"), "utun7")
    )

    code = _run(ctx, ["install", "--root", str(tmp_path), "--quiet"])

    assert code == 0
    assert harness.running is True
    assert flock.calls.count(LOCK_EX | LOCK_NB) == 1
    assert flock.calls[-1] & LOCK_UN


def test_restart_holds_the_lock_across_stop_and_start(tmp_path):
    flock = FakeFlock()
    ctx = _context(tmp_path, flock)
    LaunchdHarness(ctx.executor)
    ctx.executor.set("networksetup", FakeNetworkSetup())
    ctx.controller_factory = lambda _secret: FakeController()
    ctx.tun_observer = _sequence(
        TunObservation(("en0",), "en0"), TunObservation(("en0", "utun7"), "utun7")
    )
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    code = _run(ctx, ["restart", "--root", str(tmp_path), "--quiet"])

    assert code == 0
    assert flock.calls.count(LOCK_EX | LOCK_NB) == 1
    assert flock.calls[-1] & LOCK_UN


def _sequence(*observations):
    iterator = iter(observations)
    return lambda: next(iterator)
