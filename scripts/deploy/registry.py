"""「部署记录」: the one write that proves the app may edit this Bitable.

A bootstrap must know *before* it installs anything that the app it was handed
can read the table and write to it - a read-only app would install fine and
then fail every sync afterwards.  Registering the machine in its own table is
that probe, and it leaves the user a ledger of deployed machines.  Rows are
only ever created or updated; nothing here deletes.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../scripts
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from node_provider import schema  # noqa: E402
from node_provider.builder import field_text  # noqa: E402
from node_provider.feishu import ensure_table  # noqa: E402

REGISTRY_TABLE_KEY = "deploy_registry"
REGISTRY_TABLE_NAME = "部署记录"
#: (field name, Bitable type, property) in column order.
REGISTRY_FIELDS = (
    ("主机名", schema.FT_TEXT, None),  # the first field is the table's primary field
    ("平台", schema.FT_TEXT, None),
    ("项目版本", schema.FT_TEXT, None),
    ("部署方式", schema.FT_TEXT, None),
    ("部署时间", schema.FT_DATE_TIME, schema.DATE_PROPERTY),  # epoch milliseconds
)
HOST_FIELD = REGISTRY_FIELDS[0][0]


@dataclass(frozen=True)
class DeploymentRecord:
    """One machine's row.  ``hostname`` is the identity a re-run updates on."""

    hostname: str
    platform: str
    project_version: str
    deploy_kind: str = "github-bootstrap"
    deployed_at_ms: int = 0


def registry_table_spec() -> dict:
    return {"key": REGISTRY_TABLE_KEY, "name": REGISTRY_TABLE_NAME,
            "fields": list(REGISTRY_FIELDS)}


def registry_fields(record: DeploymentRecord) -> dict:
    values = (record.hostname, record.platform, record.project_version,
              record.deploy_kind, int(record.deployed_at_ms))
    return {name: value for (name, _ftype, _prop), value in zip(REGISTRY_FIELDS, values)}


def upsert_registry(client, record: DeploymentRecord, *,
                    log: Optional[Callable[[str], None]] = None) -> str:
    """Create or update this machine's row; returns ``"created"`` or ``"updated"``."""
    say = log or (lambda _message: None)
    table_id = ensure_table(client, registry_table_spec(), say)["table_id"]
    for row in client.list_records(table_id):
        fields = row.get("fields") if isinstance(row, dict) else None
        record_id = row.get("record_id") if isinstance(row, dict) else None
        if not record_id or not isinstance(fields, dict):
            continue
        if field_text(fields.get(HOST_FIELD)) == record.hostname:
            client.update_record(table_id, record_id, registry_fields(record))
            say(f"deploy registry updated table={REGISTRY_TABLE_NAME} host={record.hostname}")
            return "updated"
    client.batch_create(table_id, [{"fields": registry_fields(record)}])
    say(f"deploy registry created table={REGISTRY_TABLE_NAME} host={record.hostname}")
    return "created"
