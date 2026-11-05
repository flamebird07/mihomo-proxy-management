r"""Tests for the DIAN-13 log-blind-spot fix in the Feishu bitable sync.

Covered contracts:
  * the conflict itself: cmd's `>>` handle on the SAME file the Logger writes
    blocks Logger's open() (PermissionError errno=13). Reproduced for real here
    with a temporary bat, and shown to go away once the two files are separated.
  * a failed log write is no longer completely silent: the Logger reports
    LOG-DEGRADED on the stream the bat redirects, and counts the failures.
  * run-feishu-bitable-sync.bat never redirects into the Logger's own log file.
  * every round logs a timestamped start and an end carrying result + duration,
    for the ok / lock_busy / error outcomes.
  * the run state file holds exactly the required fields, accumulates
    consecutive_failures, keeps last_success_time across failures, and is written
    by atomic replace (no .tmp left behind).

No network and no real Feishu call: main() is driven down paths that fail before
the client is built. The scheduled task, C:\mihomo\config.yaml and C:\mihomo\logs
are never touched — everything happens under a TemporaryDirectory.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "sync-feishu-bitable.py")
BAT = os.path.join(HERE, os.pardir, "scripts", "run-feishu-bitable-sync.bat")

spec = importlib.util.spec_from_file_location("sync_feishu_bitable", SCRIPT)
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)

TS_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
STATE_FIELDS = {
    "last_start", "last_end", "last_result", "last_success_time",
    "last_error", "duration_sec", "consecutive_failures",
}

# Runs under the temporary bat: writes one Logger line, one stdout line, then
# reports whether the Logger line made it to its own file.
WORKER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("m", {script!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
log = m.Logger(sys.argv[1])
log.info("round marker")
print("STDOUT-SUMMARY")
sys.exit(9 if log.write_failures else 0)
"""


