"""``python scripts/deploy/__main__.py`` - the cross-platform bootstrap command.

    python scripts/deploy/__main__.py --platform macos [--dry-run] [--no-start]
        [--skip-supply] [--skip-install] [--min-nodes N] [--json]

Credentials come from the environment (MPM_FEISHU_BASE_URL / MPM_FEISHU_APP_ID /
MPM_FEISHU_APP_SECRET), from one JSON object piped on stdin (feishu_url, app_id,
app_secret), or - only when stdin is a terminal - from prompts (the secret is
not echoed).  They are never command-line arguments.  Progress goes to stderr;
with --json the result summary goes to stdout.  The exit code is the flow's.
"""

from __future__ import annotations

import os
import sys

# Allow `python scripts/deploy/__main__.py` as well as -m.
if __package__ in (None, ""):  # pragma: no cover - direct file execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from deploy.__main__ import main as _main

    sys.exit(_main())

import argparse
import getpass
import json

from deploy import bootstrap, credentials, feishu_url, platforms


def hidden_prompt(field: str) -> str:
    """Ask for ``field`` on the terminal; the secret is not echoed.

    The prompt goes to stderr so a ``--json`` run keeps stdout to one JSON value.
    """
    prompt = f"{field}: "
    if field == "app_secret":
        return getpass.getpass(prompt)
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy", description="Bootstrap this machine from a Feishu Bitable")
    parser.add_argument("--platform", required=True,
                        choices=(platforms.MACOS, platforms.WINDOWS),
                        help="which host this machine is")
    parser.add_argument("--dry-run", action="store_true",
                        help="check access and plan only; write nothing")
    parser.add_argument("--no-start", action="store_true",
                        help="install without starting the service")
    parser.add_argument("--skip-supply", action="store_true",
                        help="skip downloading binaries into staging")
    parser.add_argument("--skip-install", action="store_true",
                        help="skip calling the platform installer")
    parser.add_argument("--min-nodes", type=int, default=1,
                        help="minimum pool nodes required (default: 1)")
    parser.add_argument("--json", action="store_true",
                        help="print the result summary as JSON on stdout")
    return parser


def main(argv=None, *, stdin=None, stdout=None, stderr=None,
         platform_factory=None, runner=None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    platform_factory = (platforms.get_platform
                        if platform_factory is None else platform_factory)
    runner = bootstrap.run_bootstrap if runner is None else runner

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.min_nodes < 0:
        parser.error("--min-nodes must be >= 0")

    # Step 1-3 - credentials: environment > stdin JSON > hidden prompts.
    interactive = bool(getattr(stdin, "isatty", lambda: False)())
    try:
        stdin_values = credentials.read_stdin_values(None if interactive else stdin)
        creds = credentials.resolve_credentials(
            os.environ, stdin_values, hidden_prompt if interactive else None)
    except credentials.CredentialsError as exc:
        print(f"error: {exc}", file=stderr, flush=True)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("error: interrupted", file=stderr, flush=True)
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=stderr, flush=True)
        return 2

    # Step 4 - the address must be a Feishu Bitable address.
    try:
        parsed = feishu_url.parse_bitable_url(creds.feishu_url)
    except feishu_url.FeishuUrlError as exc:
        print(f"error: {exc}", file=stderr, flush=True)
        return 2

    # Step 5 - the redactor scrubs every secret out of what gets printed.
    redactor = credentials.Redactor()
    redactor.add(creds.app_secret)
    redactor.add(creds.feishu_url)
    redactor.add(parsed.token)

    # Step 6 - the platform for this machine.
    try:
        platform = platform_factory(args.platform, platforms.find_repo_root(),
                                    os.environ)
    except ValueError:
        print("error: unknown platform", file=stderr, flush=True)
        return 2

    def log(line: str) -> None:
        print(redactor.text(line), file=stderr, flush=True)

    # Step 7-8 - run the flow; an unexpected crash names only the class.
    try:
        result = runner(creds, platform, environ=os.environ,
                        start=not args.no_start, dry_run=args.dry_run,
                        skip_supply=args.skip_supply,
                        skip_install=args.skip_install,
                        min_nodes=args.min_nodes, log=log)
    except Exception as exc:  # noqa: BLE001 - never echo the exception text
        log(f"bootstrap: unexpected {type(exc).__name__}")
        return 2

    # Step 9 - report.
    if args.json:
        print(redactor.text(json.dumps(result.summary(), ensure_ascii=False,
                                       indent=2)), file=stdout, flush=True)
    else:
        for problem in result.problems:
            print(f"error: {redactor.text(problem)}", file=stderr, flush=True)
        if result.access is not None:
            for hint in result.access.hints():
                print(redactor.text(hint), file=stderr, flush=True)
        if result.ok():
            print("bootstrap: OK", file=stderr, flush=True)
        else:
            print(f"bootstrap: FAILED exit={result.exit_code}",
                  file=stderr, flush=True)
    return int(result.exit_code)


if __name__ == "__main__":
    sys.exit(main())
