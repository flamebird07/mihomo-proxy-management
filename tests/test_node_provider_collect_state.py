"""node_provider daily-collect state: skip logic, per-source replacement, upsert.

All clients are synthetic: no network, no real Feishu, no file state.
"""

import os
import sys
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_provider import collect, collect_state, schema  # noqa: E402
from node_provider.feishu import FeishuError, replace_source_rows  # noqa: E402
from node_provider.logutil import Logger  # noqa: E402
from node_provider.runner import Settings, run_collect  # noqa: E402
from node_provider.subscription import SubscriptionError  # noqa: E402

NOW_MS = 1_700_000_000_000
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
EXACTLY_24H_MS = NOW_MS - DAY_MS
YAML_ONE = """
proxies:
  - name: us-01
    type: anytls
    server: 1.1.1.1
    port: 443
    password: pw
"""
URI_ONE = "trojan://pw@2.2.2.2:443#sg-01\n"
ENV_ONE = {"SUBSCRIPTION_URL_1": "https://one.invalid/sub"}
ENV_1_3 = {
    "SUBSCRIPTION_URL_1": "https://one.invalid/sub",
    "SUBSCRIPTION_URL_3": "https://three.invalid/sub",
}


def fetcher_map(mapping):
    def fetch(url, ua, timeout):
        if url not in mapping:
            raise SubscriptionError("boom", "http")
        return mapping[url]
    return fetch


def state_row(source_number, channel="subscription", read_at=NOW_MS,
              record_id="state-1"):
    return {"record_id": record_id, "fields": {
        "源编号": source_number,
        "来源": schema.SOURCE_BY_INDEX.get(int(source_number), "未知"),
        "通道": channel,
        "读取机器": "machine-a",
        "最近读取时间": read_at,
        "节点数": 1,
    }}


class StateClient:
    """Minimal fake supporting the state table read path."""

    def __init__(self, rows=None, *, tables=None, table_error=False,
                 record_error=False, write_error=False):
        self.rows = list(rows or [])
        self.tables = tables if tables is not None else [
            {"name": schema.COLLECT_STATE_TABLE_NAME, "table_id": "tbl-state"}]
        self.table_error = table_error
        self.record_error = record_error
        self.write_error = write_error
        self.writes = []

    def list_tables(self):
        if self.table_error:
            raise FeishuError("network_error")
        return list(self.tables)

    def list_records(self, table_id):
        if self.record_error:
            raise FeishuError("http_500")
        return list(self.rows)

    def batch_create(self, table_id, records):
        if self.write_error:
            raise FeishuError("api_error")
        self.writes.append(("batch_create", table_id, records))
        return [f"new{len(self.writes)}"]

    def update_record(self, table_id, record_id, fields):
        if self.write_error:
            raise FeishuError("api_error")
        self.writes.append(("update_record", table_id, record_id, fields))


