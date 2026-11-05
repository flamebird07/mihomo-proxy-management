import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_status_publish.py"
spec = importlib.util.spec_from_file_location("live_status_publish", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BEIJING_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)  # 18:00 Beijing
FAKE_SECRET = "SUPER-SECRET-TOKEN-DO-NOT-LEAK"
FAKE_SUB_URL = "https://sub.example.com/link?token=abc123"


def utcnow():
    return datetime.now(timezone.utc)


def iso(minutes, base=NOW, tz=timezone.utc):
    return (base - timedelta(minutes=minutes)).astimezone(tz).isoformat()


def member(name, usable=True, stale=False):
    return {"name": name, "last_delay_ms": 120 if usable else 0, "last_time": iso(1),
            "usable_by_history": usable, "stale": stale}


def group(members, now=None):
    return {"type": "Fallback", "now": now if now is not None else members[0]["name"],
            "member_count": len(members), "members": [m["name"] for m in members],
            "member_status": members,
            "usable_count_by_history": sum(1 for m in members if m["usable_by_history"])}


def make_collected(base=None, **overrides):
    """A healthy schema-v1 document; ippool updated_at is UTC like the real one."""
    if base is None:
        base = NOW
    doc = {
        "schema_version": 1,
        "collected_at": {"utc": base.isoformat(timespec="seconds"),
                         "beijing": base.astimezone(BEIJING_TZ).isoformat(timespec="seconds")},
        "ok": True,
        "api": {"base": "http://127.0.0.1:9090", "reachable": True, "errors": []},
        "groups": {
            "GPT": group([member("美国1｜AI通用"), member("美国2｜AI通用")]),
            "US-FAST": group([member("美国3｜快")]),
            "GROKBOT": group([member("机器人1")]),
            "GROKAPI": group([member("接口1")]),
        },
        "usable_count_note": "口径=history",
        "process": {"mihomo_pid": 4321, "mihomo_start_time": iso(600, base),
                    "boot_time": iso(10000, base)},
        "state_files": {
            # real ippool-collector writes UTC (now_iso -> timezone.utc)
            "ippool": {"path": "C:\\mihomo\\state\\xigua-ippool\\ippool.json", "host_count": 2,
                       "healthy_ip_count": 3, "ip_total": 5,
                       "updated_at": iso(5, base, tz=timezone.utc)},
            "feishu_gpt_history": {"path": "x", "version": 1, "node_count": 4},
            # real sync state writes naive local (Beijing) stamps
            "feishu_bitable_sync_state": {
                "path": "y",
                "summary": {"last_result": "ok",
                            "last_success_time": (base.astimezone(BEIJING_TZ)
                                                  - timedelta(minutes=10)).replace(
                                                      tzinfo=None).isoformat(),
                            "consecutive_failures": 0}},
        },
        "scheduled_tasks": {
            name: {"exists": True, "state": "Ready", "last_run_time": iso(3, base),
                   "last_task_result": 0, "next_run_time": iso(-27, base)}
            for name in mod.TASK_NAMES
        },
        "freshness_window_minutes": 15.0,
        "errors": [],
    }
    doc.update(overrides)
    return doc


def healthy_alert():
    return {"level": "正常", "level_code": 0, "summary": "各组正常：GPT 当前 美国1｜AI通用，可用 2 个",
            "reasons": [], "next_state": {}}


def degraded_alert():
    return {"level": "降级", "level_code": 1, "summary": "GPT 仅 1 个节点可用：美国1｜AI通用",
            "reasons": [
                {"code": "gpt_single_usable", "level": "降级", "level_code": 1,
                 "text": "GPT 仅 1 个节点可用：美国1｜AI通用"},
                {"code": "ippool_stale", "level": "降级", "level_code": 1,
                 "text": "IP 池超过 30 分钟未更新（距今 45 分钟）"}],
            "next_state": {}}


def build_row(collected, alert=None, now=NOW, **kwargs):
    return mod.build_row(collected, alert if alert is not None else healthy_alert(),
                         now=now, **kwargs)


