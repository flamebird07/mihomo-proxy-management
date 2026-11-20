"""Pipeline orchestration: read Feishu -> probe -> classify -> gate -> write.

The pipeline is source-agnostic.  node_provider is the only component that
extracts nodes (subscription or app) and writes them to 「节点测速总表」.
node_ops reads the node identity/config columns from that table, probes the
nodes in an isolated mihomo core, and writes back only the measurement columns
plus the 「有效节点」 table.

Nothing here switches, reloads or otherwise mutates any mihomo controller.
The only external side effect is Feishu table replacement (disabled by
``--dry-run``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import records, schema
from . import feishu
from .feishu import FeishuClient, ensure_tables, replace_all
from .gate import evaluate_nodes, previous_passes, previous_streaks
from .logutil import Logger
from .model import NodeEntry
from .probe import IsolatedProber, ProbeResult, default_core_path

DEFAULT_APP_TOKEN = "URo9bAsd5azYe5swAmtclIbEnNh"
DEFAULT_ENV_FILE = r"C:\Users\Administrator\.hermes\.env"


class RunError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Settings:
    env_file: str = DEFAULT_ENV_FILE
    app_token: str = DEFAULT_APP_TOKEN
    workers: int = 4
    total_budget_s: float = 600.0
    ipinfo_budget_s: float = 600.0
    timeout_ms: int = 5000
    core: str = field(default_factory=default_core_path)
    dry_run: bool = False
    ensure_only: bool = False
    log_file: Optional[str] = None
    verbose: bool = False


@dataclass
class RunSummary:
    collected: int = 0
    probed: int = 0
    alive: int = 0
    valid: int = 0
    skipped_invalid: int = 0
    wrote_nodes: int = 0
    wrote_valid: int = 0
    dry_run: bool = False


def _unique_probe_names(names: List[str]) -> List[str]:
    used: set[str] = set()
    result: List[str] = []
    for name in names:
        candidate = name
        suffix = 2
        while candidate in used:
            candidate = f"{name} [{suffix}]"
            suffix += 1
        used.add(candidate)
        result.append(candidate)
    return result


def entries_from_rows(rows: List[dict], log: Logger) -> tuple[List[NodeEntry], int]:
    """Build probe entries from 「节点测速总表」 identity/config columns.

    Rows whose 节点配置JSON cannot be decoded into an object are skipped and
    counted; they are never invented.  A row without 节点名 falls back to the
    config's own ``name``.
    """
    entries: List[NodeEntry] = []
    skipped = 0
    for row in rows:
        fields = row.get("fields") if isinstance(row, dict) else None
        if not isinstance(fields, dict):
            skipped += 1
            continue
        try:
            config = records.config_from_json(fields.get("节点配置JSON"))
        except (ValueError, TypeError):
            skipped += 1
            continue
        name = records.field_text(fields.get("节点名")).strip()
        if not name:
            name = str(config.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        entries.append(NodeEntry(
            name=name,
            source=records.field_text(fields.get("来源")).strip() or "未知",
            config=config,
            probe_name="",
            source_number=records.source_number_from_row(fields),
        ))

    probe_names = _unique_probe_names([entry.name for entry in entries])
    for entry, probe_name in zip(entries, probe_names):
        entry.probe_name = probe_name
    if skipped:
        log.warn(f"rows skipped reason=invalid_config count={skipped}")
    return entries, skipped


def _probe_configs(entries: List[NodeEntry]) -> List[Dict[str, Any]]:
    configs = []
    for entry in entries:
        config = dict(entry.config)
        config["name"] = entry.probe_name
        configs.append(config)
    return configs


def _locate_tables(client: FeishuClient) -> Dict[str, Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    by_name = {table.get("name"): table.get("table_id")
               for table in client.list_tables()}
    for spec in schema.TABLE_SPECS:
        table_id = by_name.get(spec["name"])
        if table_id:
            found[spec["key"]] = {"table_id": table_id, "name": spec["name"]}
    return found


#: Master-table tombstone window (minutes): a row nobody measured within this
#: window is deleted (the only delete in the pipeline).  Overridable via
#: ``FEISHU_STALE_NODE_MINUTES``; hour-accurate because 同步时间 is the
#: measuring side's last-valid-rate clock.
DEFAULT_STALE_NODE_MINUTES = 7 * 24 * 60


def stale_node_window_minutes() -> int:
    raw = os.environ.get("FEISHU_STALE_NODE_MINUTES", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_STALE_NODE_MINUTES


def run(settings: Settings, log: Logger,
        client: Optional[FeishuClient] = None,
        prober: Optional[Any] = None,
        now: Optional[float] = None) -> RunSummary:
    summary = RunSummary(dry_run=settings.dry_run)
    if client is None:
        raise RunError("Feishu credentials required", exit_code=2)

    if settings.ensure_only:
        ensure_tables(client, log.info)
        return summary

    tables = _locate_tables(client) if settings.dry_run else ensure_tables(client, log.info)
    nodes_table = tables.get(schema.NODES_TABLE_KEY)
    valid_table = tables.get(schema.VALID_TABLE_KEY)
    if not nodes_table or not valid_table:
        raise RunError("required tables unavailable", exit_code=5)

    node_rows = client.list_records(nodes_table["table_id"])
    previous_valid_rows = client.list_records(valid_table["table_id"])

    entries, skipped = entries_from_rows(node_rows, log)
    summary.collected = len(entries)
    summary.skipped_invalid = skipped
    if not entries:
        raise RunError("no nodes in 节点测速总表", exit_code=3)

    if prober is None:
        prober = IsolatedProber(
            core=settings.core,
            timeout_ms=settings.timeout_ms,
            workers=settings.workers,
            total_budget_s=settings.total_budget_s,
            ipinfo_budget_s=settings.ipinfo_budget_s,
        )
    try:
        result: ProbeResult = prober.probe(_probe_configs(entries),
                                           names=[entry.probe_name for entry in entries])
    except Exception as exc:
        raise RunError(f"probe failed ({type(exc).__name__})", exit_code=4) from None

    evaluations = evaluate_nodes(
        entries,
        result,
        previous_streaks(previous_valid_rows),
        previous_passes(node_rows),
    )
    summary.probed = len(evaluations)
    summary.alive = sum(1 for item in evaluations if result.delays.get(item.entry.probe_name))
    summary.valid = sum(1 for item in evaluations if item.gpt_available)

    stamp = int((now if now is not None else time.time()) * 1000)
    node_records = records.build_node_records(
        entries, result, stamp, stamp, previous_rows=node_rows,
    )
    valid_records = records.build_valid_records(evaluations, stamp, stamp)

    if settings.dry_run:
        log.info(
            f"dry-run collected={summary.collected} probed={summary.probed} "
            f"alive={summary.alive} valid={summary.valid} "
            f"node_rows={len(node_records)} valid_rows={len(valid_records)}"
        )
        return summary

    try:
        summary.wrote_nodes = feishu.upsert_node_rows(
            client, nodes_table["table_id"], node_records, stamp, log.info,
        )
        summary.wrote_valid = replace_all(
            client, valid_table["table_id"], valid_records, log.info, allow_empty=True,
        )
        # The ONLY delete (write-discipline ruling): rows with no valid rate
        # for the window.  7 days by default, hour-accurate via 同步时间.
        feishu.tombstone_stale_rows(
            client, nodes_table["table_id"], stale_node_window_minutes(),
            stamp, log.info,
        )
    except Exception as exc:
        raise RunError(f"table write failed ({type(exc).__name__})", exit_code=5) from None
    if summary.wrote_nodes < len(node_records):
        # Partial write (slow Feishu rows): the round's measurements that DID
        # land keep their clock; the rest retry next round. Not a failure.
        log.warn(f"node write partial wrote={summary.wrote_nodes} "
                 f"of={len(node_records)}")
    return summary
