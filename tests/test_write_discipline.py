"""Write-discipline ruling (user, 2026-11-20): two-function isolation.

* fetch-side writers (node_provider collect; the sync chain's table writes)
  only APPEND new rows and UPDATE existing rows - they have NO delete
  capability driven by their payload;
* the measuring/switch side (node_ops; the sync chain's speedtest) writes
  rate / actual-region / usability columns only;
* 同步时间 is the tombstone clock = LAST VALID RATE; failed probes do not
  advance it;
* the ONLY delete anywhere is the time tombstone: a row with no valid data
  for 7 days (FEISHU_STALE_NODE_MINUTES, hour-accurate) - never a payload
  diff, never another host's source set.

These tests pin the sync-chain half; tests/test_node_provider_collect.py,
test_node_provider_collect_state.py and test_node_ops_runner_cli.py pin the
pipeline halves.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-feishu-bitable.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("sync_discipline", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

NOW_MS = 1_700_000_000_000
DAY_MS = 24 * 60 * 60 * 1000


class _Client:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def list_records(self, _table_id):
        return [dict(row) for row in self.rows]

    def batch_create(self, _table_id, records):
        self.calls.append(("create", [r["fields"]["节点名"] for r in records]))
        for record in records:
            self.rows.append({"record_id": f"new{len(self.rows)}",
                              "fields": dict(record["fields"])})
        return len(records)

    def update_records(self, _table_id, items):
        self.calls.append(("update", [i["fields"]["节点名"] for i in items]))
        by_id = {row["record_id"]: row for row in self.rows}
        for item in items:
            by_id[item["record_id"]]["fields"].update(item["fields"])
        return len(items)

    def batch_delete(self, _table_id, ids):
        self.calls.append(("delete", list(ids)))
        self.rows = [row for row in self.rows if row["record_id"] not in ids]
        return len(ids)


def _row(rid, name, source, *, path="订阅 Mihomo", age_ms=10 * 60 * 1000):
    return {"record_id": rid,
            "fields": {"节点名": name, "来源": source, "测速路径": path,
                       "同步时间": NOW_MS - age_ms if age_ms is not None else None}}


def _payload(name, source, *, valid=True, delay=120):
    fields = {"节点名": name, "来源": source, "测速路径": "订阅 Mihomo",
              "延迟ms": delay if valid else -1, "存活": bool(valid)}
    if valid:
        fields["同步时间"] = NOW_MS
    return {"fields": fields}


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    warn = error = info


class UpsertDisciplineTests(unittest.TestCase):
    def upsert(self, rows, records, table_key="nodes"):
        client = _Client(rows)
        log = _Log()
        counts = sync.upsert_records(client, "tbl", records, log,
                                     table_key=table_key, now_ms=NOW_MS)
        return client, counts

    def test_mac_payload_never_deletes_foreign_sources(self):
        """The ruling's headline: another host's rows survive any payload."""
        rows = [_row("w1", "cc-1", "CoffeeCloud"), _row("w2", "xg-1", "西瓜加速")]
        client, counts = self.upsert(rows, [_payload("ft-1", "飞兔")])
        self.assertEqual(counts["deleted"], 0)
        self.assertEqual([c[0] for c in client.calls], ["create"])
        self.assertEqual(len(client.rows), 3, "both foreign rows kept")

    def test_matching_row_is_updated_not_recreated(self):
        rows = [_row("m1", "ft-1", "飞兔")]
        client, counts = self.upsert(rows, [_payload("ft-1", "飞兔")])
        self.assertEqual(counts, {"created": 0, "updated": 1, "deleted": 0})
        self.assertEqual([c[0] for c in client.calls], ["update"])
        self.assertEqual(len(client.rows), 1, "no duplicate rows")

    def test_valid_rate_advances_the_clock_failed_rate_does_not(self):
        """同步时间 = last valid rate: the upsert writes it only when the
        builder produced one (failed rounds omit the field)."""
        rows = [_row("m1", "ft-1", "飞兔", age_ms=3 * DAY_MS),
                _row("m2", "ft-2", "飞兔", age_ms=3 * DAY_MS)]
        client, _ = self.upsert(rows, [_payload("ft-1", "飞兔", valid=True),
                                       _payload("ft-2", "飞兔", valid=False)])
        fields = {row["fields"]["节点名"]: row["fields"] for row in client.rows}
        self.assertEqual(fields["ft-1"]["同步时间"], NOW_MS,
                         "valid rate refreshes the clock")
        self.assertEqual(fields["ft-2"]["同步时间"], NOW_MS - 3 * DAY_MS,
                         "failed rate keeps the old clock")

    def test_tombstone_deletes_only_rows_without_data_for_the_window(self):
        rows = [_row("fresh", "ft-1", "飞兔", age_ms=1 * DAY_MS),
                _row("stale", "ft-2", "飞兔", age_ms=8 * DAY_MS),
                _row("unreadable", "ft-3", "飞兔", age_ms=None)]
        client, counts = self.upsert(rows, [])
        self.assertEqual(counts["deleted"], 1)
        names = {row["fields"]["节点名"] for row in client.rows}
        self.assertEqual(names, {"ft-1", "ft-3"},
                         "stale clock deleted; unreadable clock fail-closed kept")

    def test_ninja_rows_keep_their_own_shorter_lifecycle(self):
        rows = [_row("n1", "cc-1", "CoffeeCloud", path="Ninja Desktop",
                     age_ms=2 * 60 * 60 * 1000)]  # 2h > ninja 60min window
        client, counts = self.upsert(rows, [])
        self.assertEqual(counts["deleted"], 1, "independent rows age on their own clock")

    def test_independent_path_rows_match_their_own_upsert(self):
        """Regression: independent-path rows were excluded from the match
        index, so every ninja/source1/source3 round appended a duplicate
        generation (created=N updated=0) instead of updating its own rows."""
        rows = [_row("n1", "cc-1", "CoffeeCloud", path="Ninja Desktop",
                     age_ms=10 * 60 * 1000)]  # fresh inside the 60-min window
        payload = {"fields": {"节点名": "cc-1", "来源": "CoffeeCloud",
                              "测速路径": "Ninja Desktop", "延迟ms": 90,
                              "存活": True, "同步时间": NOW_MS}}
        client, counts = self.upsert(rows, [payload])
        self.assertEqual(counts, {"created": 0, "updated": 1, "deleted": 0})
        self.assertEqual(len(client.rows), 1, "no duplicate generation")

    def test_create_without_clock_starts_the_tombstone_now(self):
        rows = []
        client, _ = self.upsert(rows, [_payload("brand-new", "飞兔", valid=False)])
        fields = client.rows[0]["fields"]
        self.assertEqual(fields["同步时间"], NOW_MS,
                         "a new row's window starts at creation even if the "
                         "first probe failed")

    def test_gpt_regions_and_sources_use_the_same_discipline(self):
        rows = [{"record_id": "g1", "fields": {"节点名": "us-1", "来源": "CoffeeCloud",
                                               "同步时间": NOW_MS - DAY_MS}}]
        client, counts = self.upsert(rows, [_payload("sg-1", "飞兔")],
                                     table_key="gpt_regions")
        self.assertEqual(counts["deleted"], 0)
        self.assertEqual(len(client.rows), 2)


class WindowConfigTests(unittest.TestCase):
    def test_default_and_env_override(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(sync.STALE_NODE_MINUTES_ENV, None)
            self.assertEqual(sync.stale_node_minutes(), 7 * 24 * 60)
        with mock.patch.dict(os.environ, {sync.STALE_NODE_MINUTES_ENV: "60"}):
            self.assertEqual(sync.stale_node_minutes(), 60)
        with mock.patch.dict(os.environ, {sync.STALE_NODE_MINUTES_ENV: "junk"}):
            self.assertEqual(sync.stale_node_minutes(), 7 * 24 * 60)


class BuilderClockTests(unittest.TestCase):
    def test_node_records_carry_clock_only_for_valid_rates(self):
        pool = [({"name": "alive", "type": "ss"}, "飞兔"),
                ({"name": "dead", "type": "ss"}, "飞兔")]
        delays = {"alive": 120, "dead": None}
        records = sync.build_node_records(pool, delays, "t", NOW_MS)
        by_name = {r["fields"]["节点名"]: r["fields"] for r in records}
        self.assertEqual(by_name["alive"]["同步时间"], NOW_MS)
        self.assertNotIn("同步时间", by_name["dead"],
                         "failed rate must not advance the tombstone clock")


class ResilienceTests(unittest.TestCase):
    """Slow-API windows must degrade a round, not kill it."""

    class _Flaky:
        def __init__(self, fail_first):
            self.fail_first = fail_first
            self.update_calls = 0

        def list_records(self, _table_id):
            return [{"record_id": "r1", "fields": {"来源": "飞兔", "节点名": "n1",
                                                   "同步时间": 1}}]

        def update_record(self, _table_id, _rid, _fields):
            self.update_calls += 1
            if self.update_calls <= self.fail_first:
                from node_ops.feishu import FeishuError
                raise FeishuError("timeout", "slow api")
            return None

        def batch_create(self, _table_id, records):
            return [f"new{i}" for i, _ in enumerate(records)]

    def test_ops_update_retries_transient_failures(self):
        import subprocess as _sp  # noqa: F401
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "node_ops"))
        node_ops_feishu = importlib.import_module("node_ops.feishu")
        client = self._Flaky(fail_first=2)  # fails twice, succeeds on 3rd
        lines = []

        def log(message):
            lines.append(message)

        wrote = node_ops_feishu.upsert_node_rows(
            client, "tbl", [{"fields": {"来源": "飞兔", "节点名": "n1",
                                        "存活": True, "同步时间": 99}}],
            now_ms=100, log=log)
        self.assertEqual(wrote, 1)
        self.assertEqual(client.update_calls, 3, "two retries then success")

    def test_ops_partial_failure_keeps_the_round(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "node_ops"))
        node_ops_feishu = importlib.import_module("node_ops.feishu")

        class HalfDead(self._Flaky):
            def update_record(self, table_id, rid, fields):
                self.update_calls += 1
                if rid == "r1":
                    from node_ops.feishu import FeishuError
                    # retries exhaust, row lost - but the round survives
                    raise FeishuError("timeout", "always slow")
                return None

            def list_records(self, _table_id):
                return [
                    {"record_id": "r1", "fields": {"来源": "飞兔", "节点名": "n1",
                                                   "同步时间": 1}},
                    {"record_id": "r2", "fields": {"来源": "飞兔", "节点名": "n2",
                                                   "同步时间": 1}},
                ]

        client = HalfDead(fail_first=0)
        lines = []

        wrote = node_ops_feishu.upsert_node_rows(
            client, "tbl",
            [{"fields": {"来源": "飞兔", "节点名": "n1", "存活": True}},
             {"fields": {"来源": "飞兔", "节点名": "n2", "存活": True}}],
            now_ms=100, log=lines.append)
        self.assertEqual(wrote, 1, "the healthy row still lands")
        self.assertTrue(any("failed=1" in line for line in lines))



class MasterClockTests(unittest.TestCase):
    def test_matched_row_clock_advances_on_valid_rate(self):
        """Regression (2026-11-20): _identity_fields preserves an existing
        row's 同步时间 verbatim, so without an explicit stamp the clock NEVER
        advanced on updates and every alive node would hit the 7-day
        tombstone. A valid rate must stamp THIS round; a failed rate must
        omit the clock so the previous value survives the in-place update."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "node_ops"))
        records_mod = importlib.import_module("node_ops.records")
        from node_ops.model import NodeEntry

        def entry(name):
            return NodeEntry(name=name, source="西瓜加速", source_number=3,
                             probe_name=name,
                             config={"name": name, "type": "trojan",
                                     "server": "203.0.113.1", "port": 443})

        class Result:
            delays = {"alive": 321, "dead": 0}
            regions = {}
            chatgpt = {}
            openai = {}

        previous = {
            ("西瓜加速", "alive"): {"节点名": "alive", "来源": "西瓜加速", "源编号": 3,
                                    "同步时间": 111},
            ("西瓜加速", "dead"): {"节点名": "dead", "来源": "西瓜加速", "源编号": 3,
                                   "同步时间": 111},
        }
        rows = records_mod.build_node_records(
            [entry("alive"), entry("dead")], Result(), sync_ms=999,
            region_ms=999, previous_rows=[{"fields": f} for f in previous.values()])
        by_name = {r["fields"]["节点名"]: r["fields"] for r in rows}
        self.assertEqual(by_name["alive"]["同步时间"], 999,
                         "valid rate advances the clock past the preserved 111")
        self.assertNotIn("同步时间", by_name["dead"],
                         "failed rate must not carry a clock")


if __name__ == "__main__":
    unittest.main()
