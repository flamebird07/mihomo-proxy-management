"""Node pipeline daemon: the ``com.mihomo-proxy-management.node-pipeline`` job.

macOS port of upstream ``scripts/run-node-pipeline.py`` (the Windows entry;
keep both step lists in sync), the hourly scheduled task that turns measured
nodes into the local machine's assignment.  Cross-platform contract:
``docs/CROSS-PLATFORM.md``:


1. ``node_provider --collect``  - fetch the configured subscription sources
   into the Feishu 「节点测速总表」 (failures tolerated: a failed collect still
   lets the next steps refresh the existing table; fetching is explicitly not
   urgent on this host - when the sources work, the table fills by itself).
   Skipped on a Feishu-pool machine with no collect inputs (no subscription
   slot, no supplement file): a zero-source collect can only exit non-zero;
1b. ``feishu_pool.py``         - pool machines only: rebuild
   ``providers/feishu-pool.yaml`` from the master table (the pool must be
   regenerated *after* collect so new nodes reach the running core the same
   tick), then
2. ``node_ops``                 - source-agnostic isolated-core probe (free
   ports near 17890, ``tun.enable: false``, never the production ports),
   region classification and the GPT consecutive-round gate, writing the
   measurement columns and the 「有效节点」 table;
3. ``node_provider --output``   - rebuild ``providers/node-valid.yaml`` (a
   mihomo file provider inside the ``-d`` home) from the valid table;
4. *apply*                      - re-render on a valid-provider presence
   change and hot-reload the provider when its content changes;
5. ``switch_gpt``               - verify GPT candidates in an isolated core
   and update the live selector in US, SG, JP order.

Everything runs from the **installed** tree (``libexec/scripts`` + the
installed mihomo binary); a missing installed binary or package fails closed
before any step writes anything.  Credentials follow the established rule:
paths only on command lines, values only in the 0600 secret file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Callable

from mpm_common.sanitize import HOST

from . import config, context as context_mod
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
    if pool_mode(layout) and not os.path.isfile(layout.pool_script):
        messages.append(
            f"feishu_pool.py is not installed ({layout.pool_script}) - run "
            "`mihomo-proxy-management install` (the Feishu-pool config needs it "
            "to rebuild providers/feishu-pool.yaml every tick)"
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


def supplement_files(layout: Layout) -> list[str]:
    """Existing supplement files: the landing directory + the extracted
    ``nextin.yaml`` (legacy path).  Sorted for a stable argv; missing files
    are skipped (node_provider would only warn).

    These are inputs owned by the node pipeline: the host sync
    (``feishu-sync``) never reads them; it obtains the same nodes from the
    「节点测速总表」 this pipeline publishes.
    """
    import glob

    files = sorted(glob.glob(os.path.join(layout.supplements_dir, "*.yaml")))
    legacy = os.path.join(layout.providers, "nextin.yaml")
    if os.path.isfile(legacy):
        files.append(legacy)
    return files


SUBSCRIPTION_SLOT_KEYS = ("SUBSCRIPTION_URL", "SUBSCRIPTION_URL_2",
                          "SUBSCRIPTION_URL_3", "SUBSCRIPTION_URL_4")


def collect_inputs(layout: Layout) -> list[str]:
    """Everything the collect step would read: the subscription slots (main +
    auxiliary) configured in the secret file plus the supplement files.

    A pure Feishu-pool machine has no subscription URL at all and ``source4``
    is Windows-only (DPAPI), so an empty list means a collect run would find
    zero sources and exit non-zero on zero nodes - the tick skips collect
    instead (see :func:`build_step_argv`).
    """
    values = _read_secret_values(layout.secret_file)
    slots = [key for key in SUBSCRIPTION_SLOT_KEYS
             if (values.get(key) or "").strip()]
    return slots + supplement_files(layout)


def _node_ops_alive() -> bool:
    """Whether any node_ops process is running (pgrep; no /proc on macOS)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", NODE_OPS_PACKAGE],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # cannot prove it is dead: never touch the lock
    return bool((result.stdout or "").strip())