class ReadSkipTests(unittest.TestCase):
    def test_just_now_attempt_is_skipped(self):
        client = StateClient([state_row(1)])
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, {("subscription", 1)})

    def test_23h59m_old_attempt_is_skipped(self):
        client = StateClient([state_row(1, read_at=NOW_MS - (DAY_MS - 60_000))])
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, {("subscription", 1)})

    def test_exactly_24h_old_attempt_is_not_skipped(self):
        client = StateClient([state_row(1, read_at=EXACTLY_24H_MS)])
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, set())

    def test_25h_old_attempt_is_not_skipped(self):
        client = StateClient([state_row(1, read_at=NOW_MS - 25 * HOUR_MS)])
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, set())

    def test_cross_day_but_under_24h_is_still_skipped(self):
        local_midnight = time.mktime(
            (time.localtime(NOW_MS / 1000.0)[:3] + (0, 0, 0, 0, 0, -1)))
        now_ms = int(local_midnight * 1000) + 30 * 60_000      # 00:30 local
        read_at = now_ms - 2 * HOUR_MS                          # 22:30 previous day
        self.assertNotEqual(time.localtime(read_at / 1000.0)[:3],
                            time.localtime(now_ms / 1000.0)[:3])
        client = StateClient([state_row(1, read_at=read_at)])
        skip = collect_state.read_skip_set(client, now_ms, lambda _m: None)
        self.assertEqual(skip, {("subscription", 1)})

    def test_numeric_string_readback_is_parsed(self):
        row = state_row("1", read_at=str(NOW_MS))
        client = StateClient([row])
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, {("subscription", 1)})

    def test_bad_timestamp_row_is_ignored(self):
        rows = [state_row(1, read_at="not-a-time"), state_row(3, read_at=NOW_MS,
                                                             record_id="state-3")]
        client = StateClient(rows)
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, {("subscription", 3)})

    def test_unknown_channel_is_ignored(self):
        client = StateClient([state_row(1, channel="weird")])
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, set())

    def test_app_attempt_from_another_host_is_not_skipped(self):
        row = state_row(3, channel="app")
        row["fields"]["读取机器"] = "guoshiweideMac-Mini.local"
        client = StateClient([row])
        skip = collect_state.read_skip_set(
            client, NOW_MS, lambda _m: None, hostname="windows-host")
        self.assertEqual(skip, set())

    def test_app_attempt_from_same_host_is_skipped(self):
        client = StateClient([state_row(3, channel="app")])  # machine-a
        skip = collect_state.read_skip_set(
            client, NOW_MS, lambda _m: None, hostname="machine-a")
        self.assertEqual(skip, {("app", 3)})

    def test_subscription_attempt_remains_shared_across_hosts(self):
        client = StateClient([state_row(1)])  # machine-a
        skip = collect_state.read_skip_set(
            client, NOW_MS, lambda _m: None, hostname="machine-b")
        self.assertEqual(skip, {("subscription", 1)})

    def test_missing_table_returns_empty(self):
        client = StateClient(tables=[{"name": "别的表", "table_id": "t"}])
        skip = collect_state.read_skip_set(client, NOW_MS, lambda _m: None)
        self.assertEqual(skip, set())

    def test_list_tables_error_is_fail_open(self):
        logger = Logger()
        client = StateClient(table_error=True)
        self.assertEqual(collect_state.read_skip_set(client, NOW_MS, logger.warn),
                         set())
        self.assertTrue(any("collect state unavailable" in line
                            for line in logger.lines))

    def test_list_records_error_is_fail_open(self):
        logger = Logger()
        client = StateClient(record_error=True)
        self.assertEqual(collect_state.read_skip_set(client, NOW_MS, logger.warn),
                         set())
        self.assertTrue(any("collect state read failed" in line
                            for line in logger.lines))


