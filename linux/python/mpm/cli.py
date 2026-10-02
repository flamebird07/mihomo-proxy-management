"""``mihomo-proxy-management`` command line (D10=B).

The CLI holds no business logic: it builds a :class:`mpm.lifecycle.Context` and
dispatches to the lifecycle engine.  Two properties are non-negotiable:

* secrets are never command-line arguments.  ``--secret-file`` takes a *path*;
  the value itself may only come from that file or this process's environment.
* every byte written to stdout/stderr passes through the sanitizer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import TextIO

from . import lifecycle
from .controller import Controller
from .errors import ExitCode, FailClosed, MpmError
from .executor import Executor
from .paths import Layout
from .preflight import collect_facts
from .sanitize import HOST, sanitize

PROGRAM = "mihomo-proxy-management"
ENV_LOCK = "MPM_LOCK_FILE"
ENV_ROOT = "MPM_ROOT"
ENV_SOURCE_DIR = "MPM_SOURCE_DIR"
ENV_ENTRY_SCRIPT = "MPM_ENTRY_SCRIPT"
ENV_PROC_ROOT = "MPM_PROC_ROOT"

# Search order: source tree (linux/supply/...), then the installed data dir.
_LOCK_CANDIDATES = (
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "supply",
        "mihomo.lock.json",
    ),
    os.path.join("/usr/share", PROGRAM, "supply", "mihomo.lock.json"),
)


def default_lock_path() -> str:
    for candidate in _LOCK_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return _LOCK_CANDIDATES[0]


class _SanitisingWriter:
    """Fallback guard: nothing reaches a stream unsanitised."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def write(self, data: str) -> int:
        return self.stream.write(sanitize(data))

    def flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass

    def isatty(self) -> bool:  # pragma: no cover - UI hint only
        try:
            return self.stream.isatty()
        except Exception:
            return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Deploy and operate mihomo on Ubuntu/Debian (system profile, systemd).",
    )
    parser.add_argument("--version", action="store_true", help="print the project version")
    sub = parser.add_subparsers(dest="command")

    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.add_argument(
            "--secret-file",
            help="path (never a URL or secret) to the 0600 subscription file",
        )
        item.add_argument("--root", help="alternate filesystem root (staging/tests only)")
        item.add_argument("--lock-file", help="override the supply lock path")
        item.add_argument("--json", action="store_true", help="machine-readable output")
        item.add_argument("--quiet", action="store_true", help="suppress progress lines")
        return item

    common("preflight", "report support matrix, TUN capability and pinned assets")
    install = common("install", "verify + install binary, render config, install unit, start")
    install.add_argument("--enable", action="store_true", help="also systemd-enable the unit")
    install.add_argument("--skip-download", action="store_true", help="reuse the installed binary")
    configure = common("configure", "re-render and validate config (idempotent)")
    configure.add_argument(
        "--check-only",
        action="store_true",
        help="validate the on-disk config without writing (used by ExecStartPre)",
    )
    for name, text in (
        ("start", "start the unit (never changes enable state)"),
        ("stop", "stop the unit via systemctl stop"),
        ("restart", "stop, confirm stopped, start, authenticated readiness"),
        ("status", "active vs enabled, mode, DEGRADED reasons, providers"),
        ("test", "local functional test against the authenticated controller"),
    ):
        common(name, text)
    refresh = common("update-subscription", "refresh provider(s) through the controller API")
    refresh.add_argument("--provider", help="only refresh this provider name")
    uninstall = common("uninstall", "remove unit, binary and secret material; keep overrides.d")
    uninstall.add_argument("--purge", action="store_true", help="remove every project path")
    uninstall.add_argument("--yes", action="store_true", help="confirm --purge")
    uninstall.add_argument("--dry-run", action="store_true", help="report without deleting")
    return parser


