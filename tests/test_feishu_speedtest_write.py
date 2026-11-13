"""Tests for the split Feishu entrypoints in scripts/sync-feishu-bitable.py.

Covered contracts:
  * the daily default (no flags) is the full path: speed test on, 表1/表2/表3
  * the default speed-test target is the Grok homepage
  * run-feishu-bitable-sync.bat (daily task) never passes --no-speedtest or
    --speedtest-write; run-feishu-speedtest.bat is an optional manual entry
  * --speedtest-write parses, defaults off, and is mutually exclusive with
    --no-speedtest (argparse exits non-zero)
  * select_write_keys picks the tables each mode may replace:
      default + tested          -> sources + nodes + gpt_regions
      default + not tested      -> sources only (表2/表3 preserved)
      --no-speedtest            -> sources only
      --speedtest-write + tested -> nodes + gpt_regions only (never sources)
      --speedtest-write + not tested -> nothing at all
  * --limit stays forbidden for production writes in speedtest-write mode
  * main() with a mock Feishu client: --no-speedtest replaces 表1 only and
    never runs the speed test; --speedtest-write replaces 表2/表3 only via
    the existing test_isolated path; a speedtest-write run where both
    test_isolated and test_live fail replaces nothing (preserve gate).

No network: every I/O boundary in main() is patched.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "sync-feishu-bitable.py")

spec = importlib.util.spec_from_file_location("sync_feishu_bitable", SCRIPT)
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)

SOURCES = [{"源": "A"}]
NODES = [{"fields": {"节点名": "a1"}}]
REGIONS = [{"地区": "新加坡"}]


def keys_of(pairs):
    return [key for key, _records in pairs]


class SpeedtestWriteFlagTests(unittest.TestCase):
    def test_default_is_off(self):
        args = sync.parse_args([])
        self.assertFalse(args.speedtest_write)
        self.assertFalse(args.no_speedtest)

    def test_flag_parses(self):
        args = sync.parse_args(["--speedtest-write"])
        self.assertTrue(args.speedtest_write)
        self.assertFalse(args.no_speedtest)

    def test_no_speedtest_still_parses(self):
        args = sync.parse_args(["--no-speedtest"])
        self.assertTrue(args.no_speedtest)
        self.assertFalse(args.speedtest_write)

    def test_conflicting_flags_rejected(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stderr(stderr):
                sync.parse_args(["--speedtest-write", "--no-speedtest"])
        self.assertEqual(caught.exception.code, 2)
        message = stderr.getvalue()
        self.assertIn("--speedtest-write", message)
        self.assertIn("--no-speedtest", message)


class SelectWriteKeysTests(unittest.TestCase):
    def select(self, speedtest_ran, speedtest_write):
        return sync.select_write_keys(
            speedtest_ran, speedtest_write, SOURCES, NODES, REGIONS
        )

    def test_default_tested_writes_all_three(self):
        pairs = self.select(True, False)
        self.assertEqual(keys_of(pairs), ["sources", "nodes", "gpt_regions"])
        self.assertEqual(pairs[0][1], SOURCES)
        self.assertEqual(pairs[1][1], NODES)
        self.assertEqual(pairs[2][1], REGIONS)

    def test_default_untested_preserves_nodes_and_regions(self):
        self.assertEqual(keys_of(self.select(False, False)), ["sources"])

    def test_speedtest_write_never_touches_sources(self):
        pairs = self.select(True, True)
        self.assertEqual(keys_of(pairs), ["nodes", "gpt_regions"])
        self.assertNotIn(SOURCES, [records for _key, records in pairs])

    def test_speedtest_write_untested_writes_nothing(self):
        # Same preserve gate as the default mode: an unmeasured run must not
        # wipe 表2/表3, and this mode never owns 表1 either.
        self.assertEqual(self.select(False, True), [])

    def test_only_known_table_keys(self):
        for ran in (True, False):
            for write in (True, False):
                self.assertTrue(set(keys_of(self.select(ran, write))) <= set(sync.WRITE_TABLE_KEYS))


class LimitGuardTests(unittest.TestCase):
    def test_limit_with_speedtest_write_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "--limit", "3", "--speedtest-write",
                "--log-file", os.path.join(tmp, "sync.log"),
                "--env-file", os.path.join(tmp, "missing.env"),
                "--state-file", os.path.join(tmp, "missing-state.json"),
                "--sync-state-file", os.path.join(tmp, "sync-state.json"),
                "--lock-file", os.path.join(tmp, "sync.lock"),
            ]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = sync.main(argv)
        self.assertEqual(code, 2)
        self.assertIn("limit_write_forbidden", stdout.getvalue())


TABLES = {
    "sources": {"table_id": "tblS", "name": "表1"},
    "nodes": {"table_id": "tblN", "name": "表2"},
    "gpt_regions": {"table_id": "tblG", "name": "表3"},
}
POOL = [({"name": "n1", "type": "ss", "server": "1.2.3.4", "port": 8388}, "订阅源1")]
DELAYS = {"n1": 123}
REGION_BY_NODE = {"n1": "新加坡"}


class _StubLock:
    """Stands in for SyncLock so tests never touch the global Windows mutex."""

    instances: list = []

    def __init__(self, path, log, *, mutex_name=None):
        self.path = path
        self.mutex_name = mutex_name
        self.acquired = False
        _StubLock.instances.append(self)

    def acquire(self, timeout_ms=0):
        self.acquired = True
        self.timeout_ms = timeout_ms
        return self

    def release(self):
        self.acquired = False


class MainFlowMockClientTests(unittest.TestCase):
    def run_main(self, tmp, extra_argv, isolated_side_effect=None):
        """Run sync.main() with every I/O boundary patched; return (rc, state)."""
        env_file = os.path.join(tmp, "feishu.env")
        with open(env_file, "w", encoding="utf-8") as handle:
            handle.write("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret\n")
        argv = [
            "--log-file", os.path.join(tmp, "sync.log"),
            "--env-file", env_file,
            "--sources-env", os.path.join(tmp, "missing-sources.env"),
            "--live-env", os.path.join(tmp, "missing-live.env"),
            "--state-file", os.path.join(tmp, "missing-state.json"),
            "--sync-state-file", os.path.join(tmp, "sync-state.json"),
            "--gpt-history-file", os.path.join(tmp, "gpt-history.json"),
            "--lock-file", os.path.join(tmp, "sync.lock"),
        ] + extra_argv

        replaced: list[tuple[str, list]] = []
        events = []
        isolated = MagicMock(
            return_value=(dict(DELAYS), dict(REGION_BY_NODE)),
            side_effect=isolated_side_effect,
        )
        live = MagicMock(side_effect=sync.SyncError("live_fail", "test"))
        client_cls = MagicMock(name="FeishuClient")
        _StubLock.instances = []

        def fake_collect(index, url, ua, timeout, proxy, log):
            result = sync.SourceResult(index, f"sub{index}")
            result.result = "成功"
            result.nodes = [dict(POOL[0][0])]
            return result

        with ExitStack() as stack:
            p = lambda name, value: stack.enter_context(  # noqa: E731
                patch.object(sync, name, value))
            p("cleanup_temp_corpses", lambda log, **kwargs: (0, 0))
            p("FeishuClient", client_cls)
            p("ensure_tables", lambda client, state_file, keep, log: dict(TABLES))
            p("collect_source", fake_collect)
            p("write_source_cache", lambda *a, **k: None)
            p("build_pool", lambda results, log=None: [(dict(POOL[0][0]), POOL[0][1])])
            def run_isolated(*args, **kwargs):
                events.append("speedtest")
                result = isolated(*args, **kwargs)
                if kwargs.get("on_delays") is not None:
                    kwargs["on_delays"](result[0])
                events.append("geo")
                return result

            p("test_isolated", run_isolated)
            p("test_live", live)
            p("load_group_members", lambda config, log: {})
            p("build_node_records", lambda *a, **k: [{"fields": dict(row["fields"])} for row in NODES])
            p("build_region_records", lambda *a, **k: list(REGIONS))
            def record_write(client, table_id, records, log, **kwargs):
                events.append(table_id)
                replaced.append((table_id, records))
                return len(records)

            p("replace_records", record_write)
            p("print_summary", lambda *a, **k: None)
            p("SyncLock", _StubLock)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = sync.main(argv)
        return code, {
            "replaced_ids": [table_id for table_id, _ in replaced],
            "replaced": replaced,
            "events": events,
            "isolated": isolated,
            "live": live,
            "client": client_cls,
            "locks": list(_StubLock.instances),
        }

    def test_full_sync_publishes_sources_before_speedtest(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, state = self.run_main(tmp, [])
        self.assertEqual(code, 0)
        self.assertEqual(state["events"], ["tblS", "speedtest", "tblS", "geo", "tblN", "tblG"])
        early, final = state["replaced"][:2]
        self.assertNotIn("存活数", early[1][0]["fields"])
        self.assertIn("存活数", final[1][0]["fields"])
        self.assertGreaterEqual(
            state["replaced"][2][1][0]["fields"]["同步时间"],
            final[1][0]["fields"]["同步时间"],
        )

    def test_no_speedtest_writes_sources_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, state = self.run_main(tmp, ["--no-speedtest"])
        self.assertEqual(code, 0)
        self.assertEqual(state["replaced_ids"], ["tblS"])
        state["isolated"].assert_not_called()
        state["live"].assert_not_called()
        # Write-section locks (ensure_tables + table replace) are released.
        self.assertGreaterEqual(len(state["locks"]), 1)
        self.assertTrue(all(not lock.acquired for lock in state["locks"]))

    def test_speedtest_write_writes_nodes_and_regions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, state = self.run_main(tmp, ["--speedtest-write"])
        self.assertEqual(code, 0)
        self.assertEqual(state["replaced_ids"], ["tblN", "tblG"])
        state["isolated"].assert_called_once()
        state["client"].assert_called_once()  # mock client constructed once

    def test_speedtest_write_with_failed_speedtest_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, state = self.run_main(
                tmp, ["--speedtest-write"],
                isolated_side_effect=sync.SyncError("core_fail", "test"),
            )
        self.assertEqual(code, 0)
        self.assertEqual(state["replaced_ids"], [])
        state["isolated"].assert_called_once()
        state["live"].assert_called_once()  # existing fallback path reused


class DailyEntrypointTests(unittest.TestCase):
    """The scheduled daily path must measure delays; grok.com is the target."""

    SCRIPT_DIR = os.path.join(HERE, os.pardir, "scripts")

    def test_default_target_is_grok_homepage(self):
        self.assertEqual(sync.DEFAULT_TARGET, "https://grok.com/")
        self.assertEqual(sync.parse_args([]).target, "https://grok.com/")

    def test_daily_bat_runs_full_sync(self):
        path = os.path.join(self.SCRIPT_DIR, "run-feishu-bitable-sync.bat")
        with open(path, encoding="utf-8") as fh:
            lines = [line.strip().lower() for line in fh]
        commands = [line for line in lines if line.startswith("python ")]
        sync_cmd = [line for line in commands if "sync-feishu-bitable.py" in line]
        self.assertEqual(len(sync_cmd), 1)
        self.assertNotIn("--no-speedtest", sync_cmd[0])
        self.assertNotIn("--speedtest-write", sync_cmd[0])

    def test_manual_speedtest_bat_is_optional(self):
        path = os.path.join(self.SCRIPT_DIR, "run-feishu-speedtest.bat")
        with open(path, encoding="utf-8") as fh:
            lines = [line.strip().lower() for line in fh]
        commands = [line for line in lines if line.startswith("python ")]
        self.assertEqual(len(commands), 1)
        self.assertIn("--speedtest-write", commands[0])
        self.assertNotIn("--no-speedtest", commands[0])


if __name__ == "__main__":
    unittest.main()