class ReplaceSourceRowsTests(unittest.TestCase):
    class TableClient:
        def __init__(self, rows, *, shortfall=False):
            self.rows = list(rows)
            self.shortfall = shortfall
            self.calls = []
            self._seq = 0

        def list_records(self, table_id):
            self.calls.append(("list_records", table_id))
            return list(self.rows)

        def batch_create(self, table_id, records):
            self.calls.append(("batch_create", table_id, records))
            wanted = records[:-1] if self.shortfall else records
            ids = []
            for record in wanted:
                self._seq += 1
                record_id = f"new{self._seq}"
                ids.append(record_id)
                self.rows.append({"record_id": record_id,
                                  "fields": dict(record["fields"])})
            return ids

        def batch_delete(self, table_id, record_ids):
            self.calls.append(("batch_delete", table_id, record_ids))
            self.rows = [row for row in self.rows
                         if row.get("record_id") not in record_ids]
            return len(record_ids)

    def test_only_source_rows_are_deleted(self):
        rows = [
            {"record_id": "a1", "fields": {"源编号": 1, "节点名": "src1-old"}},
            {"record_id": "b1", "fields": {"源编号": "2", "节点名": "src2-old"}},
        ]
        client = self.TableClient(rows)
        created = replace_source_rows(client, "tbl", 1,
                                      [{"fields": {"源编号": 1, "节点名": "new"}}],
                                      lambda _m: None)
        self.assertEqual(created, 1)
        remaining = {(r.get("record_id"), r["fields"].get("节点名")) for r in client.rows}
        self.assertNotIn(("a1", "src1-old"), remaining)
        self.assertIn(("b1", "src2-old"), remaining)
        self.assertIn(("new1", "new"), remaining)

    def test_create_incomplete_keeps_old_rows(self):
        rows = [{"record_id": "a1", "fields": {"源编号": 1}}]
        client = self.TableClient(rows, shortfall=True)
        created = replace_source_rows(
            client, "tbl", 1,
            [{"fields": {"源编号": 1}}, {"fields": {"源编号": 1}}],
            lambda _m: None)
        self.assertEqual(created, 1)
        self.assertFalse(any(call[0] == "batch_delete" for call in client.calls))
        self.assertIn("a1", [row["record_id"] for row in client.rows])

    def test_concurrent_writer_rows_are_cleaned_by_last_finisher(self):
        rows = [
            {"record_id": "other-machine", "fields": {"源编号": 1}},
            {"record_id": "mine-old", "fields": {"源编号": 1}},
        ]
        client = self.TableClient(rows)
        replace_source_rows(client, "tbl", 1,
                            [{"fields": {"源编号": 1}}], lambda _m: None)
        ids = [row["record_id"] for row in client.rows]
        self.assertNotIn("other-machine", ids)
        self.assertNotIn("mine-old", ids)
        self.assertIn("new1", ids)

    def test_empty_records_refuses_replacement(self):
        client = self.TableClient([{"record_id": "a1", "fields": {"源编号": 1}}])
        self.assertEqual(replace_source_rows(client, "tbl", 1, [], lambda _m: None), 0)
        self.assertEqual(client.calls, [])
        self.assertEqual([row["record_id"] for row in client.rows], ["a1"])


class RecordAttemptsTests(unittest.TestCase):
    def test_no_row_creates(self):
        client = StateClient([])
        collect_state.record_attempts(
            client, "tbl-state", [(1, "飞兔", "subscription", 4)],
            "machine-a", NOW_MS, lambda _m: None)
        self.assertEqual([w[0] for w in client.writes], ["batch_create"])
        fields = client.writes[0][2][0]["fields"]
        self.assertEqual(fields["源编号"], 1)
        self.assertEqual(fields["读取机器"], "machine-a")
        self.assertEqual(fields["最近读取时间"], NOW_MS)
        self.assertEqual(fields["节点数"], 4)

    def test_failed_attempt_is_recorded_with_zero_nodes(self):
        client = StateClient([])
        collect_state.record_attempts(
            client, "tbl-state", [(2, "CoffeeCloud", "subscription", 0)],
            "machine-a", NOW_MS, lambda _m: None)
        fields = client.writes[0][2][0]["fields"]
        self.assertEqual(fields["源编号"], 2)
        self.assertEqual(fields["节点数"], 0)

    def test_existing_row_updates(self):
        client = StateClient([state_row(1, record_id="state-1")])
        collect_state.record_attempts(
            client, "tbl-state", [(1, "飞兔", "subscription", 9)],
            "machine-b", NOW_MS + 5, lambda _m: None)
        self.assertEqual([w[0] for w in client.writes], ["update_record"])
        self.assertEqual(client.writes[0][2], "state-1")
        self.assertEqual(client.writes[0][3]["节点数"], 9)

    def test_app_attempt_from_other_host_creates_a_separate_row(self):
        client = StateClient([state_row(3, channel="app", record_id="state-mac")])
        collect_state.record_attempts(
            client, "tbl-state", [(3, "西瓜加速", "app", 57)],
            "windows-host", NOW_MS, lambda _m: None)
        self.assertEqual([w[0] for w in client.writes], ["batch_create"])
        self.assertEqual(client.writes[0][2][0]["fields"]["读取机器"], "windows-host")

    def test_app_attempt_from_same_host_updates_its_own_row(self):
        client = StateClient([state_row(3, channel="app", record_id="state-win")])
        collect_state.record_attempts(
            client, "tbl-state", [(3, "西瓜加速", "app", 57)],
            "machine-a", NOW_MS, lambda _m: None)
        self.assertEqual([w[0] for w in client.writes], ["update_record"])
        self.assertEqual(client.writes[0][2], "state-win")

    def test_write_failure_does_not_raise(self):
        logger = Logger()
        client = StateClient([], write_error=True)
        collect_state.record_attempts(
            client, "tbl-state", [(1, "飞兔", "subscription", 1)],
            "machine-a", NOW_MS, logger.warn)
        self.assertTrue(any("collect state write failed" in line
                            for line in logger.lines))


