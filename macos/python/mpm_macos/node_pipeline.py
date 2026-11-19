"""Node pipeline daemon: the ``com.mihomo-proxy-management.node-pipeline`` job.

macOS port of upstream ``scripts/run-node-pipeline.py`` (commit 8926713), the
hourly scheduled task that turns measured nodes into the local machine's
assignment:

1. ``node_provider --collect``  - fetch the configured subscription sources
   into the Feishu 「节点测速总表」 (failures tolerated: a failed collect still
   lets the next steps refresh the existing table; fetching is explicitly not
   urgent on this host - when the sources work, the table fills by itself);
2. ``node_ops``                 - source-agnostic isolated-core probe (free
   ports near 17890, ``tun.enable: false``, never the production ports),
   region classification and the GPT consecutive-round gate, writing the
   measurement columns and the 「有效节点」 table;
3. ``node_provider --output``   - rebuild ``providers/node-valid.yaml`` (a
   mihomo file provider inside the ``-d`` home) from the valid table;
4. ``proxy-auto-switch.py``     - the unified GPT stability entry picks the
   stable node on the live controller (a non-selector GPT is rejected
   cleanly as ``engine_not_selector``; missing groups log and skip);
5. *apply*                       - when the valid provider appeared or
   disappeared since the last render, re-render the config under the
   lifecycle lock and reload the running core, flipping the GPT/US-FAST
   groups between subscription mode and valid-node assignment mode.

Everything runs from the **installed** tree (``libexec/scripts`` + the
installed mihomo binary); a missing installed binary or package fails closed
before any step writes anything.  Credentials follow the established rule:
paths only on command lines, values only in the 0600 secret file.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Callable

from mpm_common.sanitize import HOST

from . import context as context_mod
from .paths import Layout

PIPELINE_INTERVAL_ENV = "MPM_NODE_PIPELINE_INTERVAL_SECONDS"
PIPELINE_INTERVAL_DEFAULT = 3600  # upstream schedules the pipeline hourly
PIPELINE_INTERVAL_RANGE = (600, 86400)

LIVE_CONTROLLER = "http://127.0.0.1:9090"

#: Installed packages/scripts the pipeline subprocesses import/run.
NODE_PROVIDER_PACKAGE = "node_provider"
NODE_OPS_PACKAGE = "node_ops"
SWITCH_SCRIPT = "proxy-auto-switch.py"

#: Marker recording "the rendered config currently includes node-valid" so a
#: presence flip triggers exactly one re-render (see :func:`apply_presence`).
PRESENCE_MARKER = "valid-provider-active.flag"

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_PREREQUISITES_MISSING = 4


def pipeline_interval_seconds(environ: dict[str, str] | None = None) -> int:
    """``StartInterval`` for the plist render, clamped to a sane range.

    Mirrors upstream's hourly cadence.  An unparsable override falls back to
    the default: the interval only decides when the next tick fires.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(PIPELINE_INTERVAL_ENV) or "").strip()
    if not raw:
        return PIPELINE_INTERVAL_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return PIPELINE_INTERVAL_DEFAULT
    low, high = PIPELINE_INTERVAL_RANGE
    return max(low, min(high, value))


def _read_secret_values(path: str) -> dict[str, str]:
    # lazy: lifecycle imports this module, so the module level must not.
    from .lifecycle import parse_env

    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return parse_env(handle.read())
    except OSError:
        return {}


