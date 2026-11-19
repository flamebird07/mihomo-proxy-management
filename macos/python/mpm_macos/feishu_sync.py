"""Feishu speedtest sync daemon: the ``com.mihomo-proxy-management.feishu-sync`` job.

macOS counterpart of the Windows scheduled tasks.  Windows runs
``scripts/sync-feishu-bitable.py`` every 30 minutes (tables 1/2/3) and a
10-minute ``--ninja-only`` pass (table 2).  On macOS there is no Ninja
Desktop and no task scheduler: this LaunchDaemon fires
``StartInterval`` seconds (default 600) and runs the *same* script in-process
with macOS paths, using the full sync mode for all three Feishu tables.

Rules this module keeps:

* the installed script (``Layout.sync_script``, copied by ``install`` next to
  the already-installed ``convert_sub.py`` so its ``sys.path`` import works)
  is executed unmodified - every Windows default is overridden on the command
  line, and the command line carries **paths only**: the secret *file* is
  named there, the values never are;
* the temporary speedtest core is spawned by the script itself (mixed port
  17890, controller 19090, ``tun.enable: false``) - it never touches the
  production core's ports, TUN or groups;
* credentials missing is a *reported* condition, not a crash: the run prints
  where the three ``FEISHU_*`` keys belong (copy them from the Windows host's
  ``~/.hermes/.env`` into the 0600 ``state/subscription.env`` and re-run
  ``install``) and exits non-zero so launchd records the failure.  There is no
  ``KeepAlive``, so a failing run is simply retried on the next interval tick;
* a missing installed binary or sync script fails closed before anything is
  written - the sync must never run with a substitute binary.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from typing import Any, Callable

from mpm_common.errors import FailClosed
from mpm_common.sanitize import HOST

from . import context as context_mod
from .paths import Layout

SYNC_INTERVAL_ENV = "MPM_FEISHU_SYNC_INTERVAL_SECONDS"
SYNC_INTERVAL_DEFAULT = 600
SYNC_INTERVAL_RANGE = (300, 86400)

#: Endpoints the sync talks to.  Both are loopback services of the production
#: core: subscription fetches / page probes go through the mixed port, the
#: read-only live-controller fallback (and the SECRET bearer token) through
#: the controller port.
LIVE_CONTROLLER = "http://127.0.0.1:9090"
FETCH_PROXY = "http://127.0.0.1:7890"

SYNC_SCRIPT_NAME = "sync-feishu-bitable.py"

#: The three credential keys, all optional until the operator copies them
#: from the Windows host.  Missing keys are reported, never invented.
FEISHU_APP_ID_KEY = "FEISHU_APP_ID"
FEISHU_APP_SECRET_KEY = "FEISHU_APP_SECRET"
FEISHU_APP_TOKEN_KEY = "FEISHU_BITABLE_APP_TOKEN"
CREDENTIAL_KEYS = (FEISHU_APP_ID_KEY, FEISHU_APP_SECRET_KEY, FEISHU_APP_TOKEN_KEY)

CREDENTIALS_HINT = (
    "Feishu credentials are not configured: put FEISHU_APP_ID / FEISHU_APP_SECRET / "
    "FEISHU_BITABLE_APP_TOKEN into state/subscription.env (copy them from the "
    "Windows host's ~/.hermes/.env) and re-run `mihomo-proxy-management install` "
    "- credential values never appear on a command line"
)

#: Exit codes.  3 (credentials) and 4 (prerequisites) are distinct so launchd's
#: last-exit status and the log line say exactly why a tick did nothing.
EXIT_OK = 0
EXIT_SYNC_FAILED = 1
EXIT_CREDENTIALS_MISSING = 3
EXIT_PREREQUISITES_MISSING = 4


def sync_interval_seconds(environ: dict[str, str] | None = None) -> int:
    """``StartInterval`` for the plist render, clamped to a sane range.

    The default mirrors the Windows 10-minute Ninja task.  An unparsable
    override falls back to the default instead of failing the install - the
    interval only decides *when* the next tick fires, never *whether* data is
    written.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(SYNC_INTERVAL_ENV) or "").strip()
    if not raw:
        return SYNC_INTERVAL_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return SYNC_INTERVAL_DEFAULT
    low, high = SYNC_INTERVAL_RANGE
    return max(low, min(high, value))


def build_sync_argv(layout: Layout, *, binary: str) -> list[str]:
    """The full argv for ``sync-feishu-bitable.py`` (paths only, no secrets)."""
    state = layout.feishu_state_dir
    return [
        "--env-file", layout.secret_file,
        "--sources-env", layout.secret_file,
        "--live-env", layout.secret_file,
        "--live-controller", LIVE_CONTROLLER,
        "--fetch-proxy", FETCH_PROXY,
        "--core", binary,
        "--config", layout.config,
        "--log-file", os.path.join(layout.logs, "feishu-bitable-sync.log"),
        "--state-file", os.path.join(state, "feishu-bitable-tables.json"),
        "--sync-state-file", os.path.join(state, "feishu-bitable-sync-state.json"),
        "--gpt-history-file", os.path.join(state, "feishu-gpt-history.json"),
        "--lock-file", os.path.join(state, "feishu-bitable-sync.lock"),
        "--source-cache-dir", state,
        "--source1-supplement", os.path.join(layout.providers, "nextin.yaml"),
        "--source1-cache", os.path.join(state, "source1-cache.yaml"),
        "--source1-cache", layout.provider_file,
        "--source2-cache", os.path.join(state, "source2-cache.yaml"),
        "--source3-cache", os.path.join(state, "source3-cache.yaml"),
        "--source4-cache", os.path.join(state, "source4-cache.yaml"),
    ]


