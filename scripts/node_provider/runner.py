"""Orchestration for both node_provider modes.

Default (provide): read 「有效节点」 -> rebuild -> atomic write.
``--collect``: extract nodes (subscription and/or app) -> full replacement of
「节点测速总表」 identity/config columns.

Side effects: Feishu network I/O and, unless ``--dry-run``, either one atomic
YAML replacement or one Feishu table replacement.  No mihomo controller is
contacted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import builder, collect as collect_mod, schema
from .envfile import EnvFileError, read_env_file
from .feishu import FeishuError, ensure_table, replace_all
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

    result = collect_mod.collect(
        values,
        timeout=settings.timeout,
        fetcher=fetcher,
        from_app=list(settings.from_app),
        source1_pid=settings.source1_pid,
        source1_reader=source1_reader,
        vault_dir=settings.vault_dir,
        hostname=hostname,
        warn=logger.warn,
    )
    logger.info(result.summary())
    if not result.nodes:
        logger.error("no nodes collected from any channel")
        return EXIT_READ

    stamp = now_ms if now_ms is not None else collect_mod.now_ms()
    records = collect_mod.build_records(result.nodes, stamp)

    if settings.dry_run:
        logger.info(f"dry-run: no Feishu write; node_rows={len(records)}")
        return EXIT_OK

    try:
        nodes_table = ensure_table(client, schema.NODES_TABLE, logger.info)
        created = replace_all(client, nodes_table["table_id"], records,
                              logger.info, allow_empty=False)
    except FeishuError as exc:
        logger.error(f"table write failed class={exc.kind}")
        return EXIT_READ
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"table write failed class={type(exc).__name__}")
        return EXIT_READ
    logger.info(f"collect written nodes={created}")
    return EXIT_OK
