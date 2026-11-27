#!/usr/bin/env python3
"""Build the host's node pool from the Feishu 「节点测速总表」.

A machine deployed from Feishu alone has no subscription URL: every policy
group then reads the file provider this script writes from the master table.
It is rebuilt at bootstrap time and refreshed by every pipeline tick.  The
table is owned by the pipeline's collect side - this script only reads it and
never writes Feishu.

Exit codes:
  0 success (written, or dry-run)
  2 usage / credentials / app_token missing / output path unusable
  3 「节点测速总表」 missing or unreadable
  4 below --min-nodes; the previous output file is kept
  5 output write failed
"""

from __future__ import annotations

import os
import sys

# Allow `python scripts/feishu_pool.py` as well as imports from the repo: the
# node_provider package lives next to this script.
if __package__ in (None, ""):  # pragma: no cover - direct file execution
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import tempfile
from typing import List, Optional

from node_provider import builder, schema
from node_provider import writer
from node_provider.envfile import EnvFileError, read_env_file, resolve_app_token
from node_provider.feishu import FeishuClient, FeishuError
from node_provider.logutil import stderr_logger

NODES_TABLE_NAME = schema.NODES_TABLE_NAME  # "节点测速总表"
EXIT_OK, EXIT_USAGE, EXIT_READ, EXIT_THRESHOLD, EXIT_OUTPUT = 0, 2, 3, 4, 5
TEMP_PREFIX = ".feishu_pool."


def build_pool(rows, *, now_ms=None) -> "builder.BuildResult":
    """Every row of the master table is a candidate: no GPT gate, no region
    filter and no freshness cut (the table's own 7-day tombstone has already
    removed dead rows).  Advertisement rows and structurally invalid rows are
    dropped, semantic duplicates collapse, colliding names get ``[2]``."""
    return builder.build_proxies(rows, all_valid=True, allow_stale=True,
                                 now_ms=now_ms)


def find_nodes_table(tables) -> Optional[str]:
    """The first entry named 「节点测速总表」 that carries a ``table_id``."""
    for table in tables:
        if (isinstance(table, dict) and table.get("name") == NODES_TABLE_NAME
                and table.get("table_id")):
            return table["table_id"]
    return None


def write_pool(path: str, text: str) -> None:
    """Atomic UTF-8 write; 0600 is set before the node credentials exist on
    disk.  The temporary file lives in the target's directory; the parent
    directory must already exist (a missing one raises ``OSError`` and is
    never created)."""
    directory = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=TEMP_PREFIX)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feishu_pool",
        description="Rebuild the host's node pool YAML from the Feishu master table.",
    )
    parser.add_argument("--env-file", required=True,
                        help="credentials env file with FEISHU_APP_ID/FEISHU_APP_SECRET")
    parser.add_argument("--app-token", default=None,
                        help="Feishu Bitable app_token (--app-token > env "
                             "FEISHU_BITABLE_APP_TOKEN > --env-file); no default")
    parser.add_argument("-o", "--output",
                        help="provider YAML path (directory must already exist)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print the summary; never write a file")
    parser.add_argument("--min-nodes", type=int, default=1,
                        help="minimum proxies required to publish (default: 1)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None, client_factory=FeishuClient,
         now_ms: Optional[int] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = stderr_logger(verbose=args.verbose)

    if args.min_nodes < 0:
        parser.error("--min-nodes must be >= 0")
    if not args.dry_run:
        if not args.output:
            parser.error("--output is required unless --dry-run")
        if not os.path.isdir(os.path.dirname(os.path.abspath(args.output))):
            parser.error("output directory does not exist")

    try:
        values = read_env_file(args.env_file)
    except EnvFileError:
        logger.error("credentials unavailable class=EnvFileError")
        return EXIT_USAGE
    app_id = (values.get("FEISHU_APP_ID") or "").strip()
    app_secret = (values.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        logger.error("credentials unavailable class=missing_keys")
        return EXIT_USAGE

    # The table address is the deployment's own: explicit flag, then the
    # process environment, then the env file.  There is no built-in default.
    app_token = resolve_app_token(args.app_token, values, os.environ)
    if not app_token:
        logger.error("app_token_missing: FEISHU_BITABLE_APP_TOKEN "
                     "(--app-token > env > --env-file)")
        return EXIT_USAGE
    logger.add_secret(app_secret)
    logger.add_secret(app_token)

    client = client_factory(app_id, app_secret, app_token, logger.info)
    try:
        try:
            tables = client.list_tables()
        except FeishuError as exc:
            logger.error(f"table list failed class={exc.kind}")
            return EXIT_READ
        table_id = find_nodes_table(tables)
        if not table_id:
            logger.error(f"table missing name={NODES_TABLE_NAME}")
            return EXIT_READ
        try:
            rows = client.list_records(table_id)
        except FeishuError as exc:
            logger.error(f"record read failed class={exc.kind}")
            return EXIT_READ
    except FeishuError as exc:  # any other FeishuError bubbling to the top
        logger.error(f"feishu error class={exc.kind}")
        return EXIT_READ

    result = build_pool(rows, now_ms=now_ms)
    logger.info(result.summary())

    if args.dry_run:
        if result.selected < args.min_nodes:
            logger.warn(f"dry-run blocked: selected={result.selected} "
                        f"min_nodes={args.min_nodes}")
            return EXIT_THRESHOLD
        logger.info("dry-run: no output file written")
        return EXIT_OK

    if result.selected < args.min_nodes:
        logger.warn(f"publish blocked: selected={result.selected} "
                    f"min_nodes={args.min_nodes} previous output kept")
        return EXIT_THRESHOLD

    try:
        write_pool(args.output, writer.render_provider(result.proxies))
    except OSError as exc:
        logger.error(f"output write failed class={type(exc).__name__}")
        return EXIT_OUTPUT
    logger.info(f"pool written nodes={result.selected}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