class AttemptEntryNumberingTests(unittest.TestCase):
    def test_subscription_entry_uses_env_slot_source_number(self):
        result = types.SimpleNamespace(
            per_channel={"subscription:CoffeeCloud": 5, "subscription:飞兔": 3},
            failed=[])
        entries = collect_state.attempt_entries(result)
        self.assertEqual(entries, [
            (2, "CoffeeCloud", "subscription", 5),
            (1, "飞兔", "subscription", 3),
        ])

    def test_app_entry_uses_renumbered_app_source(self):
        result = types.SimpleNamespace(
            per_channel={"app:source4": 7, "app:source3": 2},
            failed=[])
        entries = collect_state.attempt_entries(result)
        self.assertEqual(entries, [
            (2, "CoffeeCloud", "app", 2),
            (3, "西瓜加速", "app", 7),
        ])

    def test_failed_channels_become_zero_node_entries(self):
        result = types.SimpleNamespace(
            per_channel={"subscription:飞兔": 3},
            failed=["subscription:CoffeeCloud", "app:source4"])
        entries = collect_state.attempt_entries(result)
        self.assertEqual(entries, [
            (1, "飞兔", "subscription", 3),
            (3, "西瓜加速", "app", 0),
            (2, "CoffeeCloud", "subscription", 0),
        ])

    def test_skipped_channels_produce_no_entries(self):
        result = types.SimpleNamespace(
            per_channel={}, failed=[], skipped=["subscription:飞兔"])
        self.assertEqual(collect_state.attempt_entries(result), [])


class CollectSkipTests(unittest.TestCase):
    def test_skipped_subscription_by_renumbered_key(self):
        calls = []

        def fetch(url, ua, timeout):
            calls.append(url)
            return URI_ONE

        result = collect.collect(
            ENV_1_3, fetcher=fetch, skip={("subscription", 2)})
        self.assertNotIn(ENV_1_3["SUBSCRIPTION_URL_3"], calls)
        self.assertIn(ENV_1_3["SUBSCRIPTION_URL_1"], calls)
        self.assertIn("subscription:CoffeeCloud", result.skipped)

    def test_skipped_subscription_is_not_fetched(self):
        calls = []

        def fetch(url, ua, timeout):
            calls.append(url)
            if url == ENV_1_3["SUBSCRIPTION_URL_1"]:
                return YAML_ONE
            raise SubscriptionError("HTTP error 403 (URL redacted)", "http")

        result = collect.collect(
            ENV_1_3, fetcher=fetch, skip={("subscription", 1)})
        self.assertNotIn(ENV_1_3["SUBSCRIPTION_URL_1"], calls)
        self.assertIn(ENV_1_3["SUBSCRIPTION_URL_3"], calls)
        self.assertIn("subscription:飞兔", result.skipped)
        self.assertNotIn("subscription:飞兔", result.per_channel)
        self.assertIn("skipped=1", result.summary())

    def test_no_skip_behaves_as_before(self):
        result = collect.collect(
            ENV_ONE,
            fetcher=fetcher_map({ENV_ONE["SUBSCRIPTION_URL_1"]: YAML_ONE}))
        self.assertEqual(result.collected, 1)
        self.assertEqual(result.skipped, [])


