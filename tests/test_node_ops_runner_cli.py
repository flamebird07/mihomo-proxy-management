"""node_ops: Feishu-row driven pipeline, dry-run zero write, source agnosticism."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_ops import schema  # noqa: E402
from node_ops import __main__ as cli  # noqa: E402
from node_ops import runner  # noqa: E402
from node_ops.lock import FileLock, LockBusy  # noqa: E402
from node_ops.logutil import Logger  # noqa: E402
from node_ops.probe import ProbeResult  # noqa: E402
from node_ops.runner import RunError, Settings, entries_from_rows, run  # noqa: E402


def config(name, server="1.1.1.1", ptype="trojan"):
    return {"name": name, "type": ptype, "server": server, "port": 443, "password": "pw"}


def node_row(name="us-01", source="飞兔", number=1, server="1.1.1.1", **measurement):
    fields = {
        "节点名": name,
        "来源": source,
        "源编号": number,
        "协议": "trojan",
        "节点配置JSON": json.dumps(config(name, server), ensure_ascii=False),
        "同步时间": 1_600_000_000_000,
    }
    fields.update(measurement)
    return {"record_id": f"rec-{name}", "fields": fields}


def settings(**kwargs):
    base = dict(log_file=None)
    base.update(kwargs)
    return Settings(**base)


class FakeProber:
    def __init__(self):
        self.seen = []

    def probe(self, nodes, names=None):
        self.seen = names
        delays = {name: 100 for name in names}
        return ProbeResult(delays=delays, chatgpt=delays, openai=delays,
                           regions={name: "美国/加利福尼亚" for name in names})


class FakeClient:
    def __init__(self, node_rows=None, valid_rows=None):
        self.node_rows = list(node_rows or [])
        self.valid_rows = list(valid_rows or [])
        self.calls = []
        self.tables = [
            {"name": schema.NODES_TABLE_NAME, "table_id": "tbl-n"},
            {"name": schema.VALID_TABLE_NAME, "table_id": "tbl-v"},
        ]

    def list_tables(self):
        return list(self.tables)

    def list_fields(self, table_id):
        spec = schema.NODES_TABLE if table_id == "tbl-n" else schema.VALID_TABLE
        return [{"field_id": f"fld-{name}", "field_name": name, "type": ftype,
                 "property": prop} for name, ftype, prop in spec["fields"]]

    def list_records(self, table_id):
        self.calls.append(("list_records", table_id))
        return self.node_rows if table_id == "tbl-n" else self.valid_rows

    def batch_create(self, table_id, records):
        self.calls.append(("batch_create", table_id, records))
        return len(records)

    def batch_delete(self, table_id, record_ids):
        self.calls.append(("batch_delete", table_id, record_ids))
        return len(record_ids)

    def update_record(self, table_id, record_id, fields):
        self.calls.append(("update_record", table_id, record_id, fields))

    def create_table(self, name, fields):  # pragma: no cover - not expected
        raise AssertionError("unexpected create_table")

    def create_field(self, *args):  # pragma: no cover - not expected
        raise AssertionError("unexpected create_field")

    def update_field(self, *args):  # pragma: no cover - not expected
        raise AssertionError("unexpected update_field")


class EntryTests(unittest.TestCase):
    def test_entries_built_from_feishu_rows(self):
        rows = [node_row("us-01", "飞兔", 1),
                node_row("sg-01", "CoffeeCloud", 3, "2.2.2.2")]
        entries, skipped = entries_from_rows(rows, Logger(None))
        self.assertEqual(skipped, 0)
        self.assertEqual([(e.name, e.source, e.source_number) for e in entries],
                         [("us-01", "飞兔", 1), ("sg-01", "CoffeeCloud", 3)])
        self.assertEqual(len({e.probe_name for e in entries}), 2)

    def test_invalid_config_rows_are_skipped_and_counted(self):
        rows = [node_row("ok"),
                {"fields": {"节点名": "bad", "来源": "飞兔", "节点配置JSON": "{oops"}},
                {"fields": {"节点名": "missing"}},
                {"not": "a row"}]
        entries, skipped = entries_from_rows(rows, Logger(None))
        self.assertEqual([entry.name for entry in entries], ["ok"])
        self.assertEqual(skipped, 3)

    def test_duplicate_names_get_unique_probe_names(self):
        rows = [node_row("same", "飞兔", 1, "1.1.1.1"),
                node_row("same", "星辰", 2, "2.2.2.2")]
        entries, _ = entries_from_rows(rows, Logger(None))
        self.assertEqual([entry.probe_name for entry in entries],
                         ["same", "same [2]"])


class PipelineTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        client = FakeClient(node_rows=[node_row()])
        summary = run(settings(dry_run=True), Logger(None), client=client,
                      prober=FakeProber())
        self.assertTrue(summary.dry_run)
        self.assertEqual(summary.collected, 1)
        kinds = [call[0] for call in client.calls]
        self.assertNotIn("batch_create", kinds)
        self.assertNotIn("batch_delete", kinds)

    def test_missing_client_is_exit_2(self):
        with self.assertRaises(RunError) as caught:
            run(settings(dry_run=True), Logger(None), client=None, prober=FakeProber())
        self.assertEqual(caught.exception.exit_code, 2)

    def test_empty_total_table_is_exit_3(self):
        with self.assertRaises(RunError) as caught:
            run(settings(dry_run=True), Logger(None), client=FakeClient(),
                prober=FakeProber())
        self.assertEqual(caught.exception.exit_code, 3)

    def test_write_mode_replaces_both_tables(self):
        client = FakeClient(node_rows=[node_row("us-01")],
                            valid_rows=[{"fields": {"来源": "飞兔", "节点名": "us-01",
                                                    "连续通过轮数": 1}}])
        summary = run(settings(dry_run=False), Logger(None), client=client,
                      prober=FakeProber())
        self.assertEqual(summary.wrote_nodes, summary.collected)
        # Write discipline: the master table is UPDATED in place (measured
        # rows), the derived valid table is rebuilt via creates.
        updates = [call for call in client.calls if call[0] == "update_record"]
        self.assertEqual(len(updates), summary.collected)
        creates = [call for call in client.calls if call[0] == "batch_create"]
        self.assertEqual({call[1] for call in creates}, {"tbl-v"})
        valid_rows = [call[2] for call in creates if call[1] == "tbl-v"][0]
        self.assertTrue(valid_rows)
        self.assertTrue(all("GPT可用" in row["fields"] for row in valid_rows))

    def test_identity_and_source_number_preserved_verbatim(self):
        original = node_row("us-01", "飞兔", 1,
                            实际地区="旧地区", 延迟ms=999, 存活=True,
                            测速目标="old-target", 地区检测时间=123)
        client = FakeClient(node_rows=[original])
        run(settings(dry_run=False), Logger(None), client=client, prober=FakeProber())
        node_writes = [call for call in client.calls
                       if call[0] == "update_record" and call[1] == "tbl-n"]
        self.assertEqual(len(node_writes), 1)
        fields = node_writes[0][3]
        # Write-discipline ruling: identity columns are preserved verbatim
        # EXCEPT 同步时间 - the last-valid-rate clock advances with the
        # round's stamp when the probe produced a rate.
        for key in schema.NODES_IDENTITY_FIELDS:
            if key == "同步时间":
                continue
            self.assertEqual(fields[key], original["fields"][key], key)
        self.assertGreater(fields["同步时间"], original["fields"].get("同步时间", 0),
                           "valid rate advances the tombstone clock")
        self.assertEqual(fields["源编号"], 1)
        self.assertEqual(fields["延迟ms"], 100)
        self.assertEqual(fields["实际地区"], "美国/加利福尼亚")
        self.assertTrue(fields["存活"])
        self.assertEqual(fields["测速目标"], "grok/chatgpt/openai")

    def test_valid_rows_carry_source_number(self):
        client = FakeClient(node_rows=[node_row("us-01", "CoffeeCloud", 3)],
                            valid_rows=[{"fields": {"来源": "CoffeeCloud",
                                                    "节点名": "us-01",
                                                    "连续通过轮数": 1}}])
        run(settings(dry_run=False), Logger(None), client=client, prober=FakeProber())
        valid_rows = [call[2] for call in client.calls
                      if call[0] == "batch_create" and call[1] == "tbl-v"][0]
        self.assertEqual(valid_rows[0]["fields"]["源编号"], 3)

    def test_streak_restored_from_feishu_previous_rows(self):
        client = FakeClient(node_rows=[node_row("us-01")],
                            valid_rows=[{"fields": {"来源": "飞兔", "节点名": "us-01",
                                                    "连续通过轮数": 1}}])
        summary = run(settings(dry_run=False), Logger(None), client=client,
                      prober=FakeProber())
        self.assertEqual(summary.valid, 1)

    def test_streak_bootstraps_from_previous_total_row(self):
        client = FakeClient(node_rows=[
            node_row("us-01", chatgpt延迟ms=200, openai延迟ms=300),
        ])
        summary = run(settings(dry_run=False), Logger(None), client=client,
                      prober=FakeProber())
        self.assertEqual(summary.valid, 1)


class SourceAgnosticTests(unittest.TestCase):
    def test_no_subscription_or_app_extraction_entry_point(self):
        self.assertFalse(hasattr(runner, "collect_entries"))
        self.assertFalse(hasattr(runner, "fetcher"))
        self.assertNotIn("subscription", sys.modules.get("node_ops", None).__dict__)

    def test_runner_source_has_no_fetch_or_app_tokens(self):
        source = open(os.path.join(SCRIPTS, "node_ops", "runner.py"),
                      encoding="utf-8").read()
        for token in ("fetch_subscription", "convert_content", "source_settings",
                      "from_app", "ReadProcessMemory", "extract_source1"):
            self.assertNotIn(token, source)

    def test_cli_has_no_sources_env(self):
        parser = cli.build_parser()
        options = {action.dest for action in parser._actions}
        self.assertNotIn("sources_env", options)
        self.assertIn("dry_run", options)


class LockAndCliTests(unittest.TestCase):
    def test_second_lock_is_busy(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "node_ops.lock")
        first = FileLock(path)
        first.acquire()
        self.addCleanup(first.release)
        second = FileLock(path)
        with self.assertRaises(LockBusy):
            second.acquire()

    def test_cli_dry_run_prints_summary_and_cleans_lock(self):
        directory = tempfile.mkdtemp()
        lock = os.path.join(directory, "run.lock")
        log = os.path.join(directory, "run.log")
        fake_summary = runner.RunSummary(collected=2, probed=2, alive=1,
                                         valid=0, dry_run=True)
        argv = ["--dry-run", "--lock-file", lock, "--log-file", log]
        with patch.object(cli, "_credentials", return_value=object()):
            with patch.object(cli, "run", return_value=fake_summary):
                with patch.object(sys, "stdout") as stdout:
                    code = cli.main(argv)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(lock))
        self.assertTrue(any("collected=2" in str(call) for call in stdout.write.call_args_list))

    def test_cli_requires_credentials_even_for_dry_run(self):
        argv = ["--dry-run", "--lock-file", os.path.join(tempfile.mkdtemp(), "x.lock")]
        with patch.object(cli, "_credentials", return_value=None):
            code = cli.main(argv)
        self.assertEqual(code, 2)

    def test_module_help_runs(self):
        result = subprocess.run(
            [sys.executable, "-m", "node_ops", "--help"],
            cwd=SCRIPTS, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--ensure-tables", result.stdout)
        self.assertNotIn("--sources-env", result.stdout)


if __name__ == "__main__":
    unittest.main()
