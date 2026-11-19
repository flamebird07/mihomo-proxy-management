"""CLI entry point.

Usage (from ``scripts/``, or with ``scripts`` on PYTHONPATH):

  # default: rebuild the provider YAML from 「有效节点」
  python -m node_provider --dry-run -o providers/xigua-provider.yaml
  python -m node_provider -o providers/xigua-provider.yaml
  python -m node_provider --region 美国 --min-nodes 3 -o ...

  # collect: extract nodes and replace 「节点测速总表」
  python -m node_provider --collect --sources-env config/subscription.env --dry-run
  python -m node_provider --collect --from-app source1,source3 --dry-run

Exit codes:
  0 success
  2 usage / configuration / credential / output-path error
  3 「有效节点」 read failure, or collect produced no nodes
  4 below --min-nodes; previous output file kept
  5 partial: some rows invalid, but the result was written
"""

from __future__ import annotations

import os
import sys

# Allow `python scripts/node_provider/__main__.py` as well as -m.
if __package__ in (None, ""):  # pragma: no cover - direct file execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from node_provider.__main__ import main as _main

    sys.exit(_main())

import argparse
from pathlib import Path
from typing import List, Optional

from . import app_extract, schema
from .envfile import EnvFileError, read_env_file
from .feishu import FeishuClient
from .lock import FileLock, LockBusy
from .logutil import stderr_logger
from .runner import (DEFAULT_APP_TOKEN, DEFAULT_ENV_FILE, EXIT_USAGE,
                     Settings, run, run_collect)
from .subscription import DEFAULT_FETCH_PROXY

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK_FILE = str(PROJECT_ROOT / "logs" / "node_provider.lock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node_provider",
        description="Extract nodes to Feishu, or rebuild a mihomo provider YAML.",
    )
    parser.add_argument("--collect", action="store_true",
                        help="extract nodes and replace 「节点测速总表」 (default: provide)")
    parser.add_argument("--from-app", default="",
                        help="app extraction channels, comma separated: "
                             "source1,source3,source4")
    parser.add_argument("--sources-env", default=None,
                        help="subscription env file (KEY=VALUE) for --collect")
    parser.add_argument("--timeout", type=int, default=30,
                        help="subscription fetch timeout in seconds (--collect)")
    parser.add_argument("--fetch-proxy", default=DEFAULT_FETCH_PROXY,
                        help="HTTP proxy for subscription fetch (--collect); "
                             "empty string = direct connection")
    parser.add_argument("--supplement-yaml", action="append", default=[],
                        help="Local provider YAML merged into source 1 (repeatable)")
    parser.add_argument("--source1-pid", type=int, default=None,
                        help="PID of the source1 AtlasCore process (--from-app source1)")
    parser.add_argument("--vault-dir", default=None,
                        help="Ninja Desktop vault directory (--from-app source3)")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE,
                        help="node_provider collect lock file")
    parser.add_argument("-o", "--output",
                        help="provider YAML path (directory must already exist)")
    parser.add_argument("--dry-run", action="store_true",
                        help="full extraction/rebuild; never write Feishu or a file")
    parser.add_argument("--app-token", default=DEFAULT_APP_TOKEN,
                        help="Feishu Bitable app_token (default: project app)")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help="credentials env file with FEISHU_APP_ID/FEISHU_APP_SECRET")
    parser.add_argument("--table-name", default=schema.VALID_TABLE_NAME,
                        help="source table name (default: 有效节点)")
    parser.add_argument("--all-valid", action="store_true",
                        help="do not filter on GPT可用=true (default: strict)")
    parser.add_argument("--region", action="append",
                        choices=list(schema.REGION_FILTER_CHOICES),
                        help="only keep this 地区组 (repeatable)")
    parser.add_argument("--min-nodes", type=int, default=1,
                        help="minimum proxies required to publish (default: 1)")
    parser.add_argument("--max-age-minutes", type=int, default=1440,
                        help="stale threshold for 同步时间 (default: 1440)")
    parser.add_argument("--allow-stale", action="store_true",
                        help="include rows whose 同步时间 is old or missing")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _credentials(path: str, logger):
    try:
        values = read_env_file(path)
    except EnvFileError as exc:
        logger.error(f"credentials unavailable class={type(exc).__name__}")
        return None
    app_id = (values.get("FEISHU_APP_ID") or "").strip()
    app_secret = (values.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        logger.error("credentials unavailable class=missing_keys")
        return None
    logger.add_secret(app_secret)
    return app_id, app_secret


def _parse_app_channels(value: str, parser: argparse.ArgumentParser) -> List[str]:
    names = [item.strip() for item in value.split(",") if item.strip()]
    for name in names:
        if name not in app_extract.APP_CHOICES:
            parser.error(f"unknown --from-app channel: {name}")
    return list(dict.fromkeys(names))


def main(argv: Optional[List[str]] = None, client_factory=FeishuClient,
         now_ms: Optional[int] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = stderr_logger(verbose=args.verbose)

    if args.min_nodes < 0 or args.max_age_minutes < 0:
        parser.error("--min-nodes and --max-age-minutes must be >= 0")
    if args.collect:
        if args.output:
            parser.error("--output is not valid with --collect")
        from_app = _parse_app_channels(args.from_app, parser)
        if not from_app and not args.sources_env:
            parser.error("--collect requires --sources-env and/or --from-app")
    else:
        if args.from_app or args.sources_env:
            parser.error("--from-app/--sources-env require --collect")
        from_app = []
        if not args.dry_run:
            if not args.output:
                parser.error("--output is required unless --dry-run")
            directory = Path(args.output).parent
            if not Path(directory or ".").is_dir():
                parser.error("output directory does not exist")

    credentials = _credentials(args.env_file, logger)
    if credentials is None:
        if not (args.collect and args.dry_run):
            return EXIT_USAGE
        client = None
    else:
        client = client_factory(credentials[0], credentials[1], args.app_token)

    settings = Settings(
        output=args.output,
        table_name=args.table_name,
        all_valid=args.all_valid,
        regions=list(args.region or ()),
        min_nodes=args.min_nodes,
        max_age_minutes=args.max_age_minutes,
        allow_stale=args.allow_stale,
        dry_run=args.dry_run,
        collect=args.collect,
        from_app=from_app,
        sources_env=args.sources_env,
        timeout=args.timeout,
        fetch_proxy=args.fetch_proxy,
        source1_pid=args.source1_pid,
        vault_dir=args.vault_dir,
        supplement_yaml=list(args.supplement_yaml),
    )

    if not args.collect:
        return run(settings, client, logger, now_ms=now_ms)

    lock = FileLock(args.lock_file)
    try:
        lock.acquire()
    except LockBusy:
        logger.error("another node_provider instance is running")
        return EXIT_USAGE
    try:
        return run_collect(settings, client, logger, now_ms=now_ms)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