class TableClient:
    """Fake Feishu client covering nodes/valid/state tables."""

    def __init__(self, node_rows=None, state_rows=None, include_state=True,
                 nodes_write_error=False):
        self.rows = {
            "tbl-nodes": list(node_rows or []),
            "tbl-valid": [],
            "tbl-state": list(state_rows or []),
        }
        self.nodes_write_error = nodes_write_error
        self.tables = [
            {"name": schema.NODES_TABLE_NAME, "table_id": "tbl-nodes"},
            {"name": schema.VALID_TABLE_NAME, "table_id": "tbl-valid"},
        ]
        if include_state:
            self.tables.append(
                {"name": schema.COLLECT_STATE_TABLE_NAME, "table_id": "tbl-state"})
        self.calls = []
        self._seq = 0

    def _spec(self, table_id):
        return {
            "tbl-nodes": schema.NODES_TABLE,
            "tbl-valid": schema.VALID_TABLE,
            "tbl-state": schema.COLLECT_STATE_TABLE,
        }[table_id]

    def list_tables(self):
        return list(self.tables)

    def list_fields(self, table_id):
        return [{"field_id": f"fld-{name}", "field_name": name, "type": ftype,
                 "property": prop}
                for name, ftype, prop in self._spec(table_id)["fields"]]

    def list_records(self, table_id):
        self.calls.append(("list_records", table_id))
        return list(self.rows.get(table_id, []))

    def batch_create(self, table_id, records):
        self.calls.append(("batch_create", table_id, records))
        if self.nodes_write_error and table_id == "tbl-nodes":
            raise FeishuError("api_error")
        ids = []
        for record in records:
            self._seq += 1
            record_id = f"new{self._seq}"
            ids.append(record_id)
            self.rows.setdefault(table_id, []).append(
                {"record_id": record_id, "fields": dict(record["fields"])})
        return ids

    def batch_delete(self, table_id, record_ids):
        self.calls.append(("batch_delete", table_id, record_ids))
        self.rows[table_id] = [row for row in self.rows.get(table_id, [])
                               if row.get("record_id") not in record_ids]
        return len(record_ids)

    def update_record(self, table_id, record_id, fields):
        self.calls.append(("update_record", table_id, record_id, fields))
        for row in self.rows.get(table_id, []):
            if row.get("record_id") == record_id:
                row["fields"] = dict(fields)
                return
        raise AssertionError("unknown record id")

    def create_table(self, name, fields):
        self.calls.append(("create_table", name))
        self.tables.append({"name": name, "table_id": "tbl-state"})
        self.rows.setdefault("tbl-state", [])
        return "tbl-state"

    def create_field(self, *args):  # pragma: no cover - fields are complete
        raise AssertionError("unexpected create_field")

    def update_field(self, *args):  # pragma: no cover - fields are complete
        raise AssertionError("unexpected update_field")


def settings(**kwargs):
    base = dict(dry_run=False)
    base.update(kwargs)
    return Settings(**base)