def missing_credentials(env: dict[str, str]) -> list[str]:
    """Credential keys absent from the 0600 secret file (names only)."""
    return [key for key in CREDENTIAL_KEYS if not (env.get(key) or "").strip()]


def load_sync_module(path: str):
    """Import the installed ``sync-feishu-bitable.py`` by path.

    The script inserts its own directory into ``sys.path`` and imports
    ``convert_sub`` from there, so the installed layout (both files side by
    side under ``libexec/scripts``) is what makes this work.  The module has
    an ``if __name__ == "__main__"`` guard, so importing it has no side
    effects.
    """
    spec = importlib.util.spec_from_file_location("mpm_feishu_sync_script", path)
    if spec is None or spec.loader is None:
        raise FailClosed(f"cannot load sync script: {SYNC_SCRIPT_NAME}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise FailClosed(
            f"sync script failed to load: {HOST.text(str(type(exc).__name__))}"
        ) from exc
    return module


def _read_secret_values(path: str) -> dict[str, str]:
    # lazy: lifecycle imports this module, so the module level must not.
    from .lifecycle import parse_env

    return parse_env(_read_secret_text(path))


def prerequisites(layout: Layout, *, environ: dict[str, str] | None = None) -> tuple[int, list[str]]:
    """ ``(exit_code, messages)`` for everything that must exist before a run.

    Read-only: no directory is created, no file written.  A missing installed
    mihomo binary or sync script is fail closed - the sync must never run
    with a substitute core.
    """
    messages: list[str] = []
    if not context_mod.resolve_runtime_binary(layout):
        messages.append(
            "installed mihomo binary missing - run `mihomo-proxy-management install` first"
        )
        return EXIT_PREREQUISITES_MISSING, messages
    if not os.path.isfile(layout.sync_script):
        messages.append(
            f"{SYNC_SCRIPT_NAME} is not installed - run `mihomo-proxy-management install`"
        )
        return EXIT_PREREQUISITES_MISSING, messages
    values = _read_secret_values(layout.secret_file)
    missing = missing_credentials(values)
    if missing:
        messages.append(CREDENTIALS_HINT)
        messages.append("missing keys: " + ", ".join(missing))
        return EXIT_CREDENTIALS_MISSING, messages
    return EXIT_OK, messages


def _read_secret_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.read()
    except OSError:
        return ""


def run_sync(
    layout: Layout,
    *,
    environ: dict[str, str] | None = None,
    say: Callable[[str], None] = print,
    load_module: Callable[[str], Any] | None = None,
) -> int:
    """One launchd tick: prerequisites, then the sync script's ``main``."""
    code, messages = prerequisites(layout, environ=environ)
    for message in messages:
        say("feishu-sync: " + HOST.text(message))
    if code != EXIT_OK:
        return code
    binary = context_mod.resolve_runtime_binary(layout)
    os.makedirs(layout.feishu_state_dir, mode=0o700, exist_ok=True)
    argv = build_sync_argv(layout, binary=binary)
    loader = load_module or load_sync_module
    try:
        module = loader(layout.sync_script)
        return int(module.main(argv))
    except SystemExit as exc:  # defensive: argparse on our argv never exits
        return EXIT_OK if exc.code in (None, 0) else int(exc.code)
    except FailClosed as exc:
        say("feishu-sync: " + HOST.text(str(exc)))
        return EXIT_SYNC_FAILED
    except Exception as exc:  # noqa: BLE001 - one tick failing must not crash launchd
        say("feishu-sync run failed: " + HOST.text(f"{type(exc).__name__}: {exc}")[:300])
        return EXIT_SYNC_FAILED


def main(
    argv: list[str] | None = None,
    *,
    context_factory: Callable[[], Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="mpm-feishu-sync")
    parser.add_argument("--root", help="alternate filesystem root (staging/tests only)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check prerequisites and print the argv; never run the sync",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    ctx = context_factory() if context_factory is not None else context_mod.build_context(args.root)
    if args.dry_run:
        code, messages = prerequisites(ctx.layout, environ=ctx.environ)
        for message in messages:
            print("feishu-sync: " + HOST.text(message))
        binary = context_mod.resolve_runtime_binary(ctx.layout)
        if binary:
            for item in build_sync_argv(ctx.layout, binary=binary):
                print("feishu-sync argv: " + HOST.text(item))
        print(f"feishu-sync dry-run: exit={code}")
        return code
    return run_sync(ctx.layout, environ=ctx.environ)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
