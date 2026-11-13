"""``mihomo-proxy-management`` command line for macOS.

The CLI holds no business logic: it builds a :class:`mpm_macos.lifecycle.Context`
and dispatches.  Secrets are never command-line arguments - they come from the
0600 file or the process environment - and every byte written to stdout/stderr
passes through the sanitizer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from typing import TextIO

from mpm_common.errors import ExitCode, FailClosed, MpmError
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST, sanitize

from . import __version__, lifecycle, preflight as preflight_mod
from .paths import Layout

PROGRAM = "mihomo-proxy-management"
ENV_ROOT = "MPM_ROOT"
ENV_PYTHON = "MPM_PYTHON"

# Every command that can change config, launchd, the proxy snapshot or files.
# They are serialised by the lifecycle lock; the lifecycle functions they call
# (install -> start -> fallback, restart -> stop + start) never take it again.
MUTATING_COMMANDS = frozenset(
    {"install", "configure", "start", "stop", "restart", "uninstall"}
)


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
        description="Deploy and operate mihomo on macOS (arm64, LaunchDaemon).",
    )
    parser.add_argument("--version", action="store_true", help="print the project version")
    sub = parser.add_subparsers(dest="command")

    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--root", help="alternate filesystem root (staging/tests only)")
        item.add_argument("--json", action="store_true", help="machine-readable output")
        item.add_argument("--quiet", action="store_true", help="suppress progress lines")
        return item

    common("preflight", "report Darwin/arm64/root and command capabilities")
    install = common("install", "install packages, config and LaunchDaemon, then start")
    install.add_argument("--no-start", action="store_true", help="install without starting")
    configure = common("configure", "re-render and validate config (idempotent)")
    configure.add_argument(
        "--mode",
        choices=("tun", "mixed"),
        help="force a mode (default: the recorded mode, then tun)",
    )
    for name, text in (
        ("start", "start the LaunchDaemon and verify TUN (falls back to mixed)"),
        ("stop", "bootout the LaunchDaemon and restore the system proxy"),
        ("restart", "stop, confirm stopped, start, verify"),
        ("status", "launchd/process/mode/proxy/controller status"),
        ("health", "controller readiness, provider health and live nodes"),
    ):
        common(name, text)
    uninstall = common("uninstall", "remove the LaunchDaemon and program files")
    uninstall.add_argument(
        "--purge", action="store_true", help="also remove config, provider, logs and state"
    )
    return parser


def make_context(args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> lifecycle.Context:
    root = args.root or os.environ.get(ENV_ROOT, "/")
    layout = Layout(root)
    facts = preflight_mod.collect_facts()
    # mihomo is installed under libexec (not on PATH); a fresh install can point
    # at an explicit source binary.  Either satisfies the preflight check.
    binary = os.environ.get("MPM_MIHOMO_BINARY") or (
        layout.binary if os.path.isfile(layout.binary) else ""
    )
    if binary:
        commands = dict(facts.commands)
        commands["mihomo"] = binary
        facts = replace(facts, commands=commands)
    return lifecycle.Context(
        layout=layout,
        facts=facts,
        executor=Executor(),
        out=stdout,
        err=stderr,
        environ=dict(os.environ),
        system_profile=True,
        python=os.environ.get(ENV_PYTHON) or sys.executable,
    )


def dispatch(ctx: lifecycle.Context, args: argparse.Namespace) -> lifecycle.Report:
    """Single entry point; state-changing commands run under the lifecycle lock.

    The lock is non-blocking: a concurrent operation is refused with a clear
    error and a non-zero exit instead of interleaving config, launchd and
    proxy-snapshot changes.
    """
    if args.command in MUTATING_COMMANDS:
        with ctx.lifecycle_guard():
            return _dispatch(ctx, args)
    return _dispatch(ctx, args)


def _dispatch(ctx: lifecycle.Context, args: argparse.Namespace) -> lifecycle.Report:
    command = args.command
    if command == "preflight":
        return lifecycle.run_preflight(ctx)
    if command == "install":
        return lifecycle.install(ctx, quiet=args.quiet, skip_start=bool(args.no_start))
    if command == "configure":
        return lifecycle.configure(ctx, quiet=args.quiet, mode=getattr(args, "mode", None))
    if command == "start":
        return lifecycle.start(ctx, quiet=args.quiet)
    if command == "stop":
        return lifecycle.stop(ctx, quiet=args.quiet)
    if command == "restart":
        return lifecycle.restart(ctx, quiet=args.quiet)
    if command == "status":
        return lifecycle.status(ctx)
    if command == "health":
        return lifecycle.health(ctx)
    if command == "uninstall":
        return lifecycle.uninstall(ctx, purge=bool(args.purge), quiet=args.quiet)
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
        if report.status in ("DEGRADED", "FAILED", "INACTIVE"):
            ctx.say(f"STATUS: {report.status}")
    if report.status == "FAILED":
        return int(report.exit_code or ExitCode.FAILURE)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    context_factory=None,
) -> int:
    out = _SanitisingWriter(stdout or sys.stdout)
    err = _SanitisingWriter(stderr or sys.stderr)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        out.write(f"{PROGRAM} {__version__}\n")
        return 0
    if not args.command:
        out.write("usage: see --help; a subcommand is required\n")
        return ExitCode.FAILURE

    try:
        ctx = context_factory(args) if context_factory is not None else make_context(
            args, stdout=out, stderr=err
        )
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