class FakeClient:
    """Records every call; never touches the network."""

    def __init__(self, tables=None, fields=None, records=None):
        self.tables = [{"name": mod.TABLE_NAME, "table_id": "tblLive0001"}] if tables is None \
            else tables
        self.fields = ([{"field_name": name, "field_id": f"fld{i:03d}", "type": ftype}
                        for i, (name, ftype, _) in enumerate(mod.TABLE_FIELDS)]
                       if fields is None else fields)
        self.records = [] if records is None else records
        self.calls = []
        self.created_tables = []
        self.created_fields = []
        self.created_records = []
        self.updated = []

    def list_tables(self):
        self.calls.append(("list_tables",))
        return list(self.tables)

    def create_table(self, name, fields):
        self.calls.append(("create_table", name))
        self.created_tables.append((name, list(fields)))
        return "tblNew00001"

    def list_fields(self, table_id):
        self.calls.append(("list_fields", table_id))
        return list(self.fields)

    def create_field(self, table_id, name, ftype, prop):
        self.calls.append(("create_field", name))
        self.created_fields.append(name)

    def update_field(self, table_id, field_id, name, ftype, prop):
        self.calls.append(("update_field", name))

    def delete_field(self, table_id, field_id):
        self.calls.append(("delete_field", field_id))

    def list_records(self, table_id):
        self.calls.append(("list_records", table_id))
        return list(self.records)

    def create_record(self, table_id, fields):
        self.calls.append(("create_record", table_id))
        self.created_records.append(dict(fields))
        return "recNew00001"

    def update_record(self, table_id, record_id, fields):
        self.calls.append(("update_record", record_id))
        self.updated.append((record_id, dict(fields)))


def live_record(record_id="recLive0001", created_time=None, key="live"):
    record = {"record_id": record_id, "fields": {mod.F_KEY: key}}
    if created_time is not None:
        record["created_time"] = created_time
    return record


class FakeLock:
    def __init__(self, available=True):
        self.available = available
        self.acquired = 0
        self.released = 0
        self.path = None

    def __call__(self, path, log):
        self.path = path
        return self

    def acquire(self):
        self.acquired += 1
        return self.available

    def release(self):
        self.released += 1


