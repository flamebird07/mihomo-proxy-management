"""macOS lifecycle engine - the one implementation behind every subcommand.

Invariants:

* every operation is idempotent: a second run reports ``UNCHANGED`` or
  re-verifies, and never starts a second instance;
* a config only replaces the live one after the static audit *and*
  ``mihomo -t`` passed;
* TUN is only claimed when the process, the controller and an injected
  interface/route probe agree;
* a TUN failure is a transaction: snapshot the system proxy, render mixed,
  verify the core is healthy, and only then point the system proxy at it;
* ``stop`` always restores the proxy snapshot and only deletes it after a
  successful read-back;
* every printed line is sanitised.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from mpm_common import policy
from mpm_common.atomicio import (
    check_output_mode,
    check_secret_input,
    ensure_dir,
    is_noop,
    read_text_strict,
    remove_if_present,
    secure_tempdir,
    write_atomic,
)
from mpm_common.controller_health import (
    ControllerClient,
    ControllerError,
    HEALTH_DEGRADED,
    HEALTH_FAILED,
    HEALTH_INACTIVE,
    HEALTH_OK,
    HealthReport,
    evaluate,
    readiness,
)
from mpm_common.errors import ExitCode, FailClosed, MpmError, NotReady
from mpm_common.executor import Executor
from mpm_common.platforms import PlatformFacts
from mpm_common.sanitize import HOST, sanitize

from . import config as config_mod
from . import plist as plist_mod
from . import preflight as preflight_mod
from .launchdctl import Launchd
from .locking import FileLock, is_held, read_metadata
from .network_proxy import (
    NetworkProxy,
    NetworkProxyError,
    STAGE_APPLIED,
    load_snapshot,
    save_snapshot,
    snapshot_exists,
)
from .paths import LABEL, Layout, managed_dirs

SUBCOMMANDS = (
    "preflight",
    "install",
    "configure",
    "start",
    "stop",
    "restart",
    "status",
    "health",
    "uninstall",
)

STATE_SCHEMA = 1


@dataclass
class Report:
    command: str
    status: str = "OK"  # OK | DEGRADED | CHANGED | UNCHANGED | FAILED | INACTIVE
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None

    def add(self, message: str) -> None:
        self.messages.append(HOST.text(message))

    def warn(self, message: str) -> None:
        self.warnings.append(HOST.text(message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA,
            "command": self.command,
            "status": self.status,
            "messages": [HOST.text(item) for item in self.messages],
            "warnings": [HOST.text(item) for item in self.warnings],
            "data": _safe(self.data),
        }


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return HOST.text(value)
    return value


# ---- state ----------------------------------------------------------------


@dataclass
class DeploymentState:
    mode: str = "tun"
    degraded: bool = False
    reasons: list[str] = field(default_factory=list)
    tun_ok: bool = False
    services: list[str] = field(default_factory=list)
    updated_at: str = ""
    schema: int = STATE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mode": self.mode,
            "degraded": self.degraded,
            "reasons": list(self.reasons),
            "tun_ok": self.tun_ok,
            "services": list(self.services),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "DeploymentState":
        if not isinstance(payload, dict):
            return cls()
        reasons = payload.get("reasons") or []
        services = payload.get("services") or []
        return cls(
            mode=str(payload.get("mode") or "tun"),
            degraded=bool(payload.get("degraded")),
            reasons=[str(item) for item in reasons] if isinstance(reasons, list) else [],
            tun_ok=bool(payload.get("tun_ok")),
            services=[str(item) for item in services] if isinstance(services, list) else [],
            updated_at=str(payload.get("updated_at") or ""),
            schema=int(payload.get("schema") or STATE_SCHEMA),
        )


def load_state(layout: Layout) -> DeploymentState | None:
    path = layout.deployment_state
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return DeploymentState.from_dict(json.load(handle))
    except (OSError, ValueError):
        return None


def save_state(layout: Layout, state: DeploymentState) -> None:
    state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_atomic(
        layout.deployment_state,
        json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )


# ---- secrets --------------------------------------------------------------


@dataclass
class Secrets:
    subscription_url: str
    controller_secret: str
    interval_minutes: int = 60


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_secrets(
    layout: Layout,
    *,
    environ: dict[str, str] | None = None,
    system_profile: bool = True,
) -> Secrets:
    """Environment overrides the 0600 file; a missing value fails closed."""
    values: dict[str, str] = {}
    path = layout.secret_file
    if os.path.isfile(path):
        if system_profile:
            check_secret_input(path, require_root_owned=True)
        values = parse_env(read_text_strict(path))
    env = environ or {}
    url = env.get("MPM_SUBSCRIPTION_URL") or values.get("SUBSCRIPTION_URL", "")
    secret = env.get("MPM_SECRET") or values.get("SECRET", "")
    interval = env.get("MPM_SUBSCRIPTION_INTERVAL") or values.get("SUBSCRIPTION_INTERVAL") or "60"
    if not url:
        raise FailClosed(
            "subscription URL is not configured (state/subscription.env SUBSCRIPTION_URL "
            "or MPM_SUBSCRIPTION_URL)"
        )
    if not secret:
        raise FailClosed(
            "controller secret is not configured (state/subscription.env SECRET or MPM_SECRET)"
        )
    try:
        minutes = int(interval)
    except ValueError as exc:
        raise FailClosed("SUBSCRIPTION_INTERVAL must be an integer number of minutes") from exc
    if minutes < 0:
        raise FailClosed("SUBSCRIPTION_INTERVAL must not be negative")
    return Secrets(subscription_url=url, controller_secret=secret, interval_minutes=minutes)


def save_secrets(layout: Layout, secrets: Secrets) -> None:
    payload = (
        "# 0600 secret file - never commit.\n"
        f"SUBSCRIPTION_URL={secrets.subscription_url}\n"
        f"SECRET={secrets.controller_secret}\n"
        f"SUBSCRIPTION_INTERVAL={secrets.interval_minutes}\n"
    )
    write_atomic(layout.secret_file, payload, mode=0o600)


# ---- TUN observation ------------------------------------------------------


@dataclass(frozen=True)
class TunObservation:
    interfaces: tuple[str, ...] = ()
    default_interface: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "interfaces": list(self.interfaces),
            "default_interface": self.default_interface,
        }


def parse_ifconfig_list(text: str) -> tuple[str, ...]:
    return tuple(item for item in (text or "").split() if item)


def parse_default_interface(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("interface:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def observe_tun(executor: Executor) -> TunObservation:
    """Read interfaces and the default route (read-only, never modifies)."""
    interfaces = parse_ifconfig_list(executor.run(["ifconfig", "-l"]).stdout)
    route = executor.run(["route", "-n", "get", "default"]).stdout
    return TunObservation(interfaces=interfaces, default_interface=parse_default_interface(route))


def tun_established(before: TunObservation, after: TunObservation) -> bool:
    """A new ``utun*`` interface, or the default route moved onto one.

    Merely *seeing* a utun is not enough: other VPN clients create them too.
    """
    new_interfaces = set(after.interfaces) - set(before.interfaces)
    if any(name.startswith("utun") for name in new_interfaces):
        return True
    return (
        after.default_interface.startswith("utun")
        and after.default_interface != before.default_interface
    )


# ---- context --------------------------------------------------------------


@dataclass
class Context:
    layout: Layout
    facts: PlatformFacts
    executor: Executor
    out: TextIO = sys.stdout
    err: TextIO = sys.stderr
    environ: dict[str, str] = field(default_factory=dict)
    controller_factory: Callable[[str], ControllerClient] | None = None
    converter: Callable[[str, str], list[str]] | None = None
    validator: Callable[[str], None] | None = None
    proxy: NetworkProxy | None = None
    tun_observer: Callable[[], TunObservation] | None = None
    sleeper: Callable[[float], None] = time.sleep
    system_profile: bool = True
    dry_run: bool = False
    python: str = sys.executable
    quiet: bool = False
    # injected in tests so the lifecycle lock needs no real ``fcntl``
    flock: Callable[[int, int], None] | None = None

    # ---- helpers -------------------------------------------------------
    def say(self, message: str) -> None:
        self.out.write(sanitize(message).rstrip("\n") + "\n")

    def shout(self, message: str) -> None:
        self.out.write("\n" + "!" * 72 + "\n" + sanitize(message).rstrip("\n") + "\n" + "!" * 72 + "\n")

    def lifecycle_guard(self) -> FileLock:
        """The single lock serialising every state-changing command.

        Only the CLI entry point acquires it; the lifecycle functions it calls
        (``install`` -> ``start`` -> ``_fallback_to_mixed``) never re-acquire,
        so there is no re-entrant flock and therefore no self-deadlock.
        """
        return FileLock(self.layout.lifecycle_lock, label="lifecycle", flock=self.flock)

    def launchd(self) -> Launchd:
        return Launchd(self.executor, plist=self.layout.plist, sleeper=self.sleeper)

    def proxy_client(self) -> NetworkProxy:
        return self.proxy or NetworkProxy(self.executor)

    def controller(self, secret: str) -> ControllerClient:
        if self.controller_factory is not None:
            return self.controller_factory(secret)
        return ControllerClient(secret=secret)

    def tun(self) -> TunObservation:
        if self.tun_observer is not None:
            return self.tun_observer()
        return observe_tun(self.executor)

    def secrets(self) -> Secrets:
        return load_secrets(
            self.layout, environ=self.environ, system_profile=self.system_profile
        )


# ---- shared preparation ---------------------------------------------------


def _prepare_dirs(ctx: Context) -> list[str]:
    created: list[str] = []
    for path, mode in managed_dirs(ctx.layout):
        existed = os.path.isdir(path)
        ensure_dir(path, mode)
        if not existed:
            created.append(path)
    return created


def _source_dir() -> str:
    """The ``macos/`` directory this package was shipped in."""
    configured = os.environ.get("MPM_SOURCE_DIR", "")
    if configured and os.path.isdir(configured):
        return configured
    # macos/python/mpm_macos/lifecycle.py -> macos/
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _repo_root() -> str:
    return os.path.dirname(_source_dir())


def _copy_tree(src: str, dest: str, *, mode: int) -> list[str]:
    if os.path.abspath(src) == os.path.abspath(dest):
        return []
    if not os.path.isdir(src):
        # re-running install from the installed copy: the package is already in
        # place, so a missing source tree is not an error
        if os.path.isdir(dest) and any(name.endswith(".py") for name in os.listdir(dest)):
            return []
        raise FailClosed(f"source package directory is missing: {src}")
    changed: list[str] = []
    ensure_dir(dest, 0o755)
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py"):
            continue
        source = os.path.join(src, name)
        if not os.path.isfile(source):
            continue
        payload = read_text_strict(source)
        target = os.path.join(dest, name)
        if is_noop(target, payload, mode=mode):
            continue
        write_atomic(target, payload, mode=mode)
        changed.append(name)
    return changed


def _copy_file(source: str, target: str, *, mode: int) -> bool:
    if not os.path.isfile(source):
        if os.path.isfile(target):
            return False  # already installed from a previous run
        raise FailClosed(f"required source file is missing: {source}")
    payload = read_text_strict(source)
    if is_noop(target, payload, mode=mode):
        return False
    write_atomic(target, payload, mode=mode)
    return True


def _install_static(ctx: Context) -> list[str]:
    """Install packages, templates, scripts and the thin CLI wrapper."""
    layout = ctx.layout
    source = _source_dir()
    repo = _repo_root()
    changed: list[str] = []

    changed += _copy_tree(
        os.path.join(repo, "common", "python", "mpm_common"),
        os.path.join(layout.share_python, "mpm_common"),
        mode=0o644,
    )
    changed += _copy_tree(
        os.path.join(source, "python", "mpm_macos"),
        os.path.join(layout.share_python, "mpm_macos"),
        mode=0o644,
    )

    template_sources = {
        os.path.join(source, "templates", os.path.basename(layout.plist_template)): layout.plist_template,
        os.path.join(repo, "config", "config.yaml.template"): layout.config_template,
    }
    for src, dest in template_sources.items():
        if _copy_file(src, dest, mode=0o644):
            changed.append(os.path.basename(dest))

    scripts_src = os.path.join(source, "scripts")
    if os.path.isdir(scripts_src):
        for name in sorted(os.listdir(scripts_src)):
            if not name.endswith(".sh"):
                continue
            if _copy_file(os.path.join(scripts_src, name), os.path.join(layout.scripts_dir, name), mode=0o755):
                changed.append(name)

    convert_src = os.path.join(repo, "scripts", "convert_sub.py")
    if _copy_file(convert_src, layout.convert_sub, mode=0o755):
        changed.append("convert_sub.py")

    _install_cli(ctx)
    _install_runner(ctx)
    return changed


def _install_cli(ctx: Context) -> None:
    layout = ctx.layout
    source = os.path.join(_source_dir(), "bin", "mihomo-proxy-management")
    if not os.path.isfile(source):
        if os.path.isfile(layout.cli_entry):
            return  # installed copy is already in place
        raise FailClosed(f"CLI wrapper source is missing: {source}")
    payload = read_text_strict(source)
    payload = payload.replace("__PACKAGE_DIR__", layout.share_python)
    if is_noop(layout.cli_entry, payload, mode=0o755):
        return
    write_atomic(layout.cli_entry, payload, mode=0o755)


_RUNNER_WRAPPER = """#!/bin/sh
# Static wrapper - all logic lives in mpm_macos.runner.
PYTHONPATH="__PACKAGE_DIR__"
export PYTHONPATH
exec "__PYTHON__" -m mpm_macos.runner "$@"
"""


def _install_runner(ctx: Context) -> None:
    layout = ctx.layout
    payload = _RUNNER_WRAPPER.replace("__PACKAGE_DIR__", layout.share_python).replace(
        "__PYTHON__", ctx.python
    )
    if is_noop(layout.runner, payload, mode=0o755):
        return
    write_atomic(layout.runner, payload, mode=0o755)


# ---- configure ------------------------------------------------------------


def _read_provider_nodes(ctx: Context) -> list[str]:
    path = ctx.layout.provider_file
    if os.path.isfile(path):
        names = config_mod.provider_node_names(read_text_strict(path))
        if names:
            return names
    return []


def _ensure_provider(ctx: Context, secrets: Secrets) -> list[str]:
    names = _read_provider_nodes(ctx)
    if names:
        return names
    if ctx.converter is not None:
        names = ctx.converter(secrets.subscription_url, ctx.layout.provider_file)
    else:
        module = config_mod.load_convert_module(ctx.layout.convert_sub)
        names = config_mod.convert_provider(
            secrets.subscription_url, ctx.layout.provider_file, module=module
        )
    if not names:
        raise FailClosed("the subscription produced no provider nodes")
    return names


def _default_validator(ctx: Context) -> Callable[[str], None]:
    def validate(text: str) -> None:
        if not os.path.isfile(ctx.layout.binary):
            return  # nothing to validate against; install will require the binary
        with secure_tempdir(ctx.layout.staging_dir, "validate") as staging:
            candidate = os.path.join(staging, "config.yaml")
            write_atomic(candidate, text, mode=0o600, mkdir=False)
            config_mod.validate(ctx.layout.binary, candidate, ctx.layout.state_dir, ctx.executor)

    return validate


def configure(
    ctx: Context,
    *,
    quiet: bool = False,
    mode: str | None = None,
    provider_nodes: list[str] | None = None,
) -> Report:
    """Render, audit and atomically install ``config.yaml``.  Repeatable."""
    report = Report(command="configure")
    layout = ctx.layout
    secrets = ctx.secrets()
    state = load_state(layout) or DeploymentState()
    target_mode = mode or state.mode or "tun"

    nodes = provider_nodes if provider_nodes is not None else _read_provider_nodes(ctx)
    if not nodes:
        raise FailClosed(
            "provider node list is empty; cannot validate the CURSOR/CODEBUDDY policy"
        )
    verdict = policy.enforce(nodes)
    for warning in verdict.warnings:
        report.warn(warning)
    report.data["policy"] = verdict.to_dict()

    template_path = layout.config_template
    if not os.path.isfile(template_path):
        template_path = os.path.join(_repo_root(), "config", "config.yaml.template")
    template_text = config_mod.load_template(template_path)
    rendered = config_mod.render(
        template_text,
        secret=secrets.controller_secret,
        subscription_url=secrets.subscription_url,
        interval_minutes=secrets.interval_minutes,
        mode=target_mode,
        provider_path=layout.provider_file,
    )

    validator = ctx.validator or _default_validator(ctx)
    validator(rendered)

    if is_noop(layout.config, rendered, mode=0o600):
        check_output_mode(layout.config)
        report.status = "UNCHANGED"
    else:
        write_atomic(layout.config, rendered, mode=0o600)
        report.status = "CHANGED"
    if target_mode == "tun":
        if not is_noop(layout.config_tun_backup, rendered, mode=0o600):
            write_atomic(layout.config_tun_backup, rendered, mode=0o600)

    state.mode = target_mode
    state.degraded = target_mode == "mixed"
    state.tun_ok = target_mode == "tun"
    if target_mode == "tun":
        state.reasons = []
        state.services = []
    save_state(layout, state)

    report.data["mode"] = target_mode
    # keep the "sha256:" prefix: the sanitizer preserves digests, but redacts
    # bare opaque blobs
    report.data["config_sha256"] = "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if not quiet:
        report.add(f"config rendered ({target_mode}) at mode 0600")
    return report


# ---- plist ----------------------------------------------------------------


def _install_plist(ctx: Context) -> bool:
    layout = ctx.layout
    # The plist describes the macOS target, not the rendering host: with a
    # sandbox root (tests) the paths are mapped back onto the POSIX target.
    target = layout.to_target_path
    rendered = plist_mod.render_from_file(
        layout.plist_template,
        label=LABEL,
        python=target(ctx.python),
        package_dir=target(layout.share_python),
        state_dir=target(layout.support),
        config=target(layout.config),
        binary=target(layout.binary),
        run_dir=target(layout.run_dir),
        log_out=target(layout.log_out),
        log_err=target(layout.log_err),
    )
    plist_mod.audit_text(rendered, label=LABEL, state_dir=target(layout.support))
    if is_noop(layout.plist, rendered, mode=0o644):
        return False
    write_atomic(layout.plist, rendered, mode=0o644)
    return True


# ---- install --------------------------------------------------------------


def _install_binary(ctx: Context) -> bool:
    """Copy a user-supplied mihomo into ``libexec`` (this installer never downloads).

    ``MPM_MIHOMO_BINARY`` selects the source explicitly; otherwise ``mihomo``
    on ``PATH`` is used.  Geo databases found next to the source are copied to
    the state directory (``mihomo -d``).
    """
    layout = ctx.layout
    source = ctx.environ.get("MPM_MIHOMO_BINARY") or shutil.which("mihomo")
    if not source:
        if os.path.isfile(layout.binary):
            return False
        raise FailClosed(
            "mihomo binary not found: set MPM_MIHOMO_BINARY or put mihomo on PATH "
            "(this installer never downloads)"
        )
    if not os.path.isfile(source):
        raise FailClosed(f"mihomo binary does not exist: {source}")

    changed = False
    if os.path.abspath(source) != os.path.abspath(layout.binary):
        with open(source, "rb") as handle:
            payload = handle.read()
        existing = b""
        if os.path.isfile(layout.binary):
            with open(layout.binary, "rb") as handle:
                existing = handle.read()
        if existing != payload:
            write_atomic(layout.binary, payload, mode=0o755)
            changed = True

    source_dir = os.path.dirname(os.path.abspath(source))
    for name in ("GeoIP.dat", "GeoSite.dat", "Country.mmdb"):
        candidate = os.path.join(source_dir, name)
        target = os.path.join(layout.support, name)
        if not os.path.isfile(candidate):
            continue
        if os.path.abspath(candidate) == os.path.abspath(target):
            continue
        with open(candidate, "rb") as handle:
            payload = handle.read()
        write_atomic(target, payload, mode=0o644)
    return changed


def install(ctx: Context, *, quiet: bool = False, skip_start: bool = False) -> Report:
    report = Report(command="install")
    layout = ctx.layout
    check = preflight_mod.preflight(ctx.facts)
    for warning in check.warnings:
        report.warn(warning)
    secrets = ctx.secrets()
    _prepare_dirs(ctx)
    if not os.path.isfile(layout.secret_file):
        save_secrets(layout, secrets)

    changed = _install_static(ctx)
    report.data["installed_files"] = len(changed)
    if _install_binary(ctx):
        report.add("mihomo binary installed")

    nodes = _ensure_provider(ctx, secrets)
    report.data["provider_nodes"] = len(nodes)

    config_report = configure(ctx, quiet=quiet, provider_nodes=nodes)
    report.data["configure"] = config_report.status
    report.warnings.extend(config_report.warnings)

    if _install_plist(ctx):
        report.add("LaunchDaemon plist installed")
    else:
        report.add("LaunchDaemon plist unchanged")

    if skip_start:
        report.status = "OK"
        report.add("install complete; service not started (--no-start)")
        return report

    start_report = start(ctx, quiet=True)
    report.status = start_report.status
    report.data["start"] = start_report.status
    report.messages.extend(start_report.messages)
    report.warnings.extend(start_report.warnings)
    if start_report.status == "DEGRADED":
        report.add("DEGRADED: TUN failed; running mixed-port with the system proxy set")
    if not quiet:
        report.add("installed; management CLI at " + layout.cli_entry)
    return report


# ---- start / stop / restart ----------------------------------------------


def _stop_core(ctx: Context) -> None:
    launchd = ctx.launchd()
    launchd.bootout()
    if not launchd.wait_stopped():
        raise NotReady("the core is still running after launchctl bootout")


def _readiness(ctx: Context, secrets: Secrets) -> str:
    return readiness(ctx.controller(secrets.controller_secret), sleeper=ctx.sleeper)


def _health_check(ctx: Context, secrets: Secrets, *, mode: str) -> HealthReport:
    """The shared verdict: provider healthcheck plus at least one live node.

    Same code path as ``status``/``health``, so a mode is never declared usable
    on a weaker check than the one the operator sees later.
    """
    return evaluate(
        ctx.controller(secrets.controller_secret),
        provider=config_mod.PROVIDER_NAME,
        mode=mode,
        running=True,
    )


def _health_failure(verdict: HealthReport, *, mode: str) -> str:
    reasons = "; ".join(verdict.reasons) or "health check failed"
    return HOST.text(f"{mode} core is not healthy: {reasons}")[:200]


def _failure_reason(exc: BaseException) -> str:
    detail = exc.message if isinstance(exc, MpmError) else (str(exc) or type(exc).__name__)
    return HOST.text(detail)[:200]


def _restore_proxy_if_any(ctx: Context) -> list[str]:
    """Restore an unfinished proxy transaction; delete the snapshot only on success."""
    layout = ctx.layout
    if not snapshot_exists(layout.network_backup):
        return []
    snapshot = load_snapshot(layout.network_backup)
    restored = ctx.proxy_client().restore(snapshot)
    if not layout.is_project_path(layout.network_backup):
        raise FailClosed("refusing to delete a snapshot outside the project prefixes")
    os.unlink(layout.network_backup)
    return restored


def _fallback_to_mixed(ctx: Context, reason: str, *, quiet: bool = False) -> Report:
    """Transaction: snapshot -> mixed config -> healthy core -> system proxy."""
    layout = ctx.layout
    report = Report(command="start", status="DEGRADED")
    report.warn(reason)
    report.add("TUN unavailable: falling back to mixed-port mode")

    # a leftover snapshot means an earlier fallback was interrupted
    _restore_proxy_if_any(ctx)
    _stop_core(ctx)

    proxy = ctx.proxy_client()
    snapshot = proxy.snapshot()
    save_snapshot(layout.network_backup, snapshot)

    try:
        configure(ctx, quiet=True, mode="mixed")
        launchd = ctx.launchd()
        launchd.bootstrap()
        if not launchd.wait_running():
            raise NotReady("mixed-mode core did not reach running state")
        secrets = ctx.secrets()
        report.data["version"] = _readiness(ctx, secrets)
        verdict = _health_check(ctx, secrets, mode="mixed")
        report.data["health"] = verdict.to_dict()
        if not verdict.ok:
            # fail closed: never point the system proxy at an unhealthy core
            raise NotReady(_health_failure(verdict, mode="mixed"))
        modified = proxy.apply(snapshot)
        snapshot.stage = STAGE_APPLIED
        save_snapshot(layout.network_backup, snapshot, overwrite=True)
    except Exception:
        # roll back; a failure here keeps the snapshot for the next run
        try:
            _stop_core(ctx)
        except Exception:
            pass
        try:
            proxy.restore(snapshot)
            if snapshot_exists(layout.network_backup):
                os.unlink(layout.network_backup)
        except Exception:
            pass
        raise

    state = load_state(layout) or DeploymentState()
    state.mode = "mixed"
    state.degraded = True
    state.tun_ok = False
    state.reasons = [HOST.text(reason)]
    state.services = list(modified)
    save_state(layout, state)

    report.data["mode"] = "mixed"
    report.data["services"] = list(modified)
    ctx.shout(
        "DEGRADED: TUN failed, running mixed-port 127.0.0.1:7890; "
        "system proxy set on: " + ", ".join(modified)
    )
    if not quiet:
        report.add("mixed mode active; `stop` restores the saved proxy settings")
    return report


def start(ctx: Context, *, quiet: bool = False) -> Report:
    report = Report(command="start")
    layout = ctx.layout
    check = preflight_mod.preflight(ctx.facts)
    for warning in check.warnings:
        report.warn(warning)

    launchd = ctx.launchd()
    current = launchd.state()
    state = load_state(layout) or DeploymentState()

    if current.running:
        report.status = "UNCHANGED"
        if state.degraded:
            report.status = "DEGRADED"
            report.warn("; ".join(state.reasons) or "degraded mode recorded")
        if not quiet:
            report.add("core already running; no second instance started")
        return report

    if not os.path.isfile(layout.plist):
        raise FailClosed("LaunchDaemon plist is not installed; run install first")

    # An unfinished proxy transaction (interrupted fallback) is restored before
    # a fresh TUN attempt, so the host never starts from a half-applied state.
    restored = _restore_proxy_if_any(ctx)
    if restored:
        report.add("restored an unfinished proxy transaction: " + ", ".join(restored))

    if not os.path.isfile(layout.config):
        configure(ctx, quiet=True)

    # A previous fallback left a mixed config behind.  Every explicit start
    # retries TUN; the fallback is a runtime decision, not a permanent policy.
    if state.mode == "mixed":
        configure(ctx, quiet=True, mode="tun")

    before = ctx.tun()
    after = before
    version = ""
    failure = ""
    try:
        if current.loaded:
            launchd.kickstart()
        else:
            launchd.bootstrap()
        if not launchd.wait_running():
            raise NotReady("LaunchDaemon did not reach running state")

        secrets = ctx.secrets()
        version = _readiness(ctx, secrets)
        report.data["version"] = version

        verdict = _health_check(ctx, secrets, mode="tun")
        report.data["health"] = verdict.to_dict()
        if not verdict.ok:
            raise NotReady(_health_failure(verdict, mode="tun"))

        after = ctx.tun()
        if not tun_established(before, after):
            raise NotReady(
                "mihomo did not establish a utun interface or take over the default route"
            )
    except (MpmError, ControllerError, NetworkProxyError, OSError) as exc:
        # launch, readiness, health and utun verification are one transaction:
        # every one of them failing leads to the same mixed-port fallback.
        failure = _failure_reason(exc)

    if failure:
        return _fallback_to_mixed(ctx, failure, quiet=quiet)

    state.mode = "tun"
    state.degraded = False
    state.tun_ok = True
    state.reasons = []
    state.services = []
    save_state(layout, state)
    report.data["mode"] = "tun"
    report.data["tun"] = after.to_dict()
    if not quiet:
        report.add(f"active in TUN mode, controller ready (mihomo {version})")
    return report


def stop(ctx: Context, *, quiet: bool = False) -> Report:
    report = Report(command="stop")
    launchd = ctx.launchd()
    state = launchd.state()
    if state.loaded:
        _stop_core(ctx)
    restored = _restore_proxy_if_any(ctx)
    if restored:
        report.add("system proxy restored on: " + ", ".join(restored))
    deployment = load_state(ctx.layout)
    if deployment is not None:
        deployment.degraded = False
        deployment.reasons = []
        deployment.services = []
        save_state(ctx.layout, deployment)
    report.status = "OK" if state.loaded else "UNCHANGED"
    if not quiet:
        report.add("core stopped" if state.loaded else "core already stopped")
    return report


def restart(ctx: Context, *, quiet: bool = False) -> Report:
    report = Report(command="restart")
    stop(ctx, quiet=True)
    started = start(ctx, quiet=True)
    report.status = started.status
    report.data.update(started.data)
    report.warnings.extend(started.warnings)
    if not quiet:
        report.add("restarted (stop confirmed before start)")
    return report


# ---- status / health ------------------------------------------------------


def _proxy_matches(ctx: Context, state: DeploymentState) -> bool | None:
    """Whether every service still points at this proxy (mixed mode only)."""
    if state.mode != "mixed":
        return None
    try:
        services = ctx.proxy_client().list_services()
        for entry in services:
            if entry.disabled:
                continue
            current = ctx.proxy_client().read_service(entry)
            for setting in (current.web, current.secure, current.socks):
                if not setting.enabled or setting.server != "127.0.0.1" or setting.port != 7890:
                    return False
    except NetworkProxyError:
        return None
    return True


def status(ctx: Context) -> Report:
    report = Report(command="status")
    layout = ctx.layout
    launchd_state = ctx.launchd().state()
    state = load_state(layout) or DeploymentState()
    core_locked = is_held(layout.core_lock)

    data: dict[str, Any] = {
        "launchd": launchd_state.to_dict(),
        "mode": state.mode,
        "degraded": state.degraded,
        "degraded_reasons": list(state.reasons),
        "tun_ok": state.tun_ok,
        "core_lock": {"held": core_locked, "metadata": read_metadata(layout.core_lock)},
        "config": {"present": os.path.isfile(layout.config)},
        "plist": {"present": os.path.isfile(layout.plist)},
        "network_proxy": {"matches_saved_state": _proxy_matches(ctx, state)},
    }

    client: ControllerClient | None = None
    try:
        secrets = ctx.secrets()
        client = ctx.controller(secrets.controller_secret)
    except FailClosed as exc:
        report.warn(f"controller secret not readable: {HOST.text(str(exc))[:120]}")

    health = evaluate(
        client,
        provider=config_mod.PROVIDER_NAME,
        mode=state.mode,
        running=launchd_state.running,
        degraded_reasons=state.reasons if state.degraded else [],
    )
    data["health"] = health.to_dict()
    data["version"] = health.data.get("version")

    report.data = data
    report.status = health.state
    for reason in health.reasons:
        report.warn(reason)
    if state.degraded and report.status == HEALTH_OK:
        report.status = HEALTH_DEGRADED
    return report


def health(ctx: Context) -> Report:
    report = Report(command="health")
    state = load_state(ctx.layout) or DeploymentState()
    launchd_state = ctx.launchd().state()
    client: ControllerClient | None = None
    try:
        secrets = ctx.secrets()
        client = ctx.controller(secrets.controller_secret)
    except FailClosed as exc:
        report.warn(f"controller secret not readable: {HOST.text(str(exc))[:120]}")
    verdict = evaluate(
        client,
        provider=config_mod.PROVIDER_NAME,
        mode=state.mode,
        running=launchd_state.running,
        degraded_reasons=state.reasons if state.degraded else [],
    )
    report.status = verdict.state
    report.data = verdict.to_dict()
    report.data["mode"] = state.mode
    for reason in verdict.reasons:
        report.warn(reason)
    return report


# ---- uninstall ------------------------------------------------------------


def uninstall(ctx: Context, *, purge: bool = False, quiet: bool = False) -> Report:
    report = Report(command="uninstall")
    layout = ctx.layout
    stop(ctx, quiet=True)

    removed: list[str] = []

    def discard(path: str) -> None:
        if not layout.is_project_path(path):
            raise FailClosed(f"refusing to delete a path outside the project prefixes: {path}")
        if remove_if_present(path):
            removed.append(path)

    discard(layout.plist)
    discard(layout.cli_entry)
    discard(layout.libexec)
    discard(layout.share)
    if purge:
        discard(layout.support)
        discard(layout.run_dir)
        report.add("purge: configuration, provider, logs and state removed")
    else:
        report.add("kept config, provider, logs and state (use --purge to remove them)")

    report.data["removed"] = [os.path.relpath(path, layout.root) for path in removed]
    report.data["dry_run"] = ctx.dry_run
    report.status = "OK"
    if not quiet:
        report.add("uninstalled")
    return report


# ---- preflight command ----------------------------------------------------


def run_preflight(ctx: Context) -> Report:
    report = Report(command="preflight")
    check = preflight_mod.evaluate(ctx.facts)
    report.data = check.to_dict()
    report.status = "OK" if check.supported else "FAILED"
    if not check.supported:
        report.exit_code = ExitCode.UNSUPPORTED
    for warning in check.warnings:
        report.warn(warning)
    for reason in check.reasons:
        report.warn(reason)
    return report


__all__ = [
    "Context",
    "DeploymentState",
    "Report",
    "SUBCOMMANDS",
    "Secrets",
    "TunObservation",
    "configure",
    "health",
    "install",
    "load_secrets",
    "load_state",
    "observe_tun",
    "parse_default_interface",
    "parse_ifconfig_list",
    "restart",
    "run_preflight",
    "save_secrets",
    "save_state",
    "start",
    "status",
    "stop",
    "tun_established",
    "uninstall",
]