def prerequisites(layout: Layout) -> tuple[int, list[str]]:
    """ ``(exit_code, messages)`` for everything a tick needs.  Read-only."""
    messages: list[str] = []
    if not context_mod.resolve_runtime_binary(layout):
        messages.append(
            "installed mihomo binary missing - run `mihomo-proxy-management install` first"
        )
        return EXIT_PREREQUISITES_MISSING, messages
    packages = [os.path.join(layout.scripts_dir, name, "__init__.py")
                for name in (NODE_PROVIDER_PACKAGE, NODE_OPS_PACKAGE)]
    missing = [name for name, path in
               zip((NODE_PROVIDER_PACKAGE, NODE_OPS_PACKAGE), packages)
               if not os.path.isfile(path)]
    if missing:
        messages.append(
            "installed packages missing: " + ", ".join(missing)
            + " - run `mihomo-proxy-management install`"
        )
        return EXIT_PREREQUISITES_MISSING, messages
    if not os.path.isfile(os.path.join(layout.scripts_dir, SWITCH_SCRIPT)):
        messages.append(
            f"{SWITCH_SCRIPT} is not installed - run `mihomo-proxy-management install`"
        )
        return EXIT_PREREQUISITES_MISSING, messages
    values = _read_secret_values(layout.secret_file)
    if not ((values.get("FEISHU_APP_ID") or "").strip()
            and (values.get("FEISHU_APP_SECRET") or "").strip()):
        messages.append(
            "Feishu credentials are not configured: the pipeline reads/writes the "
            "node tables through Feishu - put FEISHU_APP_ID / FEISHU_APP_SECRET "
            "into state/subscription.env and re-run install (values never appear "
            "on a command line)"
        )
        return EXIT_PREREQUISITES_MISSING, messages
    return EXIT_OK, messages


def build_step_argv(layout: Layout, *, binary: str) -> list[list[str]]:
    """The four step argvs (paths only; no secret values on any command line)."""
    state = layout.node_state_dir
    secret = layout.secret_file
    return [
        [sys.executable, "-m", NODE_PROVIDER_PACKAGE,
         "--collect",
         "--sources-env", secret,
         "--env-file", secret,
         "--fetch-proxy", "http://127.0.0.1:7890",
         # Mirrors upstream run-node-pipeline: the source4 app channel is
         # attempted and degrades to a logged per-channel failure on hosts
         # without it (macOS: no DPAPI/ShuttleClient -> profile_missing).
         "--from-app", "source4",
         # source-1 supplement (e.g. the extracted Nextin/wmsxwd nodes): the
         # host's own nodes enter the shared table even when the slot-1
         # subscription fetch is blocked.
         "--supplement-yaml", os.path.join(layout.providers, "nextin.yaml"),
         "--lock-file", os.path.join(state, "node_provider.lock")],
        [sys.executable, "-m", NODE_OPS_PACKAGE,
         "--env-file", secret,
         "--core", binary,
         "--log-file", os.path.join(layout.logs, "node_ops.log"),
         "--lock-file", os.path.join(state, "node_ops.lock")],
        [sys.executable, "-m", NODE_PROVIDER_PACKAGE,
         "--output", layout.valid_provider_file,
         "--env-file", secret,
         "--lock-file", os.path.join(state, "node_provider.lock")],
        [sys.executable, os.path.join(layout.scripts_dir, SWITCH_SCRIPT),
         "--controller", LIVE_CONTROLLER,
         "--secret-env", secret,
         "--state-file", os.path.join(state, "proxy-failover-state.json"),
         "--log-file", os.path.join(layout.logs, "proxy-auto-switch.log"),
         "--alert-log", os.path.join(state, "proxy-failover-alerts.log"),
         "--alert-target", os.path.join(state, "proxy-failover-alert-target.txt"),
         "--rounds-jsonl", os.path.join(state, "proxy-failover-rounds.jsonl")],
    ]


STEP_LABELS = ("collect", "ops", "provide", "switch")


