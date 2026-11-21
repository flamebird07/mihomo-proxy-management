"""Cross-machine rolling-24h collect state in the Feishu 「采集状态」 table.

Node collection runs on several machines and is scheduled hourly, but each
source only needs to be attempted once per rolling 24-hour window.  Every
attempt - successful or failed - is recorded in Feishu; a machine that sees
its own attempt (or any subscription attempt) less than 24 hours old skips
that source.  App channels are host-local and are therefore keyed per host.

Design rules:
  * state lives only in Feishu - no local state file exists;
  * reads are fail-open: if the table cannot be read, collect as usual;
  * writes are best-effort: a failed state write must never fail collection;
  * subscription rows are keyed by (源编号, 通道) and shared by all machines;
  * app rows add 读取机器 to the key: an app cache is local to one machine, so
    one host's attempt must never suppress another host's app extraction;
  * 最近读取时间 is the last *attempt* time; a failed attempt records 节点数=0.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import app_extract, schema
from .feishu import FeishuError

SkipKey = Tuple[str, int]  # (通道, 源编号)
Entry = Tuple[int, str, str, int]  # (源编号, 来源名, 通道, 节点数)

SKIP_WINDOW_MS = 24 * 60 * 60 * 1000  # rolling window, not a calendar day


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


def read_skip_set(client, now_ms: int,
                  log: Callable[[str], None],
                  hostname: Optional[str] = None) -> Set[SkipKey]:
    """Return the (通道, 源编号) keys attempted less than 24 hours ago.

    The window rolls with wall-clock time: a stamp 23h59m old is skipped, one
    exactly 24h old is not, regardless of calendar-day boundaries.  Rows whose
    timestamp is missing, unparsable or non-positive are ignored.

    Subscription rows are shared: any machine's recent attempt skips the fetch
    everywhere.  App rows are host-local: only a row whose 读取机器 matches
    *hostname* skips the extraction, so a failed attempt on a host without the
    app (e.g. macOS) never suppresses another host's app collection.

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

    skip: Set[SkipKey] = set()
    for row in rows:
        fields = row.get("fields") or {}
        channel = fields.get("通道")
        if channel not in schema.COLLECT_CHANNEL_OPTIONS:
            continue
        if channel == schema.COLLECT_CHANNEL_APP:
            reader = str(fields.get("读取机器") or "").strip()
            if not hostname or reader != hostname:
                continue
        source_number = parse_number(fields.get("源编号"))
        if source_number is None:
            continue
        read_at = parse_time_ms(fields.get("最近读取时间"))
        if read_at is None:
            continue
        if now_ms - read_at < SKIP_WINDOW_MS:
            skip.add((channel, source_number))
    return skip


def _entry_for(channel: str, ident: str, count: int) -> Optional[Entry]:
    if channel == schema.COLLECT_CHANNEL_SUBSCRIPTION:
        number = {name: num for num, name in schema.SOURCE_BY_INDEX.items()}.get(ident)
        if number is None:
            return None
        return (number, ident, channel, count)
    if channel == schema.COLLECT_CHANNEL_APP:
        number = app_extract.APP_SOURCE_NUMBER.get(ident)
        if number is None or number not in schema.SOURCE_BY_INDEX:
            return None
        return (number, schema.SOURCE_BY_INDEX[number], channel, count)
    return None


def attempt_entries(result: Any) -> List[Entry]:
    """Build state entries from every channel attempted this run.

    Successful channels carry their node count; each channel in ``failed``
    yields an entry with 节点数=0 so the 24-hour window also suppresses
    retries after a failure.  Skipped channels produce no entry.
    """
    entries: List[Entry] = []
    seen: Set[str] = set()

    def add(key: str, count: int) -> None:
        if key in seen:
            return
        channel, _, ident = key.partition(":")
        entry = _entry_for(channel, ident, count)
        if entry is not None:
            seen.add(key)
            entries.append(entry)

    for key, count in sorted(result.per_channel.items()):
        add(key, count)
    for key in sorted(result.failed):
        add(key, 0)
    return entries


def record_attempts(client, table_id: str, entries: Iterable[Entry],
                    hostname: str, stamp: int,
                    log: Callable[[str], None]) -> None:
    """Upsert one state row per subscription (源编号, 通道) and per
    app (源编号, 通道, 读取机器).

    App rows are host-scoped because the cache lives on one machine; keeping
    each host's row separate prevents two hosts from overwriting each other's
    rolling-24h clock.  Never raises; a failed write is logged and collection
    continues.
    """
    entries = list(entries)
    if not entries:
        return
    try:
        rows = client.list_records(table_id)
    except FeishuError as exc:
        log(f"collect state read failed class={exc.kind}")
        return

    def _key(channel: str, source_number: int, reader: str) -> tuple:
        if channel == schema.COLLECT_CHANNEL_APP:
            return (channel, source_number, reader)
        return (channel, source_number, "")

    existing: Dict[tuple, str] = {}
    for row in rows:
        fields = row.get("fields") or {}
        channel = fields.get("通道")
        source_number = parse_number(fields.get("源编号"))
        record_id = row.get("record_id")
        if channel in schema.COLLECT_CHANNEL_OPTIONS \
                and source_number is not None and record_id:
            reader = str(fields.get("读取机器") or "").strip()
            existing[_key(channel, source_number, reader)] = record_id
    for source_number, source, channel, count in entries:
        fields = {
            "源编号": source_number,
            "来源": source,
            "通道": channel,
            "读取机器": hostname,
            "最近读取时间": stamp,
            "节点数": count,
        }
        key = _key(channel, source_number, hostname)
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