class TempRunMixin:
    """Every path the script may touch is redirected into a temp dir."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_file = self.root / "logs" / "live-status-publish.log"
        self.lock_file = self.root / "live-status-publish.lock"
        self.alert_state = self.root / "state" / "publish-alert-state.json"
        self.publish_state = self.root / "state" / "publish-state.json"
        self.collect_file = self.root / "collected.json"

    def write_collect(self, doc):
        self.collect_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(self.collect_file)

    def base_argv(self, *extra):
        return ["--collect-json", str(self.collect_file),
                "--log-file", str(self.log_file), "--lock-file", str(self.lock_file),
                "--alert-state-file", str(self.alert_state),
                "--publish-state-file", str(self.publish_state), *extra]

    def run_main(self, argv, client=None, lock=None):
        clients = []

        def factory(args, log):
            if client is None:
                raise AssertionError("a Feishu client must not be built in this mode")
            clients.append(client)
            return client

        out = io.StringIO()
        with mock.patch.object(sys, "stdout", new=out):
            code = mod.main(argv, client_factory=factory,
                            lock_factory=lock if lock is not None else FakeLock())
        return code, out.getvalue(), clients

    def log_text(self):
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""


class BuildRowTests(unittest.TestCase):
    def test_healthy_row_has_every_field_and_fixed_note(self):
        row = build_row(make_collected())
        self.assertEqual(row[mod.F_KEY], "live")
        self.assertEqual(row[mod.F_LEVEL], "正常")
        self.assertEqual(row[mod.F_STALE], "否")
        self.assertEqual(row[mod.F_NOTE],
                         "生产实时状态，来源本机9090/proxies，非独立内核测速")
        self.assertEqual(row[mod.F_GPT_NOW], "美国1｜AI通用")
        self.assertEqual(row[mod.F_GPT_USABLE], 2)
        self.assertEqual(row[mod.F_USFAST_NOW], "美国3｜快")
        self.assertEqual(row[mod.F_GROKBOT_NOW], "机器人1")
        self.assertEqual(row[mod.F_GROKAPI_NOW], "接口1")
        self.assertEqual(row[mod.F_SYNC_FAILURES], 0)
        self.assertEqual(row[mod.F_HEALTHY_IPS], 3)
        self.assertEqual(row[mod.F_REASONS], "无")
        self.assertEqual(row[mod.F_UPDATED], "2026-11-05 18:00:00")
        for name, _, _ in mod.TABLE_FIELDS:
            self.assertIn(name, row, name)
        self.assertEqual(set(row), {name for name, _, _ in mod.TABLE_FIELDS})

    def test_ippool_utc_updated_at_is_converted_to_beijing(self):
        row = build_row(make_collected())
        # 09:55 UTC -> 17:55 Beijing
        self.assertEqual(row[mod.F_IPPOOL_UPDATED], "2026-11-05 17:55:00")

    def test_naive_timestamps_are_read_as_beijing(self):
        doc = make_collected()
        doc["state_files"]["ippool"]["updated_at"] = "2026-11-05T17:55:00"
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = "2026-11-05T17:50:00"
        row = build_row(doc)
        self.assertEqual(row[mod.F_IPPOOL_UPDATED], "2026-11-05 17:55:00")
        self.assertEqual(row[mod.F_SYNC_SUCCESS], "2026-11-05 17:50:00")

    def test_aware_sync_timestamp_is_converted_too(self):
        doc = make_collected()
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = iso(10, tz=timezone.utc)
        self.assertEqual(build_row(doc)[mod.F_SYNC_SUCCESS], "2026-11-05 17:50:00")

    def test_missing_times_render_unknown_not_empty(self):
        doc = make_collected()
        doc["state_files"]["ippool"]["updated_at"] = None
        doc["state_files"]["feishu_bitable_sync_state"] = None
        row = build_row(doc)
        self.assertEqual(row[mod.F_IPPOOL_UPDATED], "未知")
        self.assertEqual(row[mod.F_SYNC_SUCCESS], "未知")
        self.assertIsNone(row[mod.F_SYNC_FAILURES])
        self.assertEqual(row[mod.F_HEALTHY_IPS], 3)

    def test_update_time_names_the_collection_when_it_lags(self):
        collected_at = NOW - timedelta(minutes=5)
        row = build_row(make_collected(base=collected_at), now=NOW)
        self.assertEqual(row[mod.F_UPDATED],
                         "2026-11-05 18:00:00（数据采集于 2026-11-05 17:55:00）")
        fresh = build_row(make_collected(base=NOW), now=NOW)
        self.assertEqual(fresh[mod.F_UPDATED], "2026-11-05 18:00:00")

    def test_reasons_are_one_per_line(self):
        row = build_row(make_collected(), alert=degraded_alert())
        self.assertEqual(row[mod.F_LEVEL], "降级")
        self.assertEqual(row[mod.F_REASONS].splitlines(),
                         ["GPT 仅 1 个节点可用：美国1｜AI通用",
                          "IP 池超过 30 分钟未更新（距今 45 分钟）"])
        self.assertEqual(row[mod.F_SUMMARY], "GPT 仅 1 个节点可用：美国1｜AI通用")

    def test_task_summary_rendering(self):
        doc = make_collected()
        doc["scheduled_tasks"]["MihomoFeishuBitableSync"]["last_task_result"] = 0
        doc["scheduled_tasks"]["MihomoXiguaIpPool"]["last_task_result"] = 267008
        doc["scheduled_tasks"]["MihomoNinjaGrokFeishuSync"]["last_task_result"] = 1
        row = build_row(doc)
        self.assertEqual(row[mod.F_TASKS],
                         "MihomoFeishuBitableSync=0；MihomoXiguaIpPool=运行中(267008)；"
                         "MihomoNinjaGrokFeishuSync=失败(1)")
        doc["scheduled_tasks"]["MihomoXiguaIpPool"] = None
        doc["scheduled_tasks"]["MihomoNinjaGrokFeishuSync"]["last_task_result"] = None
        self.assertEqual(build_row(doc)[mod.F_TASKS],
                         "MihomoFeishuBitableSync=0；MihomoXiguaIpPool=缺失；"
                         "MihomoNinjaGrokFeishuSync=未知")

    def test_unreadable_groups_blank_their_cells(self):
        doc = make_collected()
        doc["groups"] = {name: None for name in ("GPT", "US-FAST", "GROKBOT", "GROKAPI")}
        row = build_row(doc)
        for name in (mod.F_GPT_NOW, mod.F_USFAST_NOW, mod.F_GROKBOT_NOW, mod.F_GROKAPI_NOW):
            self.assertEqual(row[name], "", name)
        self.assertIsNone(row[mod.F_GPT_USABLE])
        self.assertEqual(row[mod.F_KEY], "live")
        self.assertEqual(len(row), len(mod.TABLE_FIELDS))

    def test_collect_failed_row_is_severe_and_stale(self):
        doc = make_collected(ok=False)
        doc["api"]["reachable"] = False
        row = build_row(doc, alert={"level": "严重", "summary": "mihomo API 不通",
                                    "reasons": [{"text": "mihomo API 不通（控制器不可达）"}]},
                        last_ok_collected_at="2026-11-05 17:30:00")
        self.assertEqual(row[mod.F_LEVEL], "严重")
        self.assertEqual(row[mod.F_STALE], "是")
        self.assertEqual(row[mod.F_SUMMARY], "采集失败/数据陈旧，最后成功采集于 2026-11-05 17:30:00")
        self.assertIn("mihomo API 不通", row[mod.F_REASONS])

    def test_collect_failed_without_history_says_unknown(self):
        row = build_row(make_collected(ok=False))
        self.assertEqual(row[mod.F_SUMMARY], "采集失败/数据陈旧，最后成功采集于 未知")

    def test_failed_round_never_names_its_own_stamp_as_last_success(self):
        doc = make_collected(ok=False, base=NOW - timedelta(minutes=2))
        row = build_row(doc, last_ok_collected_at="2026-11-04 08:00:00")
        self.assertEqual(row[mod.F_SUMMARY], "采集失败/数据陈旧，最后成功采集于 2026-11-04 08:00:00")
        self.assertEqual(build_row(doc)[mod.F_SUMMARY], "采集失败/数据陈旧，最后成功采集于 未知")

    def test_garbage_and_empty_inputs_never_raise(self):
        for bad in ({}, None, [], "x", 42, {"ok": True}):
            row = build_row(bad)
            self.assertEqual(row[mod.F_KEY], "live")
            self.assertEqual(row[mod.F_LEVEL], "严重")
            self.assertEqual(row[mod.F_STALE], "是")
            self.assertEqual(row[mod.F_NOTE], mod.NOTE_TEXT)

    def test_old_but_ok_collection_is_stale_without_inventing_severity(self):
        doc = make_collected(base=NOW - timedelta(minutes=40))
        row = build_row(doc, alert=healthy_alert(), now=NOW)
        self.assertEqual(row[mod.F_STALE], "是")
        self.assertEqual(row[mod.F_LEVEL], "正常")

    def test_missing_collected_at_is_stale(self):
        doc = make_collected()
        doc["collected_at"] = None
        row = build_row(doc, alert=healthy_alert())
        self.assertEqual(row[mod.F_STALE], "是")
        self.assertEqual(row[mod.F_LEVEL], "严重")
        self.assertIn("采集失败/数据陈旧", row[mod.F_SUMMARY])

    def test_bad_alert_result_falls_back_to_severe(self):
        for bad in (None, {}, "x", {"level": "也许"}, {"level": "正常"}):
            row = build_row(make_collected(), alert=bad)
            self.assertIn(row[mod.F_LEVEL], ("正常", "严重"))
            self.assertEqual(row[mod.F_REASONS], "无")

    def test_row_never_carries_secret_style_strings(self):
        doc = make_collected(ok=False)
        doc["api"] = {"base": FAKE_SUB_URL, "reachable": False,
                      "errors": [FAKE_SECRET, f"Bearer {FAKE_SECRET}"]}
        doc["errors"] = [FAKE_SECRET]
        doc["usable_count_note"] = FAKE_SUB_URL
        blob = json.dumps(build_row(doc), ensure_ascii=False)
        self.assertNotIn(FAKE_SECRET, blob)
        self.assertNotIn("sub.example.com", blob)
        self.assertNotIn("Bearer", blob)

    def test_all_field_values_are_text_or_number_only(self):
        doc = make_collected()
        doc["groups"]["GPT"] = None
        for row in (build_row(make_collected()), build_row(doc)):
            for name, value in row.items():
                self.assertTrue(value is None or isinstance(value, (str, int)), (name, value))
                self.assertNotIsInstance(value, bool, name)


class UpsertTests(unittest.TestCase):
    def setUp(self):
        self.log = mod.Logger(os.devnull)

    def test_creates_when_no_live_row_exists(self):
        client = FakeClient(records=[live_record("recOther", key="其他")])
        action = mod.upsert_row(client, "tblLive0001", {"key": "live"}, self.log)
        self.assertEqual(action, "created")
        self.assertEqual(len(client.created_records), 1)
        self.assertEqual(client.updated, [])

    def test_updates_in_place_when_one_live_row_exists(self):
        client = FakeClient(records=[live_record("recA", key="other"),
                                     live_record("recLive", key="live")])
        action = mod.upsert_row(client, "tblLive0001", {"key": "live", "状态等级": "正常"},
                                self.log)
        self.assertEqual(action, "updated")
        self.assertEqual(client.created_records, [])  # an update never adds a row
        self.assertEqual([rid for rid, _ in client.updated], ["recLive"])
        self.assertEqual(client.updated[0][1]["状态等级"], "正常")

    def test_duplicate_live_rows_update_only_the_earliest(self):
        client = FakeClient(records=[
            live_record("recNew", created_time=1_800_000_000_000),
            live_record("recOld", created_time=1_700_000_000_000),
            live_record("recMid", created_time=1_750_000_000_000),
        ])
        logs = []
        log = mod.Logger(os.devnull)
        log.warn = logs.append
        action = mod.upsert_row(client, "tblLive0001", {"key": "live"}, log)
        self.assertEqual(action, "updated")
        self.assertEqual([rid for rid, _ in client.updated], ["recOld"])
        self.assertEqual(client.created_records, [])
        self.assertEqual(len(logs), 1)
        self.assertIn("duplicate", logs[0])
        self.assertIn("count=3", logs[0])

    def test_duplicate_without_created_time_uses_api_order(self):
        client = FakeClient(records=[live_record("recFirst"), live_record("recSecond")])
        mod.upsert_row(client, "tblLive0001", {"key": "live"}, self.log)
        self.assertEqual([rid for rid, _ in client.updated], ["recFirst"])

    def test_text_cell_segments_are_matched(self):
        record = {"record_id": "recSeg", "fields": {"key": [{"type": "text", "text": "li"},
                                                            {"type": "text", "text": "ve"}]}}
        client = FakeClient(records=[record])
        self.assertEqual([r["record_id"] for r in mod.find_key_records(client.records)],
                         ["recSeg"])
        mod.upsert_row(client, "tblLive0001", {"key": "live"}, self.log)
        self.assertEqual([rid for rid, _ in client.updated], ["recSeg"])

    def test_junk_records_are_ignored(self):
        junk = [None, "x", 42, {}, {"record_id": "r"}, {"fields": "nope"},
                {"record_id": "r2", "fields": {"key": 42}},
                {"record_id": "r3", "fields": {"key": "  live  "}}]
        self.assertEqual([r["record_id"] for r in mod.find_key_records(junk)], ["r3"])

    def test_create_strips_nulls_but_update_clears_stale_cells(self):
        row = {"key": "live", "GPT当前节点": "", "GPT可用数": None}
        created = FakeClient(records=[])
        mod.upsert_row(created, "tblLive0001", row, self.log)
        self.assertEqual(created.created_records, [{"key": "live", "GPT当前节点": ""}])

        updated = FakeClient(records=[live_record("recLive")])
        mod.upsert_row(updated, "tblLive0001", row, self.log)
        self.assertEqual(updated.updated, [("recLive", row)])  # null clears the old number

    def test_record_without_id_is_reported_not_crashing(self):
        client = FakeClient(records=[{"fields": {"key": "live"}}])
        with self.assertRaises(mod.PublishError) as ctx:
            mod.upsert_row(client, "tblLive0001", {"key": "live"}, self.log)
        self.assertEqual(ctx.exception.kind, "api_error")

    def test_resolve_table_id_by_name_and_override(self):
        client = FakeClient(tables=[{"name": "订阅源状态", "table_id": "tblA"},
                                    {"name": mod.TABLE_NAME, "table_id": "tblLive"}])
        self.assertEqual(mod.resolve_table_id(client, None, self.log), "tblLive")
        self.assertEqual(mod.resolve_table_id(client, "tblGiven", self.log), "tblGiven")
        self.assertEqual([c[0] for c in client.calls], ["list_tables"])
        missing = FakeClient(tables=[{"name": "订阅源状态", "table_id": "tblA"}])
        with self.assertRaises(mod.PublishError) as ctx:
            mod.resolve_table_id(missing, None, self.log)
        self.assertEqual(ctx.exception.kind, "table_missing")


class InitTests(unittest.TestCase):
    def setUp(self):
        self.log = mod.Logger(os.devnull)
        self.args = mock.Mock(table_id=None)

    def test_creates_table_with_the_full_field_spec_when_missing(self):
        client = FakeClient(tables=[{"name": "订阅源状态", "table_id": "tblA"}])
        table_id, added = mod.ensure_table(client, self.args, self.log)
        self.assertEqual(table_id, "tblNew00001")
        self.assertEqual(client.created_tables[0][0], mod.TABLE_NAME)
        self.assertEqual([f[0] for f in client.created_tables[0][1]],
                         [name for name, _, _ in mod.TABLE_FIELDS])
        self.assertEqual(added, [])  # a fresh table already has every field

    def test_existing_table_only_gains_missing_fields(self):
        present = [{"field_name": name, "field_id": f"fld{i}", "type": ftype}
                   for i, (name, ftype, _) in enumerate(mod.TABLE_FIELDS[:5])]
        client = FakeClient(fields=present)
        table_id, added = mod.ensure_table(client, self.args, self.log)
        self.assertEqual(table_id, "tblLive0001")
        self.assertEqual(client.created_tables, [])
        self.assertEqual(added, [name for name, _, _ in mod.TABLE_FIELDS[5:]])
        self.assertEqual([c[0] for c in client.calls
                          if c[0] in ("update_field", "delete_field")], [])

    def test_second_init_is_a_no_op(self):
        client = FakeClient()
        self.assertEqual(mod.ensure_table(client, self.args, self.log)[1], [])
        self.assertEqual(client.created_fields, [])
        self.assertEqual(client.created_tables, [])

    def test_table_id_override_skips_the_lookup(self):
        client = FakeClient(fields=[])
        args = mock.Mock(table_id="tblGiven")
        table_id, added = mod.ensure_table(client, args, self.log)
        self.assertEqual(table_id, "tblGiven")
        self.assertEqual(added, [name for name, _, _ in mod.TABLE_FIELDS])
        self.assertNotIn(("list_tables",), client.calls)


class CliTests(TempRunMixin, unittest.TestCase):
    def test_default_mode_is_dry_run_and_needs_no_client(self):
        self.write_collect(make_collected(base=utcnow()))
        code, out, clients = self.run_main(self.base_argv())
        self.assertEqual(code, 0)
        self.assertEqual(clients, [])
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["action"], "upsert")
        self.assertEqual(payload["table"], mod.TABLE_NAME)
        self.assertEqual(payload["row"][mod.F_KEY], "live")
        self.assertEqual(payload["row"][mod.F_LEVEL], "正常")
        self.assertFalse(self.publish_state.exists())  # dry-run leaves no state behind
        self.assertFalse(self.alert_state.exists())    # nor does it consume an alert round

    def test_dry_run_evaluates_in_process_without_consuming_a_round(self):
        zero = make_collected(base=utcnow())
        zero["groups"]["GPT"] = group([member("美国1｜AI通用", usable=False)])
        self.write_collect(zero)
        code, out, _ = self.run_main(self.base_argv())
        self.assertEqual(code, 0)
        row = json.loads(out)["row"]
        self.assertEqual(row[mod.F_LEVEL], "降级")          # evaluated, 待确认 on a first round
        self.assertIn("GPT 组暂无可用节点(待确认)", row[mod.F_REASONS])
        self.assertFalse(self.alert_state.exists())        # read-only: no counter consumed
        # the same document evaluated for real does persist its round state
        code, _, _ = self.run_main(self.base_argv("--apply"), client=FakeClient())
        self.assertEqual(code, 0)
        state = json.loads(self.alert_state.read_text(encoding="utf-8"))
        self.assertEqual(state["gpt_zero_usable_rounds"], 1)
        self.assertIn("last_collected_at", state)

    def test_alert_json_is_reused_instead_of_evaluating(self):
        self.write_collect(make_collected(base=utcnow()))
        alert_file = self.root / "alert.json"
        alert_file.write_text(json.dumps(degraded_alert(), ensure_ascii=False), encoding="utf-8")
        code, out, _ = self.run_main(self.base_argv("--alert-json", str(alert_file)))
        self.assertEqual(code, 0)
        row = json.loads(out)["row"]
        self.assertEqual(row[mod.F_LEVEL], "降级")
        self.assertEqual(len(row[mod.F_REASONS].splitlines()), 2)
        self.assertFalse(self.alert_state.exists())  # evaluation was skipped

    def test_alert_json_usable_falls_back_to_evaluating(self):
        self.write_collect(make_collected(base=utcnow()))
        bad = self.root / "alert.json"
        bad.write_text("{not json", encoding="utf-8")
        code, out, _ = self.run_main(self.base_argv("--alert-json", str(bad)))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["row"][mod.F_LEVEL], "正常")
        self.assertIn("alert json unusable", self.log_text())

    def test_collect_json_from_stdin(self):
        doc = make_collected(base=utcnow())
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(doc, ensure_ascii=False))):
            with mock.patch.object(sys, "stdout", new=io.StringIO()) as out:
                code = mod.main(self.base_argv("--collect-json", "-"),
                                client_factory=lambda *a: None, lock_factory=FakeLock())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["row"][mod.F_KEY], "live")

    def test_unusable_collect_json_yields_the_stale_row_and_exit_zero(self):
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        missing = self.root / "nope.json"
        for path in (str(bad), str(missing)):
            self.log_file.unlink(missing_ok=True)
            code, out, clients = self.run_main(
                ["--collect-json", path, "--log-file", str(self.log_file),
                 "--lock-file", str(self.lock_file), "--alert-state-file", str(self.alert_state),
                 "--publish-state-file", str(self.publish_state)])
            self.assertEqual(code, 0)
            row = json.loads(out)["row"]
            self.assertEqual(row[mod.F_LEVEL], "严重")
            self.assertEqual(row[mod.F_STALE], "是")
            self.assertIn("采集失败/数据陈旧，最后成功采集于", row[mod.F_SUMMARY])
            self.assertEqual(clients, [])
            self.assertIn("fallback row reason=collect_json_unusable", self.log_text())

    def test_apply_creates_the_row_when_absent(self):
        self.write_collect(make_collected(base=utcnow()))
        client = FakeClient(records=[])
        code, out, clients = self.run_main(self.base_argv("--apply"), client=client)
        self.assertEqual(code, 0)
        self.assertEqual(clients, [client])
        self.assertEqual(len(client.created_records), 1)
        self.assertEqual(client.created_records[0][mod.F_KEY], "live")
        self.assertEqual(client.updated, [])
        self.assertIn("row created", self.log_text())

    def test_apply_updates_the_existing_row_and_records_last_ok(self):
        self.write_collect(make_collected(base=utcnow()))
        client = FakeClient(records=[live_record("recLive")])
        code, _, _ = self.run_main(self.base_argv("--apply"), client=client)
        self.assertEqual(code, 0)
        self.assertEqual([rid for rid, _ in client.updated], ["recLive"])
        self.assertEqual(client.created_records, [])
        state = json.loads(self.publish_state.read_text(encoding="utf-8"))
        self.assertIn("last_ok_collected_at", state)
        self.assertIn("row updated", self.log_text())

    def test_apply_with_stale_data_does_not_record_last_ok(self):
        self.write_collect(make_collected(base=utcnow() - timedelta(hours=2)))
        client = FakeClient(records=[live_record("recLive")])
        code, out, _ = self.run_main(self.base_argv("--apply"), client=client)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.updated), 1)
        self.assertFalse(self.publish_state.exists())

    def test_last_ok_collected_at_feeds_the_next_failure_summary(self):
        self.write_collect(make_collected(base=utcnow()))
        client = FakeClient(records=[live_record("recLive")])
        self.run_main(self.base_argv("--apply"), client=client)
        recorded = json.loads(self.publish_state.read_text(encoding="utf-8"))[
            "last_ok_collected_at"]
        broken = make_collected(ok=False, base=utcnow())
        broken["collected_at"] = None  # nothing readable this round
        self.write_collect(broken)
        code, _, _ = self.run_main(self.base_argv("--apply"), client=client)
        self.assertEqual(code, 0)
        self.assertEqual(client.updated[-1][1][mod.F_SUMMARY],
                         f"采集失败/数据陈旧，最后成功采集于 {recorded}")

    def test_apply_uses_the_table_id_override(self):
        self.write_collect(make_collected(base=utcnow()))
        client = FakeClient()
        code, _, _ = self.run_main(self.base_argv("--apply", "--table-id", "tblGiven"),
                                   client=client)
        self.assertEqual(code, 0)
        self.assertEqual([c for c in client.calls if c[0] == "list_records"],
                         [("list_records", "tblGiven")])
        self.assertNotIn(("list_tables",), client.calls)

    def test_init_mode_provisions_without_collecting_or_writing_a_row(self):
        client = FakeClient(tables=[])
        code, out, _ = self.run_main(
            ["--init", "--log-file", str(self.log_file), "--lock-file", str(self.lock_file),
             "--alert-state-file", str(self.alert_state),
             "--publish-state-file", str(self.publish_state)], client=client)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "init")
        self.assertEqual(payload["table_id"], "tblNew00001")
        self.assertEqual(client.created_records, [])
        self.assertEqual(client.updated, [])
        self.assertFalse(self.collect_file.exists())

    def test_modes_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            with mock.patch.object(sys, "stderr", new=io.StringIO()):
                mod.build_parser().parse_args(["--init", "--apply"])

    def test_lock_busy_skips_the_round(self):
        self.write_collect(make_collected(base=utcnow()))
        lock = FakeLock(available=False)
        code, out, clients = self.run_main(self.base_argv("--apply"), client=FakeClient(),
                                           lock=lock)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(clients, [])
        self.assertEqual(lock.acquired, 1)
        self.assertEqual(lock.released, 0)
        self.assertIn("skipped_lock_busy", self.log_text())

    def test_lock_is_released_after_a_normal_round(self):
        self.write_collect(make_collected(base=utcnow()))
        lock = FakeLock()
        self.run_main(self.base_argv(), lock=lock)
        self.assertEqual((lock.acquired, lock.released), (1, 1))
        self.assertEqual(lock.path, str(self.lock_file))

    def test_client_failure_is_logged_and_exit_zero(self):
        self.write_collect(make_collected(base=utcnow()))

        def boom(args, log):
            raise mod.PublishError("credentials_missing", "FEISHU_APP_ID/FEISHU_APP_SECRET not set")

        out = io.StringIO()
        with mock.patch.object(sys, "stdout", new=out):
            code = mod.main(self.base_argv("--apply"), client_factory=boom,
                            lock_factory=FakeLock())
        self.assertEqual(code, 0)
        self.assertIn("credentials_missing", self.log_text())
        self.assertNotIn(FAKE_SECRET, self.log_text())

    def test_unexpected_exception_is_logged_with_traceback_and_exit_zero(self):
        self.write_collect(make_collected(base=utcnow()))
        client = FakeClient()

        def explode(table_id):
            raise RuntimeError("kaboom")

        client.list_records = explode
        code, _, _ = self.run_main(self.base_argv("--apply"), client=client)
        self.assertEqual(code, 0)
        text = self.log_text()
        self.assertIn("publish failed unexpectedly", text)
        self.assertIn("Traceback", text)
        self.assertIn("kaboom", text)

    def test_log_has_start_and_end_lines_without_secrets(self):
        self.write_collect(make_collected(base=utcnow()))
        self.run_main(self.base_argv())
        text = self.log_text()
        self.assertIn("start mode=dry-run", text)
        self.assertIn("end mode=dry-run result=dry_run_ok", text)
        self.assertIn("duration_sec=", text)
        for banned in (FAKE_SECRET, "tenant_access_token", "Bearer", "app_secret"):
            self.assertNotIn(banned, text)

    def test_dry_run_writes_nothing_but_the_log(self):
        self.write_collect(make_collected(base=utcnow()))
        before = {p.name for p in self.root.iterdir()}
        self.run_main(self.base_argv())
        after = {p.name for p in self.root.iterdir()}
        self.assertEqual(after - before, {"logs"})
        self.assertEqual([p.name for p in (self.root / "logs").iterdir()],
                         ["live-status-publish.log"])

    def test_log_dir_is_configurable_and_created(self):
        self.write_collect(make_collected(base=utcnow()))
        log_file = self.root / "elsewhere" / "nested" / "publish.log"
        code, _, _ = self.run_main(["--collect-json", str(self.collect_file),
                                    "--log-file", str(log_file),
                                    "--lock-file", str(self.lock_file),
                                    "--alert-state-file", str(self.alert_state),
                                    "--publish-state-file", str(self.publish_state)])
        self.assertEqual(code, 0)
        self.assertIn("start mode=dry-run", log_file.read_text(encoding="utf-8"))


class LockTests(TempRunMixin, unittest.TestCase):
    def test_real_lock_acquires_writes_meta_and_releases(self):
        log = mod.Logger(str(self.log_file))
        name = rf"Local\mihomo-live-status-publish-test-{os.getpid()}"
        lock = mod.PublishLock(str(self.lock_file), log, mutex_name=name)
        self.assertTrue(lock.acquire())
        try:
            self.assertTrue(self.lock_file.is_file())
            self.assertEqual(json.loads(self.lock_file.read_text(encoding="utf-8"))["pid"],
                             os.getpid())
        finally:
            lock.release()
        self.assertFalse(self.lock_file.exists())

    def test_release_is_safe_without_acquire(self):
        log = mod.Logger(str(self.log_file))
        mod.PublishLock(str(self.lock_file), log,
                        mutex_name=rf"Local\mihomo-lsp-test2-{os.getpid()}").release()

    def test_posix_style_file_lock_is_exclusive_and_takes_over_dead_holders(self):
        if sys.platform == "win32":
            self.skipTest("POSIX file-lock branch only")
        log = mod.Logger(str(self.log_file))
        first = mod.PublishLock(str(self.lock_file), log)
        self.assertTrue(first.acquire())
        second = mod.PublishLock(str(self.lock_file), log)
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()


class HelperTests(unittest.TestCase):
    def test_beijing_or_unknown(self):
        self.assertEqual(mod._beijing_or_unknown("2026-11-05T09:55:00+00:00"),
                         "2026-11-05 17:55:00")
        self.assertEqual(mod._beijing_or_unknown("2026-11-05T17:55:00"), "2026-11-05 17:55:00")
        for bad in (None, "", "   ", "not-a-time", 42, []):
            self.assertEqual(mod._beijing_or_unknown(bad), "未知")

    def test_long_fractional_seconds_are_parsed(self):
        self.assertEqual(mod._beijing_or_unknown("2026-11-05T09:55:00.1234567+00:00"),
                         "2026-11-05 17:55:00")

    def test_read_env_file_strips_quotes_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comment\nFEISHU_APP_ID='cli_x'\nFEISHU_APP_SECRET=\"s\"\n"
                            "broken-line\n", encoding="utf-8")
            values = mod.read_env_file(str(path))
            self.assertEqual(values["FEISHU_APP_ID"], "cli_x")
            self.assertEqual(values["FEISHU_APP_SECRET"], "s")
            self.assertNotIn("broken-line", values)
            self.assertEqual(mod.read_env_file(str(Path(tmp) / "missing")), {})

    def test_build_client_refuses_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("FEISHU_APP_ID=\n", encoding="utf-8")
            args = mock.Mock(env_file=str(env), app_token="app")
            with self.assertRaises(mod.PublishError) as ctx:
                mod.build_client(args, mod.Logger(os.devnull))
            self.assertEqual(ctx.exception.kind, "credentials_missing")

    def test_publish_state_round_trip_and_corrupt_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nested" / "state.json")
            self.assertEqual(mod.load_publish_state(path), {})
            mod.save_publish_state(path, {"last_ok_collected_at": "2026-11-05 18:00:00"})
            self.assertEqual(mod.load_publish_state(path)["last_ok_collected_at"],
                             "2026-11-05 18:00:00")
            Path(path).write_text("{corrupt", encoding="utf-8")
            self.assertEqual(mod.load_publish_state(path), {})
            leftovers = [p.name for p in Path(path).parent.iterdir()]
            self.assertEqual(leftovers, ["state.json"])

    def test_table_fields_are_text_or_number_only(self):
        for name, ftype, prop in mod.TABLE_FIELDS:
            self.assertIn(ftype, (mod.FT_TEXT, mod.FT_NUMBER), name)
        names = [name for name, _, _ in mod.TABLE_FIELDS]
        self.assertEqual(names[0], "key")
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 17)


if __name__ == "__main__":
    unittest.main()