def _default_interface() -> str:
    """The physical default-route interface (route -n get default), so the
    isolated probe core bypasses the host's own TUN via
    NODE_OPS_INTERFACE_NAME (see node_ops/probe.py)."""
    try:
        result = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=10,
        )
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-2] == "interface:":
                return parts[-1]
    except Exception:
        pass
    return ""


def clear_stale_ops_lock(layout: Layout, *, say: Callable[[str], None] = print) -> bool:
    """Remove an orphaned node_ops lockfile (no process, lock older than 60s).

    ``launchctl kickstart -k`` kills the pipeline parent but not the running
    node_ops child; a child killed mid-probe leaves its O_EXCL lockfile
    behind, and every later tick fails with "another node_ops instance is
    running" until the 24h staleness window.  The daemon proves staleness
    (no matching process + age) before unlinking; an unprovable state keeps
    the lock (fail-closed).
    """
    lock_path = os.path.join(layout.node_state_dir, "node_ops.lock")
    if not os.path.isfile(lock_path):
        return False
    try:
        age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        return False
    if age < 60 or _node_ops_alive():
        return False
    try:
        os.unlink(lock_path)
    except OSError:
        return False
    say("node-pipeline: stale node_ops lock removed "
        f"(no process, age {int(age)}s)")
    return True


#: Pool mode: the step inserted between collect and ops, and the marker that
#: records "the running core has loaded this pool file's content" (see
#: :func:`maybe_refresh_pool`).
POOL_STEP_LABEL = "pool"
POOL_MARKER = "pool.sha256"


