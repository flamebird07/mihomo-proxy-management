"""deploy.registry: the 「部署记录」 table is upsert-only and never deletes."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import registry  # noqa: E402
from deploy_test_support import FakeFeishu, NODES_TABLE_ID  # noqa: E402
from node_provider import schema  # noqa: E402

STAMP = 1_700_000_000_000


def record(**overrides):
    values = dict(hostname="host-a.local", platform="macos",
                  project_version="1.0.0", deployed_at_ms=STAMP)
    values.update(overrides)
    return registry.DeploymentRecord(**values)


def delete_calls(client):
    return [call for call in client.calls if "delete" in call[0]]


class SpecTests(unittest.TestCase):
    def test_table_spec(self):
        spec = registry.registry_table_spec()
        self.assertEqual(spec["name"], "部署记录")
        self.assertEqual(spec["key"], registry.REGISTRY_TABLE_KEY)
        self.assertEqual(spec["fields"], list(registry.REGISTRY_FIELDS))
        self.assertEqual(spec["fields"][0][0], "主机名")
        time_field = spec["fields"][-1]
        self.assertEqual(time_field[0], "部署时间")
        self.assertEqual(time_field[1], schema.FT_DATE_TIME)
        self.assertEqual(time_field[2], schema.DATE_PROPERTY)
        # A plain Bitable table needs its first field to be a text primary field.
        self.assertEqual(spec["fields"][0][1], schema.FT_TEXT)

    def test_fields_follow_the_column_order_with_a_millisecond_stamp(self):
        fields = registry.registry_fields(record())
        self.assertEqual(tuple(fields), tuple(name for name, _t, _p in registry.REGISTRY_FIELDS))
        self.assertEqual(fields["主机名"], "host-a.local")
        self.assertEqual(fields["平台"], "macos")
        self.assertEqual(fields["部署方式"], "github-bootstrap")
        self.assertEqual(fields["部署时间"], STAMP)
        self.assertIsInstance(fields["部署时间"], int)


class UpsertTests(unittest.TestCase):
    def test_missing_table_is_created_then_the_row_is_added(self):
        client = FakeFeishu(tables=[{"name": "节点测速总表", "table_id": NODES_TABLE_ID}])
        logs = []
        self.assertEqual(registry.upsert_registry(client, record(), log=logs.append), "created")
        created = client.called("create_table")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][1], "部署记录")
        self.assertEqual(created[0][2], list(registry.REGISTRY_FIELDS))
        batch = client.called("batch_create")
        self.assertEqual(len(batch), 1)
        self.assertEqual(len(batch[0][2]), 1)
        self.assertEqual(batch[0][2][0]["fields"]["主机名"], "host-a.local")
        self.assertEqual(client.called("update_record"), [])
        self.assertEqual(delete_calls(client), [])
        self.assertTrue(any("deploy registry created" in line for line in logs))

    def test_existing_row_for_the_same_hostname_is_updated(self):
        client = FakeFeishu(tables=[{"name": "节点测速总表", "table_id": NODES_TABLE_ID},
                                    {"name": "部署记录", "table_id": "tblReg"}])
        client.records("tblReg").append({"record_id": "recOld", "fields": {
            "主机名": "host-a.local", "平台": "linux", "项目版本": "0.9.0"}})
        logs = []
        self.assertEqual(registry.upsert_registry(client, record(), log=logs.append), "updated")
        updates = client.called("update_record")
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1:], ("tblReg", "recOld", registry.registry_fields(record())))
        self.assertEqual(client.called("create_table"), [])
        self.assertEqual(client.called("batch_create"), [])
        self.assertEqual(delete_calls(client), [])
        self.assertTrue(any("deploy registry updated" in line for line in logs))

    def test_other_hostname_gets_its_own_row(self):
        client = FakeFeishu(tables=[{"name": "部署记录", "table_id": "tblReg"}])
        client.records("tblReg").append({"record_id": "recOther", "fields": {"主机名": "host-b"}})
        self.assertEqual(registry.upsert_registry(client, record()), "created")
        self.assertEqual(client.called("update_record"), [])
        self.assertEqual(len(client.records("tblReg")), 2)

    def test_segmented_text_hostname_is_matched(self):
        client = FakeFeishu(tables=[{"name": "部署记录", "table_id": "tblReg"}])
        client.records("tblReg").append({"record_id": "recSeg", "fields": {
            "主机名": [{"text": "host-a.lo"}, {"text": "cal"}]}})
        self.assertEqual(registry.upsert_registry(client, record()), "updated")
        self.assertEqual(client.called("update_record")[0][2], "recSeg")

    def test_rows_without_a_usable_identity_are_skipped_not_crashed(self):
        client = FakeFeishu(tables=[{"name": "部署记录", "table_id": "tblReg"}])
        client.records("tblReg").extend([
            {"fields": {"主机名": "host-a.local"}},              # no record_id
            {"record_id": "recX", "fields": None},               # no fields
            {"record_id": "recY", "fields": {"other": 1}},       # no hostname
        ])
        self.assertEqual(registry.upsert_registry(client, record()), "created")

    def test_repeated_runs_converge_to_one_row_per_host(self):
        client = FakeFeishu(tables=[])
        first = registry.upsert_registry(client, record())
        second = registry.upsert_registry(client, record(deployed_at_ms=STAMP + 5_000))
        third = registry.upsert_registry(client, record(hostname="host-b", platform="windows"))
        self.assertEqual((first, second, third), ("created", "updated", "created"))
        self.assertEqual(len(client.records("tblNew1")), 2)
        self.assertEqual(client.records("tblNew1")[0]["fields"]["部署时间"], STAMP + 5_000)
        self.assertEqual(delete_calls(client), [])


if __name__ == "__main__":
    unittest.main()
