"""Core runner: holds the core lock, optionally wraps ``caffeinate -i``.

launchd restarts this job when it exits non-zero (``KeepAlive =
{SuccessfulExit: false}``).  The exit codes therefore carry meaning:

* mihomo exits ``0`` -> runner exits ``0`` -> launchd leaves it stopped;
* mihomo exits ``N != 0`` -> runner exits ``N`` -> launchd restarts the job;
* mihomo is killed by signal ``S`` (Popen reports ``-S``) -> runner exits
  ``128 + S`` -> launchd restarts the job;
* the runner itself is ``SIGKILL``-ed -> launchd sees an unsuccessful exit and
  restarts the job (the core lock is released by the kernel when the process
  dies, so the new instance can acquire it);
* an external VPN/route conflict (Nextin still connected) -> the runner prints
  the reason and exits ``0`` *without spawning mihomo*: launchd treats that as
  a successful exit and does not restart in a loop.  A broken check is treated
  as a conflict (fail-safe);
* an explicit ``stop`` uses ``launchctl bootout`` plus ``launchctl disable``,
  which removes the job and keeps it off at the next boot, so the restart
  policy cannot race it.

``caffeinate`` is *not* part of the LaunchDaemon: it is an explicit opt-in for
manual/foreground runs, and only ``caffeinate -i`` is ever built.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from typing import Callable, Sequence

from mpm_common.errors import FailClosed
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST

from . import conflict as conflict_mod
from .locking import FileLock, LockBusy

EXIT_TEMPFAIL = 75
EXIT_CONFIG = 78
CAFFEINATE_PATH = "/usr/bin/caffeinate"
TERMINATE_TIMEOUT = 10.0


def build_argv(
    binary: str,
    state_dir: str,
    *,
    config: str | None = None,
    caffeinate: bool = False,
    caffeinate_path: str = CAFFEINATE_PATH,
) -> list[str]:
    """``mihomo -d <state>`` (plus ``-f`` when given), optionally caffeinated.

    Only ``caffeinate -i`` is permitted: ``-s``/``-d``/``pmset`` would change
    system power behaviour, which this project never does.
    """
    base = [binary, "-d", state_dir]
    if config:
        base.extend(["-f", config])
    if caffeinate:
        return [caffeinate_path, "-i", *base]
    return base


def exit_code_for(returncode: int) -> int:
    """Map a child return code onto a launchd-visible runner exit code."""
    if returncode is None:  # pragma: no cover - Popen always reports a code here
        return 1
    if returncode < 0:
        return 128 + min(127, abs(int(returncode)))
    return int(returncode)


class _StopRequest:
    def __init__(self) -> None:
        self.requested = False
        self.signal_number = 0


def _install_handlers(child, stop: _StopRequest) -> dict[int, object]:
    previous: dict[int, object] = {}

    def handler(signum, _frame):
        stop.requested = True
        stop.signal_number = signum
        try:
            child.terminate()
        except Exception:  # pragma: no cover - child already gone
            pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass
    return previous


def _restore_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):  # pragma: no cover
            pass


def _conflict_verdict(checker) -> "conflict_mod.ConflictReport":
    """Run the injected checker; any error is a conflict (fail-safe)."""
    try:
        return checker()
    except Exception as exc:  # noqa: BLE001 - never start on a broken check
        return conflict_mod.ConflictReport(
            ok=False,
            error=HOST.text(str(exc) or type(exc).__name__)[:200],
            reasons=["conflict check failed; treating it as a conflict"],
        )


def run(
    *,
    binary: str,
    state_dir: str,
    core_lock: str,
    config: str | None = None,
    caffeinate: bool = False,
    argv: Sequence[str] | None = None,
    popen: Callable[..., object] | None = None,
    flock=None,
    install_signal_handlers: bool = True,
    terminate_timeout: float = TERMINATE_TIMEOUT,
    conflict_checker: Callable[[], "conflict_mod.ConflictReport"] | None = None,
    skip_conflict_check: bool = False,
) -> int:
    """Acquire the core lock, run mihomo, and translate its outcome."""
    # Boot path defence: launchd can start this job with Nextin already
    # connected.  Refuse to spawn a second stack and exit 0 (SuccessfulExit) so
    # launchd does not retry in a loop.
    if not skip_conflict_check:
        checker = conflict_checker or (lambda: conflict_mod.detect(Executor()))
        verdict = _conflict_verdict(checker)
        if not verdict.ok:
            sys.stderr.write(
                HOST.text("[runner] " + conflict_mod.describe(verdict)) + "\n"
            )
            return 0

    lock = FileLock(core_lock, label="core", flock=flock)
    try:
        lock.acquire()
    except LockBusy as exc:
        # Non-zero so launchd retries after ThrottleInterval; never steal a lock.
        sys.stderr.write(HOST.text(f"[runner] {exc.message}") + "\n")
        return EXIT_TEMPFAIL

    try:
        if not os.path.isfile(binary):
            sys.stderr.write(f"[runner] mihomo binary not found: {binary}\n")
            return EXIT_CONFIG
        if config and not os.path.isfile(config):
            sys.stderr.write(f"[runner] config not found: {config}\n")
            return EXIT_CONFIG

        command = list(argv) if argv is not None else build_argv(
            binary, state_dir, config=config, caffeinate=caffeinate
        )
        spawn = popen or subprocess.Popen
        child = spawn(
            command,
            cwd=state_dir,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            start_new_session=True,
        )
        stop = _StopRequest()
        previous = _install_handlers(child, stop) if install_signal_handlers else {}
        returncode = 0
        try:
            # Assign, never ``return`` here: the signal handlers are restored in
            # ``finally`` and the mapping below must still see ``stop.requested``.
            returncode = child.wait()
        finally:
            _restore_handlers(previous)

        if stop.requested:
            # An explicit SIGTERM/SIGINT is a clean stop: exit 0 so launchd
            # does not immediately restart the job.
            return 0
        # A negative wait status means the child died from a signal; mapping it
        # through ``exit_code_for`` (128 + signum) keeps launchd's
        # SuccessfulExit=false restart policy working.
        return exit_code_for(int(returncode))
    finally:
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpm_macos.runner",
        description="Run the mihomo core under a single-instance flock (launchd entry point).",
    )
    parser.add_argument("--binary", required=True, help="path to the mihomo binary")
    parser.add_argument("--state-dir", required=True, help="mihomo -d directory")
    parser.add_argument("--config", help="optional -f config path")
    parser.add_argument("--run-dir", required=True, help="directory holding core.lock")
    parser.add_argument(
        "--caffeinate",
        action="store_true",
        help="wrap with 'caffeinate -i' (explicit opt-in; never in the LaunchDaemon)",
    )
    parser.add_argument(
        "--skip-conflict-check",
        action="store_true",
        help="testing only: do not probe for an external VPN/route conflict",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(
            binary=args.binary,
            state_dir=args.state_dir,
            config=args.config,
            core_lock=os.path.join(args.run_dir, "core.lock"),
            caffeinate=bool(args.caffeinate),
            skip_conflict_check=bool(args.skip_conflict_check),
        )
    except FailClosed as exc:
        sys.stderr.write(HOST.text(f"[runner] {exc.message}") + "\n")
        return EXIT_CONFIG
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
