"""node_ops: Feishu client safety, idempotent schema, no-write dry-run."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_ops import feishu, schema  # noqa: E402
from node_ops.logutil import Logger  # noqa: E402


class FakeClient:
    def __init__(self, tables=None, fields=None, records=None):
        self.tables = list(tables or [])
        self.fields = fields or {}
        self.records = records or {}
        self.calls = []
        self._next_table = 1
        self._next_field = 1

    def list_tables(self):
        self.calls.append(("list_tables",))
        return list(self.tables)

    def create_table(self, name, fields):
        self.calls.append(("create_table", name, fields))
        table_id = f"tbl-new-{self._next_table}"
        self._next_table += 1
        self.tables.append({"name": name, "table_id": table_id})
        return table_id

    def list_fields(self, table_id):
        self.calls.append(("list_fields", table_id))
        return list(self.fields.get(table_id, []))

    def create_field(self, table_id, name, ftype, prop):
        self.calls.append(("create_field", table_id, name, ftype))

    def update_field(self, table_id, field_id, name, ftype, prop):
        self.calls.append(("update_field", table_id, name, prop))

    def list_records(self, table_id):
        self.calls.append(("list_records", table_id))
        return list(self.records.get(table_id, []))

    def batch_create(self, table_id, records):
        self.calls.append(("batch_create", table_id, records))
        return len(records)

    def batch_delete(self, table_id, record_ids):
        self.calls.append(("batch_delete", table_id, record_ids))
        return len(record_ids)


class SchemaIdempotencyTests(unittest.TestCase):
    def test_creates_both_tables_and_all_fields(self):
        client = FakeClient()
        messages = []
        tables = feishu.ensure_tables(client, messages.append)
        self.assertEqual(set(tables), {"nodes", "valid_nodes"})
        created_tables = [call for call in client.calls if call[0] == "create_table"]
        self.assertEqual([call[1] for call in created_tables],
                         [schema.NODES_TABLE_NAME, schema.VALID_TABLE_NAME])
        created_fields = [call[2] for call in client.calls if call[0] == "create_field"]
        self.assertEqual(
            created_fields,
            list(schema.field_names("nodes")) + list(schema.field_names("valid_nodes")),
        )

    def test_existing_complete_schema_is_untouched(self):
        def fields_for(spec):
            result = []
            for index, (name, ftype, prop) in enumerate(spec["fields"]):
                result.append({
                    "field_id": f"fld-{name}",
                    "field_name": name,
                    "type": ftype,
                    "property": prop,
                })
            return result

        tables = [
            {"name": schema.NODES_TABLE_NAME, "table_id": "tbl-n"},
            {"name": schema.VALID_TABLE_NAME, "table_id": "tbl-v"},
        ]
        fields = {"tbl-n": fields_for(schema.NODES_TABLE),
                  "tbl-v": fields_for(schema.VALID_TABLE)}
        client = FakeClient(tables=tables, fields=fields)
        feishu.ensure_tables(client, lambda _m: None)
        mutating = [call for call in client.calls
                    if call[0] in ("create_table", "create_field", "update_field")]
        self.assertEqual(mutating, [])

    def test_missing_single_select_option_is_extended(self):
        node_fields = []
        for name, ftype, prop in schema.NODES_TABLE["fields"]:
            current_prop = prop
            if name == "来源":
                current_prop = {"options": [{"name": "飞兔"}]}
            node_fields.append({"field_id": f"fld-{name}", "field_name": name,
                                "type": ftype, "property": current_prop})
        valid_fields = []
        for name, ftype, prop in schema.VALID_TABLE["fields"]:
            valid_fields.append({"field_id": f"fldv-{name}", "field_name": name,
                                 "type": ftype, "property": prop})
        tables = [
            {"name": schema.NODES_TABLE_NAME, "table_id": "tbl-n"},
            {"name": schema.VALID_TABLE_NAME, "table_id": "tbl-v"},
        ]
        client = FakeClient(tables=tables,
                            fields={"tbl-n": node_fields, "tbl-v": valid_fields})
        feishu.ensure_tables(client, lambda _m: None)
        updates = [call for call in client.calls if call[0] == "update_field"]
        self.assertEqual(len(updates), 1)
        options = [item["name"] for item in updates[0][3]["options"]]
        self.assertEqual(options[0], "飞兔")
        self.assertIn("未知", options)


class ReplaceAllTests(unittest.TestCase):
    def test_dry_run_never_creates_or_deletes(self):
        client = FakeClient(records={"tbl": [{"record_id": "old"}]})
        messages = []
        feishu.replace_all(client, "tbl", [{"fields": {"a": 1}}],
                           messages.append, dry_run=True)
        kinds = [call[0] for call in client.calls]
        self.assertNotIn("batch_create", kinds)
        self.assertNotIn("batch_delete", kinds)
        self.assertIn("list_records", kinds)

    def test_create_then_delete_old_rows(self):
        client = FakeClient(records={"tbl": [{"record_id": "old1"}, {"record_id": "old2"}]})
        created = feishu.replace_all(client, "tbl", [{"fields": {"a": 1}}], lambda _m: None)
        self.assertEqual(created, 1)
        kinds = [call[0] for call in client.calls]
        self.assertLess(kinds.index("batch_create"), kinds.index("batch_delete"))
        delete = [call for call in client.calls if call[0] == "batch_delete"][0]
        self.assertEqual(delete[2], ["old1", "old2"])

    def test_failed_create_never_deletes(self):
        class ZeroCreate(FakeClient):
            def batch_create(self, table_id, records):
                self.calls.append(("batch_create", table_id))
                return 0

        client = ZeroCreate(records={"tbl": [{"record_id": "old"}]})
        feishu.replace_all(client, "tbl", [{"fields": {"a": 1}}], lambda _m: None)
        kinds = [call[0] for call in client.calls]
        self.assertNotIn("batch_delete", kinds)

    def test_empty_payload_can_be_refused(self):
        client = FakeClient(records={"tbl": [{"record_id": "old"}]})
        feishu.replace_all(client, "tbl", [], lambda _m: None, allow_empty=False)
        kinds = [call[0] for call in client.calls]
        self.assertNotIn("batch_delete", kinds)

    def test_empty_valid_payload_clears_table(self):
        client = FakeClient(records={"tbl": [{"record_id": "old"}]})
        feishu.replace_all(client, "tbl", [], lambda _m: None, allow_empty=True)
        kinds = [call[0] for call in client.calls]
        self.assertIn("batch_delete", kinds)


class CredentialLoggingTests(unittest.TestCase):
    def test_token_and_secret_never_logged(self):
        messages = []
        client = feishu.FeishuClient("app-id", "SECRET-VALUE", "app-token", messages.append)
        client._raw = MagicMock(return_value={
            "code": 0, "tenant_access_token": "TOKEN-VALUE", "expire": 7200,
        })
        self.assertEqual(client.token(), "TOKEN-VALUE")
        joined = "\n".join(messages)
        self.assertNotIn("SECRET-VALUE", joined)
        self.assertNotIn("TOKEN-VALUE", joined)

    def test_logger_scrubs_registered_secret(self):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        log = Logger(handle.name)
        log.add_secret("SECRET-VALUE")
        log.info("secret is SECRET-VALUE")
        with open(handle.name, encoding="utf-8") as stream:
            text = stream.read()
        self.assertNotIn("SECRET-VALUE", text)
        self.assertIn("<redacted>", text)

    def test_token_error_has_no_secret_detail(self):
        messages = []
        client = feishu.FeishuClient("app-id", "SECRET-VALUE", "app-token", messages.append)
        client._raw = MagicMock(return_value={"code": 99991663, "msg": "SECRET-VALUE"})
        with self.assertRaises(feishu.FeishuError) as caught:
            client.token()
        self.assertEqual(caught.exception.kind, "token_error")
        self.assertNotIn("SECRET-VALUE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