def make_context(args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> lifecycle.Context:
    root = args.root or os.environ.get(ENV_ROOT, "/")
    layout = Layout(root)
    lock_path = args.lock_file or os.environ.get(ENV_LOCK) or default_lock_path()
    proc_root = os.environ.get(ENV_PROC_ROOT, "/proc")
    executor = Executor()
    facts = collect_facts(root=root)
    return lifecycle.Context(
        layout=layout,
        executor=executor,
        facts=facts,
        out=stdout,
        err=stderr,
        lock_path=lock_path,
        environ=dict(os.environ),
        secret_file=args.secret_file,
        enable=getattr(args, "enable", False),
        controller_factory=lambda secret: Controller(secret=secret),
        proc_root=proc_root,
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def dispatch(ctx: lifecycle.Context, args: argparse.Namespace) -> lifecycle.Report:
    command = args.command
    if command == "preflight":
        return lifecycle.run_preflight(ctx)
    if command == "install":
        return lifecycle.install(
            ctx, skip_download=bool(getattr(args, "skip_download", False)), quiet=args.quiet
        )
    if command == "configure":
        return lifecycle.configure(
            ctx, check_only=bool(getattr(args, "check_only", False)), quiet=args.quiet
        )
    if command == "start":
        return lifecycle.start(ctx, quiet=args.quiet)
    if command == "stop":
        return lifecycle.stop(ctx, quiet=args.quiet)
    if command == "restart":
        return lifecycle.restart(ctx, quiet=args.quiet)
    if command == "status":
        return lifecycle.status(ctx)
    if command == "test":
        return lifecycle.run_test(ctx)
    if command == "update-subscription":
        return lifecycle.update_subscription(ctx, provider=getattr(args, "provider", None))
    if command == "uninstall":
        return lifecycle.uninstall(
            ctx,
            purge=bool(getattr(args, "purge", False)),
            assume_yes=bool(getattr(args, "yes", False)),
        )
    raise FailClosed(f"unknown command: {sanitize(str(command))}")


def emit(ctx: lifecycle.Context, report: lifecycle.Report, *, as_json: bool) -> int:
    if as_json:
        ctx.out.write(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    else:
        if not ctx.quiet:
            for message in report.messages:
                ctx.say(message)
        for warning in report.warnings:
            ctx.err.write(sanitize("[WARN] " + warning) + "\n")
        if report.status == "DEGRADED":
            ctx.say("STATUS: DEGRADED")
        elif report.status in ("FAILED", "INACTIVE"):
            ctx.say(f"STATUS: {report.status}")
    if report.status == "FAILED":
        return int(report.exit_code or ExitCode.FAILURE)
    return 0


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None,
         context_factory=None, executor: Executor | None = None) -> int:
    out = _SanitisingWriter(stdout or sys.stdout)
    err = _SanitisingWriter(stderr or sys.stderr)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        out.write(f"{PROGRAM} {__version__}\n")
        return 0
    if not args.command:
        out.write("usage: see --help; a subcommand is required\n")
        return ExitCode.FAILURE

    try:
        if context_factory is not None:
            ctx = context_factory(args)
        else:
            ctx = make_context(args, stdout=out, stderr=err)
            if executor is not None:
                ctx.executor = executor
        ctx.out = out
        ctx.err = err
        ctx.quiet = bool(getattr(args, "quiet", False))
        report = dispatch(ctx, args)
    except MpmError as exc:
        err.write(sanitize(f"[FAIL] {exc.message}") + "\n")
        return int(exc.exit_code or ExitCode.FAILURE)
    except KeyboardInterrupt:  # pragma: no cover
        err.write("[FAIL] interrupted\n")
        return 130
    except Exception as exc:  # last-resort guard: never leak exception text
        err.write(HOST.text(f"[FAIL] unexpected {type(exc).__name__}"))
        err.write("\n")
        return 1

    try:
        return emit(ctx, report, as_json=bool(getattr(args, "json", False)))
    except Exception:  # pragma: no cover - output stream failure
        return 1


__all__ = ["PROGRAM", "build_parser", "dispatch", "emit", "main", "make_context"]