def write_bat(directory, name, python, worker, logger_path, redirect_path):
    bat = os.path.join(directory, name)
    body = (
        "@echo off\r\n"
        f'"{python}" "{worker}" "{logger_path}" >> "{redirect_path}" 2>&1\r\n'
        f"echo EXIT=%ERRORLEVEL%\r\n"
    )
    with open(bat, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    return bat


def run_bat(bat):
    return subprocess.run(
        ["cmd.exe", "/c", bat], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)


@unittest.skipUnless(sys.platform.startswith("win"), "cmd.exe redirection is Windows-only")
class SameFileConflictTests(unittest.TestCase):
    """The bug, reproduced through a real cmd `>>`, and then shown to be fixed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.worker = os.path.join(self.tmp, "worker.py")
        with open(self.worker, "w", encoding="utf-8") as handle:
            handle.write(WORKER.format(script=SCRIPT))

    def _read(self, path):
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def test_conflict_reproduces_when_bat_shares_the_log_file(self):
        shared = os.path.join(self.tmp, "shared.log")
        bat = write_bat(self.tmp, "conflict.bat", sys.executable, self.worker, shared, shared)
        result = run_bat(bat)

        text = self._read(shared)
        self.assertIn("STDOUT-SUMMARY", text, "the bat redirect itself must still work")
        self.assertNotIn("INFO round marker", text,
                         "Logger must not reach a file cmd is holding open")
        self.assertIn("LOG-DEGRADED", text,
                      "a blocked log write must announce itself, never pass silently")
        self.assertEqual(result.stdout.count("LOG-DEGRADED"), 0)
        self.assertIn("EXIT=9", result.stdout)

    def test_fixed_when_bat_redirects_elsewhere(self):
        logger_log = os.path.join(self.tmp, "sync.log")
        stdout_log = os.path.join(self.tmp, "sync.stdout.log")
        bat = write_bat(self.tmp, "fixed.bat", sys.executable, self.worker,
                        logger_log, stdout_log)
        result = run_bat(bat)

        logged = self._read(logger_log).splitlines()
        self.assertEqual(len(logged), 1)
        self.assertRegex(logged[0], TS_LINE.pattern + r"INFO round marker$")
        self.assertIn("STDOUT-SUMMARY", self._read(stdout_log))
        self.assertNotIn("LOG-DEGRADED", self._read(stdout_log))
        self.assertIn("EXIT=0", result.stdout)


class BatEntrypointTests(unittest.TestCase):
    def bat_redirect_for(self, needle):
        with open(BAT, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.lower().startswith(("rem ", "::")) or ">>" not in line:
                    continue
                if needle in line:
                    return line.split(">>", 1)[1].split()[0].strip()
        self.fail(f"no redirect line for {needle!r} in {BAT}")

    def test_sync_stdout_does_not_share_the_logger_log_file(self):
        redirected = self.bat_redirect_for("sync-feishu-bitable.py")
        self.assertNotEqual(os.path.basename(redirected).lower(),
                            os.path.basename(sync.DEFAULT_LOG_FILE).lower())
        self.assertEqual(os.path.basename(redirected).lower(),
                         "feishu-bitable-sync.stdout.log")

    def test_sync_and_auto_switch_stdout_logs_are_distinct(self):
        self.assertNotEqual(self.bat_redirect_for("sync-feishu-bitable.py"),
                            self.bat_redirect_for("proxy-auto-switch.py"))


class LoggerDegradationTests(unittest.TestCase):
    def test_blocked_write_is_reported_and_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = sync.Logger(os.path.join(tmp, "sync.log"))
            stderr = io.StringIO()
            with patch.object(sync.Path, "open", side_effect=PermissionError(13, "Permission denied")):
                with redirect_stderr(stderr):
                    for _ in range(3):
                        log.info("attempt")
            self.assertEqual(log.write_failures, 3)
            self.assertIn("PermissionError(errno=13)", log.last_write_error)
            self.assertEqual(stderr.getvalue().count("LOG-DEGRADED"), 1,
                             "report once, then keep counting")

    def test_healthy_write_is_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sync.log")
            log = sync.Logger(path)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                log.info("fine")
            self.assertEqual(log.write_failures, 0)
            self.assertEqual(stderr.getvalue(), "")
            with open(path, encoding="utf-8") as handle:
                self.assertRegex(handle.read().strip(), TS_LINE.pattern + r"INFO fine$")


def base_argv(tmp):
    return [
        "--log-file", os.path.join(tmp, "sync.log"),
        "--sync-state-file", os.path.join(tmp, "state", "feishu-bitable-sync-state.json"),
        "--env-file", os.path.join(tmp, "missing.env"),
        "--state-file", os.path.join(tmp, "missing-tables.json"),
        "--lock-file", os.path.join(tmp, "sync.lock"),
    ]


class StubLock:
    """Stands in for SyncLock so tests never touch the global Windows mutex."""

    def __init__(self, path, log, *, mutex_name=None):
        self.path = path

    def acquire(self):
        return self

    def release(self):
        pass


class BusyLock(StubLock):
    def acquire(self):
        raise sync.SyncError("lock_busy", "holder_pid=4321")


class RunRecordTests(unittest.TestCase):
    """Each round must leave a timestamped start/end and a state file."""

    def run_main(self, tmp, argv, lock_cls=StubLock):
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sync, "cleanup_temp_corpses", lambda log, **k: (0, 0)))
            stack.enter_context(patch.object(sync, "SyncLock", lock_cls))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            code = sync.main(base_argv(tmp) + argv)
        with open(os.path.join(tmp, "sync.log"), encoding="utf-8") as handle:
            logged = handle.read()
        state_path = os.path.join(tmp, "state", "feishu-bitable-sync-state.json")
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        return code, logged, state

    def test_lock_busy_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, logged, state = self.run_main(tmp, [], BusyLock)
            self.assertEqual(code, 1)
            self.assertRegex(logged, TS_LINE.pattern + r"INFO sync start")
            end = re.search(TS_LINE.pattern + r"INFO sync end .*$", logged, re.M)
            self.assertIsNotNone(end, logged)
            self.assertIn("result=lock_busy", end.group(0))
            self.assertIn("reason=holder_pid=4321", end.group(0))
            self.assertRegex(end.group(0), r"duration_sec=\d+(\.\d+)?")
            self.assertEqual(state["last_result"], "lock_busy")
            self.assertEqual(state["last_error"], "holder_pid=4321")
            self.assertEqual(state["consecutive_failures"], 1)
            self.assertEqual(state["last_success_time"], "")
            self.assertNotIn("LOG-DEGRADED", logged)

    def test_error_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, logged, state = self.run_main(tmp, [])
            self.assertEqual(code, 1)
            self.assertIn("result=error", logged)
            self.assertIn("reason=token_error", logged)
            self.assertEqual(state["last_result"], "error")
            self.assertEqual(state["last_error"], "token_error")
            self.assertEqual(state["consecutive_failures"], 1)

    def test_limit_guard_is_recorded_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, logged, _ = self.run_main(tmp, ["--limit", "3"])
            self.assertEqual(code, 2)
            self.assertIn("result=error", logged)
            self.assertIn("reason=limit_write_forbidden", logged)

    def test_dry_run_never_mirrors_the_state_file(self):
        """--dry-run records its round but must not touch last_success_time."""
        with tempfile.TemporaryDirectory() as tmp:
            stdout, stderr = io.StringIO(), io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(patch.object(sync, "cleanup_temp_corpses", lambda log, **k: (0, 0)))
                stack.enter_context(patch.object(sync, "SyncLock", StubLock))
                stack.enter_context(redirect_stdout(stdout))
                stack.enter_context(redirect_stderr(stderr))
                sync.main(base_argv(tmp) + ["--dry-run"])
            state_path = os.path.join(tmp, "state", "feishu-bitable-sync-state.json")
            self.assertFalse(os.path.exists(state_path))
            with open(os.path.join(tmp, "sync.log"), encoding="utf-8") as handle:
                logged = handle.read()
            self.assertIn("INFO sync start", logged)
            self.assertIn("INFO sync end", logged)
            self.assertFalse(os.path.exists(state_path))


class RunStateFileTests(unittest.TestCase):
    def fields(self, tmp):
        return os.path.join(tmp, "state", "nested", "feishu-bitable-sync-state.json")

    def record(self, log, state_file, code, result, reason="", mirror=True):
        recorder = sync.RunRecorder(state_file, log, mirror=mirror)
        recorder.log_start()
        return recorder.finish(code, result, reason)

    def test_ok_writes_every_field_and_is_the_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = sync.Logger(os.path.join(tmp, "sync.log"))
            state_file = self.fields(tmp)
            self.assertEqual(self.record(log, state_file, 0, "ok"), 0)
            state = sync.read_run_state(state_file)
            self.assertEqual(set(state), STATE_FIELDS)
            self.assertEqual(state["last_result"], "ok")
            self.assertEqual(state["last_error"], "")
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertRegex(state["last_start"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
            self.assertEqual(state["last_success_time"], state["last_end"])
            self.assertIsInstance(state["duration_sec"], float)
            self.assertGreaterEqual(state["duration_sec"], 0.0)
            self.assertFalse(os.path.exists(state_file + ".tmp"), "atomic replace leaves no temp")

    def test_failures_accumulate_and_success_time_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = sync.Logger(os.path.join(tmp, "sync.log"))
            state_file = self.fields(tmp)
            self.record(log, state_file, 0, "ok")
            first_success = sync.read_run_state(state_file)["last_success_time"]

            for expected in (1, 2):
                self.record(log, state_file, 1, "lock_busy", f"holder_pid={expected}")
                state = sync.read_run_state(state_file)
                self.assertEqual(state["consecutive_failures"], expected)
                self.assertEqual(state["last_success_time"], first_success,
                                 "a failure must not rewind or clear the last good sync")

            self.record(log, state_file, 1, "error", "token_error")
            state = sync.read_run_state(state_file)
            self.assertEqual(state["consecutive_failures"], 3)
            self.assertEqual(state["last_error"], "token_error")
            self.assertEqual(state["last_result"], "error")

            self.record(log, state_file, 0, "ok")
            state = sync.read_run_state(state_file)
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertEqual(state["last_error"], "")
            self.assertGreaterEqual(state["last_success_time"], first_success)

    def test_mirror_off_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = sync.Logger(os.path.join(tmp, "sync.log"))
            state_file = self.fields(tmp)
            self.record(log, state_file, 0, "ok", mirror=False)
            self.assertFalse(os.path.exists(state_file))

    def test_read_tolerates_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = self.fields(tmp)
            self.assertEqual(sync.read_run_state(missing), {})
            broken = os.path.join(tmp, "broken.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("not json{")
            self.assertEqual(sync.read_run_state(broken), {})

    def test_unwritable_state_file_does_not_crash_the_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = sync.Logger(os.path.join(tmp, "sync.log"))
            blocker = os.path.join(tmp, "blocked")
            os.makedirs(blocker)
            state_file = os.path.join(blocker, "sub", "state.json")
            with patch.object(sync.Path, "mkdir", side_effect=OSError(13, "Permission denied")):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = self.record(log, state_file, 1, "error", "token_error")
            self.assertEqual(code, 1)
            with open(os.path.join(tmp, "sync.log"), encoding="utf-8") as handle:
                self.assertIn("sync state write failed", handle.read())


if __name__ == "__main__":
    unittest.main()
