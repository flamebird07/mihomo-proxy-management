"""Shared fakes for the deploy kit tests (no network, no real credentials)."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

NODES_TABLE_ID = "tblNodes"
VALID_TABLE_ID = "tblValid"


class FakeFeishu:
    """Scriptable stand-in for ``node_provider.feishu.FeishuClient``.

    Every public method records its call in ``calls`` as ``(method, *args)``.
    ``raise_on`` maps a method name to an exception it raises instead;
    ``raise_for_table`` does the same for ``list_records`` of one table only.
    Tables start with the two pipeline tables unless ``tables`` is given; a
    table created through ``create_table`` gets an empty record list.  Any
    delete method fails the test: the deploy kit must never delete.
    """

    def __init__(self, *, tables=None, rows=None, wiki_node=None, raise_on=None,
                 raise_for_table=None):
        self.tables = ([{"name": "节点测速总表", "table_id": NODES_TABLE_ID},
                        {"name": "有效节点", "table_id": VALID_TABLE_ID}]
                       if tables is None else [dict(table) for table in tables])
        self.rows = {NODES_TABLE_ID: list(rows or [])}   # table_id -> records
        self.wiki_node = wiki_node
        self.raise_on = dict(raise_on or {})
        self.raise_for_table = dict(raise_for_table or {})
        self.calls = []
        self.fields = {}                                 # table_id -> field names
        self._next = 0

    # -- helpers -----------------------------------------------------------

    def _enter(self, method, *args):
        self.calls.append((method, *args))
        if method in self.raise_on:
            raise self.raise_on[method]

    def called(self, method):
        return [call for call in self.calls if call[0] == method]

    def records(self, table_id):
        return self.rows.setdefault(table_id, [])

    # -- transport ---------------------------------------------------------

    def token(self):
        self._enter("token")
        return "fake-tenant-token"

    def call(self, method, path, body=None, params=None):
        self._enter("call", method, path, params)
        if path == "/wiki/v2/spaces/get_node":
            return {"node": dict(self.wiki_node or {})}
        if path.startswith("/bitable/v1/apps/"):
            return {"app": {"name": "fake base"}}
        raise AssertionError(f"unexpected path {path}")

    # -- tables ------------------------------------------------------------

    def list_tables(self):
        self._enter("list_tables")
        return [dict(table) for table in self.tables]

    def create_table(self, name, fields):
        self._enter("create_table", name, fields)
        self._next += 1
        table_id = f"tblNew{self._next}"
        self.tables.append({"name": name, "table_id": table_id})
        self.fields[table_id] = [(field[0], field[1]) for field in fields]
        self.rows[table_id] = []
        return table_id

    def list_fields(self, table_id):
        self._enter("list_fields", table_id)
        return [{"field_id": f"fld{index}", "field_name": name, "type": ftype}
                for index, (name, ftype) in enumerate(self.fields.get(table_id, []))]

    def create_field(self, table_id, name, ftype, prop):
        self._enter("create_field", table_id, name, ftype)
        self.fields.setdefault(table_id, []).append((name, ftype))

    def update_field(self, table_id, field_id, name, ftype, prop):
        self._enter("update_field", table_id, field_id)

    # -- records -----------------------------------------------------------

    def list_records(self, table_id):
        self._enter("list_records", table_id)
        if table_id in self.raise_for_table:
            raise self.raise_for_table[table_id]
        return [dict(row) for row in self.records(table_id)]

    def batch_create(self, table_id, records):
        self._enter("batch_create", table_id, records)
        created = []
        for record in records:
            self._next += 1
            record_id = f"rec{self._next}"
            self.records(table_id).append({"record_id": record_id,
                                           "fields": dict(record["fields"])})
            created.append(record_id)
        return created

    def update_record(self, table_id, record_id, fields):
        self._enter("update_record", table_id, record_id, fields)
        for row in self.records(table_id):
            if row.get("record_id") == record_id:
                row["fields"].update(fields)
                return
        raise AssertionError(f"no record {record_id} in {table_id}")

    # -- forbidden ---------------------------------------------------------

    def batch_delete(self, *args, **kwargs):
        self._enter("batch_delete")
        raise AssertionError("the deploy kit must never delete records")