def pool_mode(layout: Layout) -> bool:
    """Whether the rendered config reads the Feishu pool file (no subscription
    URL).  An unreadable config means "not in pool mode": nothing to refresh.

    Only the managed pool provider counts: the ``subscription`` provider must
    be a ``type: file`` provider pointing at ``config.POOL_PROVIDER_PATH`` -
    a hand-written custom file provider is not refreshed here.
    """
    try:
        import yaml  # lazy: the daemon's interpreter carries PyYAML

        with open(layout.config, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except Exception:  # noqa: BLE001 - missing file / bad YAML / no yaml module
        return False
    providers = document.get("proxy-providers") if isinstance(document, dict) else None
    provider = providers.get(config.PROVIDER_NAME) if isinstance(providers, dict) else None
    return (isinstance(provider, dict)
            and provider.get("type") == "file"
            and provider.get("path") == config.POOL_PROVIDER_PATH)


def _provider_hash(path: str) -> str | None:
    import hashlib

    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _loaded_provider_hash(path: str) -> str | None:
    """The hash recorded in the marker file at ``path`` (``None`` if absent)."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def maybe_refresh_provider(layout: Layout, *,
                           say: Callable[[str], None] = print,
                           reload_provider: Callable[[str], int | None] | None = None,
                           name: str = "node-valid",
                           path: str | None = None,
                           marker_name: str = "provider.sha256",
                           ) -> bool:
    """Hot-reload a ``type: file`` provider when its content changed.

    mihomo never re-reads a ``type: file`` provider on its own; without this
    the running core would keep the pool from the first tick forever.  The
    hash is only persisted after a successful reload, so a failed reload is
    retried on the next tick.  ``node-valid`` (the measured valid-node pool)
    is the default target; :func:`maybe_refresh_pool` passes the Feishu pool
    file for hosts without a subscription URL.
    """
    provider_file = path or layout.valid_provider_file
    if not os.path.isfile(provider_file):
        return False
    marker = os.path.join(layout.node_state_dir, marker_name)
    current = _provider_hash(provider_file)
    if current is None:
        return False
    if current == _loaded_provider_hash(marker):
        return False

    if reload_provider is None:
        def reload_provider(_secret: str) -> int | None:
            import urllib.error
            import urllib.request

            values = _read_secret_values(layout.secret_file)
            secret = (values.get("SECRET") or "").strip()
            request = urllib.request.Request(
                f"{LIVE_CONTROLLER}/providers/proxies/{name}",
                data=b"",
                headers={"Authorization": f"Bearer {secret}"} if secret else {},
                method="PUT",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status
            except (OSError, urllib.error.URLError):
                return None

    secret_present = bool((_read_secret_values(layout.secret_file) or {}).get("SECRET"))
    status = reload_provider(secret_present)
    if status not in (200, 204):
        say(f"node-pipeline: {name} provider reload unavailable "
            "(retried next tick): " + HOST.text(f"{status}"))
        return False
    os.makedirs(layout.node_state_dir, mode=0o700, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(current + "\n")
    say(f"node-pipeline: {name} provider reloaded "
        f"http={status} (pool refreshed without dropping connections)")
    return True


def maybe_refresh_pool(layout: Layout, *, say: Callable[[str], None] = print,
                       reload_provider: Callable[[str], int | None] | None = None) -> bool:
    """Hot-reload the Feishu pool provider on hosts without a subscription URL."""
    if not pool_mode(layout):
        return False
    return maybe_refresh_provider(layout, say=say, reload_provider=reload_provider,
                                  name=config.PROVIDER_NAME, path=layout.pool_file,
                                  marker_name=POOL_MARKER)


def build_step_argv(layout: Layout, *, binary: str,
                    feishu_pool: bool | None = None) -> list:
    """The step argvs (paths only; no secret values on any command line).

    Pool mode (``feishu_pool``; auto-detected from the rendered config when
    ``None``) inserts the Feishu pool rebuild between collect and ops - the
    pool must be regenerated *after* collect so nodes merged into the master
    table this tick reach the running core the same tick.  A pool machine
    with no collect inputs gets ``None`` in the collect slot: the tick records
    and skips it instead of running a collect that can only exit non-zero on
    zero nodes.  Subscription mode returns exactly the historical three argvs.
    """
    state = layout.node_state_dir
    secret = layout.secret_file
    collect: list[str] | None = [
        sys.executable, "-m", NODE_PROVIDER_PACKAGE,
        "--collect",
        "--sources-env", secret,
        "--env-file", secret,
        "--fetch-proxy", "http://127.0.0.1:7890",
        # Mirrors upstream run-node-pipeline: the source4 app channel is
        # attempted and degrades to a logged per-channel failure on hosts
        # without it (macOS: no DPAPI/ShuttleClient -> profile_missing).
        "--from-app", "source4",
        # Source-1 supplements: the host's own nodes enter the shared table
        # even when the slot-1 subscription fetch is blocked.  Every file in
        # providers/supplements/ (the landing point for locally obtained
        # nodes: browser downloads, app exports) plus the extracted
        # nextin.yaml is merged each tick; fetch-side dedupe keeps fetched
        # nodes first and per-source table replace keeps them fresh.
        *sum((["--supplement-yaml", path] for path in supplement_files(layout)), []),
        "--lock-file", os.path.join(state, "node_provider.lock"),
    ]
    if feishu_pool is None:
        feishu_pool = pool_mode(layout)
    steps: list = []
    if feishu_pool:
        if collect_inputs(layout):
            steps.append(collect)
        else:
            steps.append(None)
        steps.append([sys.executable, layout.pool_script,
                      "--env-file", secret,
                      "--output", layout.pool_file])
    else:
        steps.append(collect)
    steps.append([sys.executable, "-m", NODE_OPS_PACKAGE,
                  "--env-file", secret,
                  "--core", binary,
                  # Probe budgets sized for a ~130-row master table (the upstream
                  # 600s defaults were tuned for ~100 nodes: with 129 entries the
                  # delay budget ran out mid-round, most nodes never got GPT-target
                  # measurements, and the gate wiped 有效节点 to zero - the publish
                  # gate kept the old provider, but the pool stopped growing).
                  "--total-budget", "1800",
                  "--ipinfo-budget", "1200",
                  "--log-file", os.path.join(layout.logs, "node_ops.log"),
                  "--lock-file", os.path.join(state, "node_ops.lock")])
    steps.append([sys.executable, "-m", NODE_PROVIDER_PACKAGE,
                  "--output", layout.valid_provider_file,
                  "--env-file", secret,
                  "--lock-file", os.path.join(state, "node_provider.lock")])
    return steps


STEP_LABELS = ("collect", "ops", "provide")


def step_labels(feishu_pool: bool) -> tuple:
    """Labels matching :func:`build_step_argv` for the given mode.

    ``STEP_LABELS`` itself stays the subscription-mode constant: existing
    callers pair it with the three subscription argvs.
    """
    return ("collect", POOL_STEP_LABEL, "ops", "provide") if feishu_pool else STEP_LABELS


def run_steps(
    layout: Layout,
    *,
    runner: Callable[[list[str]], int] | None = None,
    say: Callable[[str], None] = print,
    feishu_pool: bool | None = None,
) -> int:
    """Build the provider; every step runs even if a previous one failed.

    Mirrors upstream ``run-node-pipeline.py`` semantics: the exit code is 1
    when any step failed, and each step's stdout/stderr lands in the shared
    ``node-pipeline.log``.  A ``None`` argv (the skipped no-input collect of a
    pure pool machine) is recorded and not executed.
    """
    binary = context_mod.resolve_runtime_binary(layout)
    os.makedirs(layout.node_state_dir, mode=0o700, exist_ok=True)
    os.makedirs(layout.logs, exist_ok=True)
    log_path = os.path.join(layout.logs, "node-pipeline.log")

    def _default_runner(argv: list[str]) -> int:
        env = os.environ | {"NODE_OPS_INTERFACE_NAME": _default_interface()}
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"START {argv[1] if argv[1] != '-m' else argv[2]}\n")
            log.flush()
            process = subprocess.run(
                argv,
                cwd=layout.scripts_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
            )
            log.write(f"EXIT {process.returncode}\n")
            return process.returncode

    if feishu_pool is None:
        feishu_pool = pool_mode(layout)
    execute = runner or _default_runner
    failed: list[str] = []
    for label, argv in zip(step_labels(feishu_pool),
                           build_step_argv(layout, binary=binary,
                                           feishu_pool=feishu_pool)):
        if argv is None:
            say(f"node-pipeline: step {label} skipped (no collect inputs; "
                "ops/provide still refresh from the shared table)")
            continue
        code = execute(argv)
        if code != 0:
            failed.append(label)
            say(f"node-pipeline: step {label} failed (exit {code})")
    return EXIT_STEP_FAILED if failed else EXIT_OK


def _controller_api(layout: Layout) -> tuple[str, str]:
    values = _read_secret_values(layout.secret_file)
    return LIVE_CONTROLLER, (values.get("SECRET") or "").strip()


def _api_json(path: str, secret: str, method: str = "GET", body: dict | None = None):
    import urllib.request

    request = urllib.request.Request(
        f"{LIVE_CONTROLLER}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {secret}"} if secret else {})},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read().decode()
    # PUT /proxies/<group> answers 204 No Content; json.loads("") would turn a
    # successful switch into a bogus JSONDecodeError in the pipeline log.
    return json.loads(raw) if raw.strip() else {}


_AD_NODE_NAME = re.compile(r"★|官网|招募|防丢失|网不通|更新订阅|请咨询|客服")
_GPT_UNSUPPORTED_NAME = re.compile(
    r"香港|🇭🇰|\bHK\b|俄罗斯|🇷🇺|\bRU\b|伊朗|🇮🇷|\bIR\b|朝鲜|🇰🇵", re.IGNORECASE)
_GPT_REGIONS = ("US", "SG", "JP")
_GPT_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_GPT_BLOCK_MARKERS = (b"_cf_chl_opt",
                      b"<title>just a moment", b"<title>attention required",
                      b"unsupported_country_region_territory")


def _gpt_http_body(port: int, url: str) -> bytes:
    result = subprocess.run(
        ["/usr/bin/curl", "--disable", "--silent", "--noproxy", "",
         "--proxy", f"http://127.0.0.1:{port}", "--user-agent", _GPT_BROWSER_UA,
         "--max-time", "6", "--max-filesize", "2097152",
         "--compressed", "--include", "--suppress-connect-headers", url],
        capture_output=True, timeout=8,
    )
    if result.returncode != 0:
        return b""
    headers, _, body = result.stdout.partition(b"\r\n\r\n")
    status = headers.split(b"\r\n", 1)[0].split()
    if status[1:2] != [b"200"] or b"\r\ncf-mitigated:" in headers.lower():
        return b""
    return body


@contextmanager
def _gpt_probe_session(layout: Layout, names: list[str]):
    # Launchd puts the macOS package on PYTHONPATH. Load the same installed
    # node_ops package that the preceding measurement step runs.
    sys.path.insert(0, layout.scripts_dir)
    try:
        from node_ops.probe import IsolatedProber, select_and_verify
    finally:
        sys.path.pop(0)

    with open(layout.valid_provider_file, encoding="utf-8") as handle:
        provider = config._yaml().safe_load(handle)
    candidates = set(names)
    nodes = {node["name"]: node for node in provider["proxies"]
             if isinstance(node, dict) and node.get("name") in candidates}
    if not nodes:
        yield lambda _name: None
        return

    core = context_mod.resolve_runtime_binary(layout)
    if not core:
        raise RuntimeError("installed mihomo binary missing")
    with IsolatedProber(core).session(list(nodes.values()), list(nodes),
                                     interface=_default_interface()) as (api, port):
        def verify(name: str) -> str | None:
            if name not in nodes or not select_and_verify(api, "TEST", name):
                return None
            try:
                trace = _gpt_http_body(port, config.GPT_URL)
                location = re.search(rb"(?m)^loc=([A-Z]{2})\r?$", trace)
                region = location[1].decode() if location else ""
                if region not in _GPT_REGIONS:
                    return None
                homepage = _gpt_http_body(port, "https://chatgpt.com/")
            except subprocess.TimeoutExpired:
                return None
            if not homepage or any(marker in homepage.lower() for marker in _GPT_BLOCK_MARKERS):
                return None
            return region

        yield verify


def switch_gpt(layout: Layout, *, say: Callable[[str], None] = print,
               api_json: Callable[..., dict] | None = None) -> str | None:
    """Select a verified GPT exit by region first and trace delay second."""
    import urllib.parse

    _api, secret = _controller_api(layout)
    call = api_json or (lambda path, method="GET", body=None:
                        _api_json(path, secret, method, body))

    gpt = call("/proxies/GPT")
    if (gpt.get("type") or "").lower() != "selector":
        say("node-pipeline: GPT is not a selector (engine="
            + str(gpt.get("type")) + "); switch skipped")
        return None
    current = gpt.get("now")
    provider_hash = _provider_hash(layout.valid_provider_file)
    if provider_hash != _loaded_provider_hash(
            os.path.join(layout.node_state_dir, "provider.sha256")):
        say("node-pipeline: node-valid reload pending; GPT switch deferred")
        return None
    try:
        delays = call("/group/GPT/delay?timeout=3000&expected=200&url="
                      + urllib.parse.quote(config.GPT_URL, safe=""))
    except Exception as exc:  # noqa: BLE001 - transient controller slowness
        say("node-pipeline: group delay unavailable, keeping " + str(current)
            + ": " + type(exc).__name__)
        return None
    if not isinstance(delays, dict):
        say("node-pipeline: group delay returned no map; keeping " + str(current))
        return None
    members = set(gpt.get("all") or ()) - {"AUTO-FOREIGN", "DIRECT", "COMPATIBLE"}
    names = sorted(
        (name for name, delay in delays.items()
         if name in members and type(delay) is int and delay > 0
         and not _AD_NODE_NAME.search(name) and not _GPT_UNSUPPORTED_NAME.search(name)),
        key=lambda name: (delays[name], name != current, name),
    )
    if not names:
        say("node-pipeline: no alive GPT pool member; keeping " + str(current))
        return None

    verified: dict[str, str] = {}
    with _gpt_probe_session(layout, names) as verify:
        for name in names:
            region = verify(name)
            if region in _GPT_REGIONS:
                verified.setdefault(region, name)
                if region == "US":
                    break
    best_region = next((region for region in _GPT_REGIONS if region in verified), None)
    if best_region is None:
        say("node-pipeline: no verified US/SG/JP GPT exit; keeping " + str(current))
        return None
    best_name = verified[best_region]
    details = f"{best_name} ({best_region}, {delays[best_name]}ms)"
    live = call("/proxies/GPT")
    if ((live.get("type") or "").lower() != "selector" or live.get("now") != current
            or best_name not in (live.get("all") or ())
            or provider_hash != _provider_hash(layout.valid_provider_file)):
        say("node-pipeline: GPT changed during verification; switch skipped")
        return None
    if best_name == current:
        say(f"node-pipeline: GPT keeping {details}")
        return best_name
    call("/proxies/GPT", method="PUT", body={"name": best_name})
    if call("/proxies/GPT").get("now") != best_name:
        raise RuntimeError("GPT selection could not be verified")
    say(f"node-pipeline: GPT -> {details} (was {current})")
    return best_name


def ensure_gpt_exit(
    layout: Layout,
    *,
    say: Callable[[str], None] = print,
    refresh: Callable[..., bool] | None = None,
    switch: Callable[..., str | None] | None = None,
) -> str | None:
    """Select a verified GPT exit once the core is in charge (boot step).

    The scheduled pipeline already ends with :func:`switch_gpt`, but its job
    only has ``StartInterval``: the first tick lands one interval after load,
    so right after a boot GPT stayed on its default member (``AUTO-FOREIGN``,
    the fastest node - often Hong Kong, which OpenAI blocks).  The boot entry
    calls this so "switched to mihomo" and "GPT has a working exit" are the
    same event.

    The provider reload comes first on purpose: a tick that rebuilt
    ``node-valid.yaml`` while the core was stopped could not write the
    ``provider.sha256`` marker, so :func:`switch_gpt` would defer on that
    mismatch until the next hourly tick.  The core reads the provider from
    disk on start, so one reload reconciles the marker.
    """
    if not os.path.isfile(layout.valid_provider_file):
        say("node-pipeline: no node-valid provider; GPT uses its regional fallback")
        return None
    (refresh or maybe_refresh_provider)(layout, say=say)
    return (switch or switch_gpt)(layout, say=say)


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
            if not _reload_core(layout, say=say):
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
    say("node-pipeline: config reloaded (node-valid provider "
        + ("active" if present else "removed") + ")")
    return True


def _reload_core(layout: Layout, *, say: Callable[[str], None] = print) -> bool:
    """Return whether the live controller accepted the configuration reload."""
    import urllib.error
    import urllib.request

    values = _read_secret_values(layout.secret_file)
    secret = (values.get("SECRET") or "").strip()
    request = urllib.request.Request(
        f"{LIVE_CONTROLLER}/configs?force=true",
        data=b'{"path": "", "payload": ""}',
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {secret}"} if secret else {})},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            say(f"node-pipeline: core reload http={response.status}")
            return response.status in (200, 204)
    except (OSError, urllib.error.URLError) as exc:
        say("node-pipeline: core reload unavailable (retried next tick): "
            + type(exc).__name__)
        return False


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
            mode = pool_mode(layout)
            for label, argv in zip(step_labels(mode),
                                   build_step_argv(layout, binary=binary,
                                                   feishu_pool=mode)):
                if argv is None:
                    print(f"node-pipeline argv[{label}]: skipped "
                          "(no collect inputs; ops/provide still refresh)")
                else:
                    print(f"node-pipeline argv[{label}]: " + HOST.text(" ".join(argv)))
        print(f"node-pipeline dry-run: exit={code}")
        return code

    clear_stale_ops_lock(layout)
    steps_code = run_steps(layout)
    # Pool machines: push the freshly rebuilt pool file into the running core
    # before the presence flip (a re-render would restart from disk anyway).
    maybe_refresh_pool(layout)
    apply_presence(layout, context_factory=lambda: ctx)
    maybe_refresh_provider(layout)
    try:
        switch_gpt(layout)
    except Exception as exc:  # noqa: BLE001 - report switch failure without rolling back the provider
        print("node-pipeline: GPT switch failed: "
              + HOST.text(f"{type(exc).__name__}: {exc}")[:200])
        return EXIT_STEP_FAILED
    return steps_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
