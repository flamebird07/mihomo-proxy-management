"""CLI entry point.

Usage (from the project root, with scripts on PYTHONPATH, or from scripts/):

  python -m node_ops --dry-run
  python -m node_ops --ensure-tables
  python -m node_ops                      # explicit write run

node_ops never fetches a subscription and never extracts nodes from an app.
It reads the node identity/config columns of 「节点测速总表」, probes each
node, and writes back only the measurement columns plus 「有效节点」.

Exit codes:
  0 success (dry-run or write)
  2 usage / configuration / credential error
  3 no usable rows in 「节点测速总表」
  4 probe failure
  5 table write failure
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from .envfile import EnvFileError, read_env_file, resolve_app_token
from .feishu import FeishuClient, FeishuError
from .lock import FileLock, LockBusy
from .logutil import Logger
from .probe import default_core_path
from .runner import DEFAULT_APP_TOKEN, DEFAULT_ENV_FILE, RunError, Settings, run

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_FILE = str(PROJECT_ROOT / "logs" / "node_ops.log")
DEFAULT_LOCK_FILE = str(PROJECT_ROOT / "logs" / "node_ops.lock")

#: Logged (key name only) when the table address is configured nowhere.  Every
#: node_ops mode reads Feishu, so there is no token-less path.
APP_TOKEN_MISSING = ("app_token_missing: FEISHU_BITABLE_APP_TOKEN "
                     "(--app-token > env > --env-file)")


class AppTokenMissing(Exception):
    """The Bitable app_token is configured nowhere; nothing was contacted."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node_ops",
        description="Probe nodes read from Feishu, classify and advise; data lives in Feishu only.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="full read/probe/classify, never write Feishu")
    parser.add_argument("--ensure-tables", action="store_true",
                        help="only create/patch the two Feishu tables, then exit")
    parser.add_argument("--app-token", default=DEFAULT_APP_TOKEN,
                        help="Feishu Bitable app_token (--app-token > env "
                             "FEISHU_BITABLE_APP_TOKEN > --env-file); no default")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help="credentials env file with FEISHU_APP_ID/FEISHU_APP_SECRET")
    parser.add_argument("--core", default=default_core_path(),
                        help="mihomo core binary for the isolated probe")
    parser.add_argument("--probe-timeout-ms", type=int, default=5000,
                        help="per-target probe timeout (ms)")
    parser.add_argument("--workers", type=int, default=4, help="probe concurrency (max 4)")
    parser.add_argument("--total-budget", type=float, default=600.0,
                        help="delay-probe wall-clock budget (s)")
    parser.add_argument("--ipinfo-budget", type=float, default=600.0,
                        help="region-probe wall-clock budget (s)")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _credentials(settings: Settings, log: Logger,
                 environ: Optional[dict] = None) -> Optional[FeishuClient]:
    try:
        values = read_env_file(settings.env_file)
    except EnvFileError as exc:
        log.warn(f"credentials unavailable class={type(exc).__name__}")
        return None
    app_id = (values.get("FEISHU_APP_ID") or "").strip()
    app_secret = (values.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        log.warn("credentials unavailable class=missing_keys")
        return None
    log.add_secret(app_secret)
    # The table address is the deployment's own: explicit flag, then the
    # process environment, then the env file.  There is no built-in default.
    app_token = resolve_app_token(settings.app_token, values,
                                  os.environ if environ is None else environ)
    if not app_token:
        log.error(APP_TOKEN_MISSING)
        raise AppTokenMissing(APP_TOKEN_MISSING)
    log.add_secret(app_token)
    return FeishuClient(app_id, app_secret, app_token, log.info)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings(
        env_file=args.env_file,
        app_token=args.app_token or "",
        workers=args.workers,
        total_budget_s=args.total_budget,
        ipinfo_budget_s=args.ipinfo_budget,
        timeout_ms=args.probe_timeout_ms,
        core=args.core,
        dry_run=args.dry_run,
        ensure_only=args.ensure_tables,
        log_file=args.log_file,
        verbose=args.verbose,
    )
    log = Logger(args.log_file, verbose=args.verbose)
    try:
        client = _credentials(settings, log)
    except AppTokenMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if client is None:
        print("error: Feishu credentials are required", file=sys.stderr)
        return 2

    lock = FileLock(args.lock_file)
    try:
        lock.acquire()
    except LockBusy:
        print("error: another node_ops instance is running", file=sys.stderr)
        return 2

    try:
        summary = run(settings, log, client=client)
        print(
            f"collected={summary.collected} probed={summary.probed} "
            f"alive={summary.alive} valid={summary.valid} "
            f"skipped_invalid={summary.skipped_invalid} "
            f"dry_run={'yes' if summary.dry_run else 'no'} "
            f"wrote_nodes={summary.wrote_nodes} wrote_valid={summary.wrote_valid}"
        )
        return 0
    except EnvFileError as exc:
        log.error(f"configuration error class={type(exc).__name__}")
        print("error: configuration error", file=sys.stderr)
        return 2
    except FeishuError as exc:
        log.error(f"feishu error class={exc.kind}")
        print(f"error: feishu {exc.kind}", file=sys.stderr)
        return 5
    except RunError as exc:
        log.error(f"run failed class={str(exc).split(' (')[0]}")
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