def run_steps(
    layout: Layout,
    *,
    runner: Callable[[list[str]], int] | None = None,
    say: Callable[[str], None] = print,
) -> int:
    """Run the four steps; every step runs even if a previous one failed.

    Mirrors upstream ``run-node-pipeline.py`` semantics: the exit code is 1
    when any step failed, and each step's stdout/stderr lands in the shared
    ``node-pipeline.log``.
    """
    binary = context_mod.resolve_runtime_binary(layout)
    os.makedirs(layout.node_state_dir, mode=0o700, exist_ok=True)
    os.makedirs(layout.logs, exist_ok=True)
    log_path = os.path.join(layout.logs, "node-pipeline.log")

    def _default_runner(argv: list[str]) -> int:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"START {argv[1] if argv[1] != '-m' else argv[2]}\n")
            log.flush()
            process = subprocess.run(
                argv,
                cwd=layout.scripts_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            log.write(f"EXIT {process.returncode}\n")
            return process.returncode

    execute = runner or _default_runner
    failed: list[str] = []
    for label, argv in zip(STEP_LABELS, build_step_argv(layout, binary=binary)):
        code = execute(argv)
        if code != 0:
            failed.append(label)
            say(f"node-pipeline: step {label} failed (exit {code})")
    return EXIT_STEP_FAILED if failed else EXIT_OK


def apply_presence(
    layout: Layout,
    *,
    say: Callable[[str], None] = print,
    context_factory: Callable[[], object] | None = None,
) -> bool:
    """Re-render the config when the valid provider's presence flipped.

    The renderer includes the ``node-valid`` file provider (and switches GPT
    to a selector over it / US-FAST to ``use: [node-valid]``) exactly when
    ``providers/node-valid.yaml`` exists at render time - mihomo rejects a
    file provider whose file is missing, so presence must gate the render.
    The flip is rare (once the pipeline first succeeds) and is applied under
    the non-blocking lifecycle lock; a busy lock skips the apply this tick.
    """
    marker = os.path.join(layout.node_state_dir, PRESENCE_MARKER)
    present = os.path.isfile(layout.valid_provider_file)
    active = os.path.isfile(marker)
    if present == active:
        return False

    from . import lifecycle  # lazy: lifecycle imports this module

    ctx = context_factory() if context_factory is not None else context_mod.build_context()
    try:
        guard = ctx.lifecycle_guard()
    except Exception as exc:  # noqa: BLE001 - busy/unavailable: retry next tick
        say("node-pipeline: lifecycle lock unavailable, apply deferred: "
            + HOST.text(f"{type(exc).__name__}"))
        return False
    try:
        with guard:
            report = lifecycle.configure(ctx, quiet=True)
            if report.status == "FAILED":
                say("node-pipeline: re-render failed; apply deferred")
                return False
    except Exception as exc:  # noqa: BLE001 - any failure defers to the next tick
        say("node-pipeline: re-render error, apply deferred: " + HOST.text(str(exc))[:200])
        return False

    os.makedirs(layout.node_state_dir, mode=0o700, exist_ok=True)
    if present:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("node-valid provider active in the rendered config\n")
    else:
        try:
            os.remove(marker)
        except OSError:
            pass
    say("node-pipeline: config re-rendered (node-valid provider "
        + ("active" if present else "removed") + "); reload the core to apply")
    _reload_core(layout, say=say)
    return True


def _reload_core(layout: Layout, *, say: Callable[[str], None] = print) -> None:
    """Best-effort ``PUT /configs`` on the live controller (hot reload)."""
    import urllib.error
    import urllib.request

    values = _read_secret_values(layout.secret_file)
    secret = (values.get("SECRET") or "").strip()
    request = urllib.request.Request(
        f"{LIVE_CONTROLLER}/configs?force=true",
        data=b'{"path": "", "payload": ""}',
        headers={"Authorization": f"Bearer {secret}"} if secret else {},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            say(f"node-pipeline: core reload http={response.status}")
    except (OSError, urllib.error.URLError) as exc:
        say("node-pipeline: core reload unavailable (picked up on next start): "
            + HOST.text(f"{type(exc).__name__}"))


def main(
    argv: list[str] | None = None,
    *,
    context_factory: Callable[[], object] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="mpm-node-pipeline")
    parser.add_argument("--root", help="alternate filesystem root (staging/tests only)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check prerequisites and print the step argvs; never run anything",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    ctx = context_factory() if context_factory is not None else context_mod.build_context(args.root)
    layout = ctx.layout

    code, messages = prerequisites(layout)
    for message in messages:
        print("node-pipeline: " + HOST.text(message))
    if args.dry_run or code != EXIT_OK:
        if args.dry_run and code == EXIT_OK:
            binary = context_mod.resolve_runtime_binary(layout)
            for label, argv in zip(STEP_LABELS, build_step_argv(layout, binary=binary)):
                print(f"node-pipeline argv[{label}]: " + HOST.text(" ".join(argv)))
        print(f"node-pipeline dry-run: exit={code}")
        return code

    steps_code = run_steps(layout)
    apply_presence(layout, context_factory=lambda: ctx)
    return steps_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
