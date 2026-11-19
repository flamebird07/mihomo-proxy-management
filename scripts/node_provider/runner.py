"""Orchestration for both node_provider modes.

Default (provide): read 「有效节点」 -> rebuild -> atomic write.
``--collect``: extract nodes (subscription and/or app) -> per-source
replacement of 「节点测速总表」 identity/config columns, skipping any source
already read today (recorded in 「采集状态」 by any machine).

Side effects: Feishu network I/O and, unless ``--dry-run``, either one atomic
YAML replacement or per-source Feishu row replacement plus a state upsert.  No
mihomo controller is contacted.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import builder, collect as collect_mod, collect_state, schema
from .envfile import EnvFileError, read_env_file
from .feishu import FeishuError, ensure_table, replace_source_rows
from .logutil import Logger
from .subscription import DEFAULT_FETCH_PROXY, fetch_subscription
from .writer import OutputError, atomic_write

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_READ = 3
EXIT_THRESHOLD = 4
EXIT_PARTIAL = 5

DEFAULT_APP_TOKEN = "URo9bAsd5azYe5swAmtclIbEnNh"
DEFAULT_ENV_FILE = r"C:\Users\Administrator\.hermes\.env"


@dataclass
class Settings:
    output: Optional[str] = None
    table_name: str = schema.VALID_TABLE_NAME
    all_valid: bool = False
    regions: List[str] = field(default_factory=list)
    min_nodes: int = 1
    max_age_minutes: int = builder.DEFAULT_MAX_AGE_MINUTES
    allow_stale: bool = False
    dry_run: bool = False
    # collect mode
    collect: bool = False
    from_app: List[str] = field(default_factory=list)
    sources_env: Optional[str] = None
    timeout: int = 30
    fetch_proxy: str = DEFAULT_FETCH_PROXY
    source1_pid: Optional[int] = None
    vault_dir: Optional[str] = None


def read_rows(client, settings: Settings, logger: Logger):
    """Return rows or None; a None result means "read failure" (exit 3)."""
    try:
        tables = client.list_tables()
    except FeishuError as exc:
        logger.error(f"table list failed class={exc.kind}")
        return None
    table_id = builder.select_table(tables, settings.table_name)
    if not table_id:
        logger.error(f"table missing name={settings.table_name} "
                     f"available={','.join(builder.table_names(tables))}")
        return None
    try:
        return client.list_records(table_id)
    except FeishuError as exc:
        logger.error(f"record read failed table_id={table_id} class={exc.kind}")
        return None


def run(settings: Settings, client, logger: Logger, now_ms: Optional[int] = None) -> int:
    rows = read_rows(client, settings, logger)
    if rows is None:
        return EXIT_READ

    result = builder.build_proxies(
        rows,
        all_valid=settings.all_valid,
        regions=settings.regions,
        max_age_minutes=settings.max_age_minutes,
        allow_stale=settings.allow_stale,
        now_ms=now_ms,
    )
    logger.info(result.summary())
    if result.invalid:
        logger.warn(f"invalid rows skipped count={result.invalid}")

    if settings.dry_run:
        logger.info("dry-run: no output file written")
        if result.selected < settings.min_nodes:
            logger.warn(f"below threshold selected={result.selected} "
                        f"min_nodes={settings.min_nodes}")
            return EXIT_THRESHOLD
        return EXIT_OK

    if result.selected < settings.min_nodes:
        logger.warn(f"publish blocked: selected={result.selected} "
                    f"min_nodes={settings.min_nodes} previous output kept")
        return EXIT_THRESHOLD

    try:
        atomic_write(settings.output, result.proxies)
    except OutputError as exc:
        logger.error(f"output invalid class={type(exc).__name__}")
        return EXIT_USAGE
    except OSError as exc:
        logger.error(f"output write failed class={type(exc).__name__}")
        return EXIT_READ

    logger.info(f"provider written nodes={result.selected} "
                f"invalid={result.invalid}")
    return EXIT_PARTIAL if result.invalid else EXIT_OK


def run_collect(settings: Settings, client, logger: Logger, *,
                env_values: Optional[Dict[str, str]] = None,
                fetcher=fetch_subscription,
                source1_reader: Optional[Any] = None,
                hostname: Optional[str] = None,
                now_ms: Optional[int] = None) -> int:
    """Extract nodes and replace the total table's identity/config columns."""
    if not settings.dry_run and client is None:
        logger.error("collect write mode requires Feishu credentials")
        return EXIT_USAGE

    values = env_values
    if values is None:
        if settings.sources_env:
            try:
                values = read_env_file(settings.sources_env)
            except EnvFileError as exc:
                logger.error(f"sources env unavailable class={type(exc).__name__}")
                return EXIT_USAGE
        else:
            values = {}

    if fetcher is fetch_subscription:
        proxy = settings.fetch_proxy

        def fetcher(url, ua, timeout, _proxy=proxy):
            return fetch_subscription(url, ua, timeout, fetch_proxy=_proxy)

    stamp = now_ms if now_ms is not None else collect_mod.now_ms()
    host = hostname if hostname is not None else socket.gethostname()

    # Sources already read today on any machine are skipped.  Dry-run still
    # performs this read so the reported plan matches a real run, but never
    # creates the state table or writes to it.
    skip: set = set()
    if client is not None:
        skip = collect_state.read_skip_set(client, stamp, logger.warn)

    result = collect_mod.collect(
        values,
        timeout=settings.timeout,
        fetcher=fetcher,
        from_app=list(settings.from_app),
        source1_pid=settings.source1_pid,
        source1_reader=source1_reader,
        vault_dir=settings.vault_dir,
        hostname=hostname,
        skip=skip,
        warn=logger.warn,
    )
    logger.info(result.summary())
    if not result.nodes:
        if result.skipped:
            # Daily cadence: everything readable today was already read; the
            # remaining failures (e.g. a banned source) are reported in the
            # summary and retried next hour, not treated as a collect failure.
            logger.info("collect skipped all sources read_today "
                        f"failed={len(result.failed)}")
            return EXIT_OK
        logger.error("no nodes collected from any channel")
        return EXIT_READ

    records = collect_mod.build_records(result.nodes, stamp)
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(int(record["fields"]["源编号"]), []).append(record)

    if settings.dry_run:
        sources = ",".join(str(number) for number in sorted(grouped))
        logger.info(f"dry-run: no Feishu write; node_rows={len(records)} "
                    f"replace_sources=[{sources}]")
        return EXIT_OK

    try:
        nodes_table = ensure_table(client, schema.NODES_TABLE, logger.info)
    except FeishuError as exc:
        logger.error(f"table write failed class={exc.kind}")
        return EXIT_READ
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"table write failed class={type(exc).__name__}")
        return EXIT_READ

    state_table_id = None
    try:
        state_table_id = ensure_table(
            client, schema.COLLECT_STATE_TABLE, logger.info)["table_id"]
    except FeishuError as exc:
        logger.warn(f"collect state table unavailable class={exc.kind}")

    entries_by_source: Dict[int, List[Any]] = {}
    for entry in collect_state.success_entries(result):
        entries_by_source.setdefault(entry[0], []).append(entry)

    total_created = 0
    for source_number in sorted(grouped):
        source_records = grouped[source_number]
        try:
            created = replace_source_rows(
                client, nodes_table["table_id"], source_number,
                source_records, logger.info)
        except FeishuError as exc:
            logger.error(f"table write failed source={source_number} "
                         f"class={exc.kind}")
            return EXIT_READ
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"table write failed source={source_number} "
                         f"class={type(exc).__name__}")
            return EXIT_READ
        total_created += created
        if state_table_id and created == len(source_records):
            collect_state.record_success(
                client, state_table_id, entries_by_source.get(source_number, ()),
                host, stamp, logger.warn)

    logger.info(f"collect written nodes={total_created}")
    return EXIT_OK