class RunCollectStateTests(unittest.TestCase):
    def test_force_ignores_recent_state(self):
        client = TableClient(state_rows=[state_row(1)])
        code = run_collect(
            settings(force=True), client, Logger(), env_values=ENV_ONE,
            fetcher=fetcher_map({ENV_ONE["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            hostname="machine-a", now_ms=NOW_MS)
        self.assertEqual(code, 0)
        self.assertTrue(any(call[0] == "batch_create" and call[1] == "tbl-nodes"
                            for call in client.calls))

    def test_all_sources_skipped_is_exit_ok_and_zero_write(self):
        client = TableClient(state_rows=[state_row(1)])
        logger = Logger()
        code = run_collect(
            settings(), client, logger, env_values=ENV_ONE,
            fetcher=fetcher_map({ENV_ONE["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            hostname="machine-a", now_ms=NOW_MS)
        self.assertEqual(code, 0)
        writes = [call for call in client.calls
                  if call[0] in ("batch_create", "batch_delete", "update_record")]
        self.assertEqual(writes, [])
        self.assertTrue(any("skipped all sources within_24h" in line
                            for line in logger.lines))

    def test_skipped_with_failed_source_records_failure_and_exit_ok(self):
        client = TableClient(state_rows=[state_row(1)])
        logger = Logger()
        code = run_collect(
            settings(), client, logger, env_values=ENV_1_3,
            fetcher=fetcher_map({}),
            hostname="machine-a", now_ms=NOW_MS)
        self.assertEqual(code, 0)
        # No total-table write (no nodes), but the failed attempt is recorded.
        node_writes = [call for call in client.calls
                       if call[0] in ("batch_create", "batch_delete")
                       and call[1] == "tbl-nodes"]
        self.assertEqual(node_writes, [])
        state = {(r["fields"]["通道"], r["fields"]["源编号"]): r["fields"]["节点数"]
                 for r in client.rows["tbl-state"]}
        self.assertEqual(state[("subscription", 2)], 0)
        self.assertTrue(any("skipped all sources within_24h" in line
                            for line in logger.lines))

    def test_partial_skip_replaces_only_collected_source(self):
        node_rows = [
            {"record_id": "s1-old", "fields": {"源编号": 1, "节点名": "keep1"}},
            {"record_id": "s2-old", "fields": {"源编号": "2", "节点名": "old2"}},
            {"record_id": "s3-old", "fields": {"源编号": 3, "节点名": "keep3"}},
        ]
        client = TableClient(node_rows=node_rows, state_rows=[state_row(1)])
        code = run_collect(
            settings(), client, Logger(), env_values=ENV_1_3,
            fetcher=fetcher_map({ENV_1_3["SUBSCRIPTION_URL_3"]: URI_ONE}),
            hostname="machine-b", now_ms=NOW_MS)
        self.assertEqual(code, 0)
        node_ids = [row["record_id"] for row in client.rows["tbl-nodes"]]
        self.assertIn("s1-old", node_ids)      # skipped source untouched
        self.assertIn("s3-old", node_ids)      # other source untouched
        # Write-discipline ruling: even the collected source's leftover row
        # survives the write (append/update only); the time tombstone is the
        # sole deleter and these rows carry no clock.
        self.assertIn("s2-old", node_ids)
        self.assertTrue(any(record_id.startswith("new") for record_id in node_ids))
        created_tables = [call[1] for call in client.calls
                          if call[0] == "batch_create" and call[1] == "tbl-nodes"]
        self.assertEqual(set(created_tables), {"tbl-nodes"})
        for call in client.calls:
            if call[0] == "batch_create" and call[1] == "tbl-nodes":
                self.assertEqual({r["fields"]["源编号"] for r in call[2]}, {2})
                self.assertEqual({r["fields"]["来源"] for r in call[2]},
                                 {"CoffeeCloud"})
        state_rows = client.rows["tbl-state"]
        keys = {(r["fields"]["通道"], r["fields"]["源编号"]) for r in state_rows}
        self.assertIn(("subscription", 1), keys)   # pre-existing row untouched
        self.assertIn(("subscription", 2), keys)   # newly recorded (CoffeeCloud)
        recorded = {(r["fields"]["通道"], r["fields"]["源编号"]): r["fields"]
                    for r in state_rows}
        self.assertEqual(recorded[("subscription", 2)]["最近读取时间"], NOW_MS)

    def test_failed_source_and_success_both_recorded(self):
        node_rows = [{"record_id": "s3-old", "fields": {"源编号": 3}}]
        client = TableClient(node_rows=node_rows, state_rows=[])
        code = run_collect(
            settings(), client, Logger(), env_values=ENV_1_3,
            fetcher=fetcher_map({ENV_1_3["SUBSCRIPTION_URL_3"]: URI_ONE}),
            hostname="machine-c", now_ms=NOW_MS)
        self.assertEqual(code, 0)
        state = {(r["fields"]["通道"], r["fields"]["源编号"]): r["fields"]["节点数"]
                 for r in client.rows["tbl-state"]}
        # 飞兔 (源1) never produced a body -> failed attempt, 0 nodes.
        # CoffeeCloud (源2) succeeded with one node.
        self.assertEqual(state, {("subscription", 1): 0, ("subscription", 2): 1})
        self.assertIn("s3-old", [r["record_id"] for r in client.rows["tbl-nodes"]])

    def test_all_attempts_fail_exit_read_and_record_zero(self):
        client = TableClient(state_rows=[])
        logger = Logger()
        code = run_collect(
            settings(), client, logger, env_values=ENV_1_3,
            fetcher=fetcher_map({}),
            hostname="machine-a", now_ms=NOW_MS)
        self.assertEqual(code, 3)
        state = {(r["fields"]["通道"], r["fields"]["源编号"]): r["fields"]["节点数"]
                 for r in client.rows["tbl-state"]}
        self.assertEqual(state, {("subscription", 1): 0, ("subscription", 2): 0})
        self.assertTrue(any("no nodes collected" in line for line in logger.lines))

    def test_total_table_write_failure_still_records_attempt(self):
        client = TableClient(state_rows=[], nodes_write_error=True)
        logger = Logger()
        code = run_collect(
            settings(), client, logger, env_values=ENV_ONE,
            fetcher=fetcher_map({ENV_ONE["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            hostname="machine-a", now_ms=NOW_MS)
        self.assertEqual(code, 3)
        state = {(r["fields"]["通道"], r["fields"]["源编号"]): r["fields"]["节点数"]
                 for r in client.rows["tbl-state"]}
        self.assertEqual(state, {("subscription", 1): 1})
        self.assertTrue(any("table write failed" in line for line in logger.lines))

    def test_dry_run_zero_write_and_keeps_rows(self):
        node_rows = [{"record_id": "s1-old", "fields": {"源编号": 1}}]
        client = TableClient(node_rows=node_rows, state_rows=[])
        code = run_collect(
            settings(dry_run=True), client, Logger(), env_values=ENV_ONE,
            fetcher=fetcher_map({ENV_ONE["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            hostname="machine-a", now_ms=NOW_MS)
        self.assertEqual(code, 0)
        writes = [call for call in client.calls
                  if call[0] in ("batch_create", "batch_delete", "update_record",
                                 "create_table")]
        self.assertEqual(writes, [])
        self.assertEqual([r["record_id"] for r in client.rows["tbl-nodes"]],
                         ["s1-old"])
        self.assertEqual(client.rows["tbl-state"], [])

    def test_missing_state_table_is_created_and_collect_succeeds(self):
        client = TableClient(node_rows=[], state_rows=[], include_state=False)
        logger = Logger()
        code = run_collect(
            settings(), client, logger, env_values=ENV_ONE,
            fetcher=fetcher_map({ENV_ONE["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            hostname="machine-a", now_ms=NOW_MS)
        self.assertEqual(code, 0)
        self.assertTrue(any(call[0] == "create_table"
                            and call[1] == schema.COLLECT_STATE_TABLE_NAME
                            for call in client.calls))
        self.assertTrue(any(r["fields"]["通道"] == "subscription"
                            and r["fields"]["源编号"] == 1
                            for r in client.rows["tbl-state"]))


if __name__ == "__main__":
    unittest.main()
