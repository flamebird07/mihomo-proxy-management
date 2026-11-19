"""Cross-machine daily-collect state stored in the Feishu 「采集状态」 table.

Node collection runs on several machines and is scheduled hourly, but each
source only needs to be read once per local calendar day.  Every successful
read is recorded in Feishu; any machine that sees a source already read today
skips it.

Design rules:
  * state lives only in Feishu - no local state file exists;
  * reads are fail-open: if the table cannot be read, collect as usual;
  * writes are best-effort: a failed state write must never fail collection;
  * state rows are keyed by (源编号, 通道); at most one row per key.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import app_extract, schema
from .feishu import FeishuError

SkipKey = Tuple[str, int]  # (通道, 源编号)
Entry = Tuple[int, str, str, int]  # (源编号, 来源名, 通道, 节点数)


def parse_number(value: Any) -> Optional[int]:
    """Parse a NUMBER field readback (int or numeric string)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def parse_time_ms(value: Any) -> Optional[int]:
    """Parse a DATE_TIME field readback into epoch milliseconds."""
    number = parse_number(value)
    if number is None or number <= 0:
        return None
    return number


def local_day(ms: int) -> Optional[Tuple[int, int, int]]:
    """Local calendar day (year, month, day) for an epoch-millisecond stamp."""
    try:
        return time.localtime(int(ms) / 1000.0)[:3]
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def read_skip_set(client, now_ms: int,
                  log: Callable[[str], None]) -> Set[SkipKey]:
    """Return the (通道, 源编号) keys whose last successful read was today.

    Fail-open: a missing table or any FeishuError yields an empty set, so
    collection proceeds (duplicate reads are preferable to stale data).
    """
    try:
        tables = client.list_tables()
    except FeishuError as exc:
        log(f"collect state unavailable class={exc.kind}")
        return set()
    table_id = None
    for table in tables:
        if table.get("name") == schema.COLLECT_STATE_TABLE_NAME:
            table_id = table.get("table_id")
            break
    if not table_id:
        log("collect state table missing")
        return set()
    try:
        rows = client.list_records(table_id)
    except FeishuError as exc:
        log(f"collect state read failed class={exc.kind}")
        return set()

    today = local_day(now_ms)
    if today is None:
        return set()
    skip: Set[SkipKey] = set()
    for row in rows:
        fields = row.get("fields") or {}
        channel = fields.get("通道")
        if channel not in schema.COLLECT_CHANNEL_OPTIONS:
            continue
        source_number = parse_number(fields.get("源编号"))
        if source_number is None:
            continue
        read_at = parse_time_ms(fields.get("最近读取时间"))
        if read_at is None or local_day(read_at) != today:
            continue
        skip.add((channel, source_number))
    return skip


def success_entries(result: Any) -> List[Entry]:
    """Build state entries from the channels that succeeded this run."""
    name_to_number = {name: number for number, name in schema.SOURCE_BY_INDEX.items()}
    entries: List[Entry] = []
    for key, count in sorted(result.per_channel.items()):
        channel, _, ident = key.partition(":")
        if channel == schema.COLLECT_CHANNEL_SUBSCRIPTION:
            number = name_to_number.get(ident)
            if number is None:
                continue
            entries.append((number, ident, channel, count))
        elif channel == schema.COLLECT_CHANNEL_APP:
            number = app_extract.APP_SOURCE_NUMBER.get(ident)
            if number is None:
                continue
            entries.append((number, schema.SOURCE_BY_INDEX[number], channel, count))
    return entries


def record_success(client, table_id: str, entries: Iterable[Entry],
                   hostname: str, stamp: int,
                   log: Callable[[str], None]) -> None:
    """Upsert one state row per (源编号, 通道).  Never raises."""
    entries = list(entries)
    if not entries:
        return
    try:
        rows = client.list_records(table_id)
    except FeishuError as exc:
        log(f"collect state read failed class={exc.kind}")
        return
    existing: Dict[SkipKey, str] = {}
    for row in rows:
        fields = row.get("fields") or {}
        channel = fields.get("通道")
        source_number = parse_number(fields.get("源编号"))
        record_id = row.get("record_id")
        if channel in schema.COLLECT_CHANNEL_OPTIONS \
                and source_number is not None and record_id:
            existing[(channel, source_number)] = record_id
    for source_number, source, channel, count in entries:
        fields = {
            "源编号": source_number,
            "来源": source,
            "通道": channel,
            "读取机器": hostname,
            "最近读取时间": stamp,
            "节点数": count,
        }
        key = (channel, source_number)
        try:
            record_id = existing.get(key)
            if record_id:
                client.update_record(table_id, record_id, fields)
            else:
                created = client.batch_create(table_id, [{"fields": fields}])
                if created:
                    existing[key] = created[0]
        except FeishuError as exc:
            log(f"collect state write failed source={source_number} "
                f"channel={channel} class={exc.kind}")
