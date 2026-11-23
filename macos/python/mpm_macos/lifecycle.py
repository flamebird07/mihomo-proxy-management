"""macOS lifecycle engine - the one implementation behind every subcommand.

Invariants:

* every operation is idempotent: a second run reports ``UNCHANGED`` or
  re-verifies, and never starts a second instance;
* a config only replaces the live one after the static audit *and*
  ``mihomo -t`` passed;
* there is exactly one mihomo home (``-d``): ``layout.support``.  The plist's
  ``--state-dir``/``WorkingDirectory``, ``mihomo -t -d``, the geo databases and
  the rendered provider path (``support/providers/subscription.yaml``) all use
  it; ``state/`` only holds this project's secret/deployment/backup files;
* TUN is only claimed when the process, the controller and an injected
  interface/route probe agree;
* a start failure is a transaction: stop the core (``bootout`` + persistent
  ``disable``), restore the proxy snapshot and report ``FAILED``.  There is no
  automatic switch to mixed mode and never a system-proxy takeover: mixed is
  only used when the operator explicitly asks for it
  (``configure --mode mixed`` + ``start --mode mixed``);
* an external VPN (Nextin) that already owns routing/DNS is a hard stop: the
  core is not started and mixed is not used as a way around it;
* ``stop`` always restores the proxy snapshot and only deletes it after a
  successful read-back;
* every printed line is sanitised.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, NoReturn, TextIO

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
    evaluate_with_deadline,
    readiness,
)
from mpm_common.errors import ExitCode, FailClosed, MpmError, NotReady
from mpm_common.executor import Executor
from mpm_common.platforms import PlatformFacts
from mpm_common.sanitize import HOST, sanitize

from . import config as config_mod
from . import conflict as conflict_mod
from . import context as context_mod
from . import feishu_sync as feishu_sync_mod
from . import node_pipeline as node_pipeline_mod
from . import plist as plist_mod
from . import preflight as preflight_mod
from . import tun_dns as tun_dns_mod
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
from .paths import (
    BOOT_LABEL,
    FEISHU_SYNC_LABEL,
    LABEL,
    NODE_PIPELINE_LABEL,
    XIGUA_IPPOOL_LABEL,
    Layout,
    managed_dirs,
)

SUBCOMMANDS = (
    "preflight",
    "install",
    "configure",
    "start",
    "stop",
    "restart",
    "status",
    "health",
    "groups",
    "update-sources",
    "uninstall",
    "switch",
    "probe",
)

STATE_SCHEMA = 1

#: Total budget for the start-time health gate (healthcheck trigger + polling).
#: The healthcheck endpoint blocks until every node was tested; on a slow
#: subscription the old 5 s client timeout produced a bogus "controller
#: unreachable" (2026-11-14).  ``MPM_HEALTH_DEADLINE_SECONDS`` overrides it.
HEALTH_DEADLINE_ENV = "MPM_HEALTH_DEADLINE_SECONDS"
HEALTH_DEADLINE_DEFAULT = 45.0
HEALTH_DEADLINE_RANGE = (10.0, 300.0)


def health_deadline_seconds(environ: dict[str, str] | None = None) -> float:
    """Configured start-time health budget, clamped to a sane range."""
    values = environ if environ is not None else dict(os.environ)
    raw = (values.get(HEALTH_DEADLINE_ENV) or "").strip()
    if not raw:
        return HEALTH_DEADLINE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return HEALTH_DEADLINE_DEFAULT
    low, high = HEALTH_DEADLINE_RANGE
    return min(high, max(low, value))


#: Geo databases copied next to the binary into the mihomo ``-d`` directory.
#: ``GeoIP.dat``/``GeoSite.dat`` are what ``geodata-mode: true`` loads;
#: ``Country.mmdb`` is kept for a future ``geodata-mode: false`` config.
_GEO_DATA_FILES = ("GeoIP.dat", "GeoSite.dat", "Country.mmdb")


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


# ---- ownership ------------------------------------------------------------

ROOT_UID = 0
ROOT_GID = 0


def _default_geteuid() -> int:
    """``-1`` where the platform has no ``geteuid`` (Windows) - never root."""
    getter = getattr(os, "geteuid", None)
    return getter() if getter is not None else -1


def _default_chown(path: str, uid: int, gid: int) -> None:
    """Real ``chown`` on POSIX; a no-op where the platform has none (Windows)."""
    chown = getattr(os, "chown", None)
    if chown is not None:
        chown(path, uid, gid)


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
    #: Optional Feishu bitable credentials for the sync daemon.  Empty means
    #: "not configured yet": install still succeeds, the sync run reports the
    #: missing keys and exits non-zero until the operator copies them from the
    #: Windows host's ``~/.hermes/.env``.
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_bitable_app_token: str = ""
    #: Optional additional subscription sources (``SUBSCRIPTION_URL_2..4``),
    #: e.g. the airport the Nextin app subscribes to.  The Feishu speedtest
    #: sync and ``update-sources`` read these keys from the same 0600 file;
    #: they are never required and never overwrite the live provider.
    extra_subscription_urls: dict[int, str] = field(default_factory=dict)


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
    if minutes <= 0:
        # A zero interval would be silently clamped to the 3600 s floor, which
        # hides the "minutes written into a seconds field" class of mistake.
        raise FailClosed("SUBSCRIPTION_INTERVAL must be at least 1 minute")
    feishu_app_id = env.get("MPM_FEISHU_APP_ID") or values.get("FEISHU_APP_ID", "")
    feishu_app_secret = env.get("MPM_FEISHU_APP_SECRET") or values.get("FEISHU_APP_SECRET", "")
    feishu_token = env.get("MPM_FEISHU_BITABLE_APP_TOKEN") or values.get(
        "FEISHU_BITABLE_APP_TOKEN", ""
    )
    extras: dict[int, str] = {}
    for index in (2, 3, 4):
        extra = (
            env.get(f"MPM_SUBSCRIPTION_URL_{index}")
            or values.get(f"SUBSCRIPTION_URL_{index}", "")
        ).strip()
        if extra:
            extras[index] = extra
    return Secrets(
        subscription_url=url,
        controller_secret=secret,
        interval_minutes=minutes,
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        feishu_bitable_app_token=feishu_token,
        extra_subscription_urls=extras,
    )


def save_secrets(layout: Layout, secrets: Secrets) -> None:
    """Write the 0600 secret file, preserving existing Feishu credentials.

    A re-install must never silently drop the three optional ``FEISHU_*`` keys
    (the operator copies them from the Windows host once), so they are kept
    from the file when the caller did not supply new values.  The file also
    carries the derived ``SUBSCRIPTION_URL_1`` mirror: the Feishu sync script
    reads its sources as ``SUBSCRIPTION_URL_1..4``, and mirroring the deployed
    ``SUBSCRIPTION_URL`` at every write means the mirror can never drift from
    the URL it mirrors.
    """
    existing: dict[str, str] = {}
    if os.path.isfile(layout.secret_file):
        try:
            existing = parse_env(read_text_strict(layout.secret_file))
        except Exception:  # noqa: BLE001 - unreadable old file: write fresh content
            existing = {}
    app_id = secrets.feishu_app_id or existing.get("FEISHU_APP_ID", "")
    app_secret = secrets.feishu_app_secret or existing.get("FEISHU_APP_SECRET", "")
    app_token = secrets.feishu_bitable_app_token or existing.get("FEISHU_BITABLE_APP_TOKEN", "")
    lines = [
        "# 0600 secret file - never commit.",
        f"SUBSCRIPTION_URL={secrets.subscription_url}",
        f"SECRET={secrets.controller_secret}",
        f"SUBSCRIPTION_INTERVAL={secrets.interval_minutes}",
        "# Derived by install: the Feishu sync reads sources as SUBSCRIPTION_URL_1..4.",
        f"SUBSCRIPTION_URL_1={secrets.subscription_url}",
    ]
    # Additional sources 2..4 (optional, e.g. the Nextin/wmsxwd subscription):
    # kept from the file when this call carries no new value, so a hand-added
    # source survives every re-install, and never reordered to break the
    # converter's duplicate-key check.
    for index in (2, 3, 4):
        url_key = f"SUBSCRIPTION_URL_{index}"
        value = secrets.extra_subscription_urls.get(index) or existing.get(url_key, "")
        if value:
            lines.append(f"{url_key}={value}")
    for index in (2, 3, 4):
        ua_key = f"SUBSCRIPTION_UA_{index}"
        if ua_key in existing:
            lines.append(f"{ua_key}={existing[ua_key]}")
    if app_id:
        lines.append(f"FEISHU_APP_ID={app_id}")
    if app_secret:
        lines.append(f"FEISHU_APP_SECRET={app_secret}")
    if app_token:
        lines.append(f"FEISHU_BITABLE_APP_TOKEN={app_token}")
    write_atomic(layout.secret_file, "\n".join(lines) + "\n", mode=0o600)


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
    conflict_detector: Callable[[], "conflict_mod.ConflictReport"] | None = None
    sleeper: Callable[[float], None] = time.sleep
    #: monotonic clock paired with ``sleeper``; tests inject a fake so no
    #: deadline test ever really sleeps
    clock: Callable[[], float] = time.monotonic
    system_profile: bool = True
    dry_run: bool = False
    python: str = sys.executable
    quiet: bool = False
    # injected in tests so the lifecycle lock needs no real ``fcntl``
    flock: Callable[[int, int], None] | None = None
    # injected in tests so ownership can be asserted without root (and on
    # Windows, where neither ``geteuid`` nor ``chown`` exists)
    geteuid: Callable[[], int] = _default_geteuid
    chown: Callable[[str, int, int], None] = _default_chown

    # ---- helpers -------------------------------------------------------
    def say(self, message: str) -> None:
        self.out.write(sanitize(message).rstrip("\n") + "\n")

    def shout(self, message: str) -> None:
        self.out.write("\n" + "!" * 72 + "\n" + sanitize(message).rstrip("\n") + "\n" + "!" * 72 + "\n")

    def lifecycle_guard(self) -> FileLock:
        """The single lock serialising every state-changing command.

        Only the CLI entry point acquires it; the lifecycle functions it calls
        (``install`` -> ``start``, ``switch`` -> ``start`` -> ``stop``) never
        re-acquire, so there is no re-entrant flock and therefore no
        self-deadlock.
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

    def conflicts(self) -> "conflict_mod.ConflictReport":
        if self.conflict_detector is not None:
            return self.conflict_detector()
        return conflict_mod.detect(self.executor)

    def secrets(self) -> Secrets:
        return load_secrets(
            self.layout, environ=self.environ, system_profile=self.system_profile
        )


def _own(ctx: Context, *paths: str) -> None:
    """Force ``root:wheel`` on installer artifacts of a root system install.

    BSD ``chown`` semantics: a new file inherits its *parent directory's* group,
    and ``/Library/Application Support`` belongs to ``admin``.  Without an
    explicit ``chown`` the installer's own ``state/subscription.env`` is
    ``root:admin`` and :func:`check_secret_input` (root:wheel) rejects it on the
    next read.  Sandbox/test runs (non-root) never chown, and a symlink is never
    followed - only the managed artifact itself is chowned.
    """
    if not ctx.system_profile or ctx.geteuid() != ROOT_UID:
        return
    for path in paths:
        if os.path.islink(path) or not os.path.exists(path):
            continue
        ctx.chown(path, ROOT_UID, ROOT_GID)


def _save_state(ctx: Context, state: DeploymentState) -> None:
    save_state(ctx.layout, state)
    _own(ctx, ctx.layout.deployment_state)


# ---- shared preparation ---------------------------------------------------


def _prepare_dirs(ctx: Context) -> list[str]:
    created: list[str] = []
    for path, mode in managed_dirs(ctx.layout):
        existed = os.path.isdir(path)
        ensure_dir(path, mode)
        if not existed:
            created.append(path)
    layout = ctx.layout
    _own(ctx, layout.support, layout.providers, layout.state_dir, layout.logs)
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
        os.path.join(
            source, "templates", os.path.basename(layout.boot_plist_template)
        ): layout.boot_plist_template,
        os.path.join(
            source, "templates", os.path.basename(layout.feishu_sync_plist_template)
        ): layout.feishu_sync_plist_template,
        os.path.join(
            source, "templates", os.path.basename(layout.node_pipeline_plist_template)
        ): layout.node_pipeline_plist_template,
        os.path.join(
            source, "templates", os.path.basename(layout.feishu_sync_plist_template)
        ): layout.feishu_sync_plist_template,
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

    # The Feishu sync daemon imports this script by path; it must sit next to
    # the installed convert_sub.py because it imports its sibling via sys.path.
    sync_src = os.path.join(repo, "scripts", feishu_sync_mod.SYNC_SCRIPT_NAME)
    if _copy_file(sync_src, layout.sync_script, mode=0o755):
        changed.append(feishu_sync_mod.SYNC_SCRIPT_NAME)

    for name in (
        "health-check-provider.py",
        "ippool-collector.py",
        "sync-xigua-ippool-feishu.py",
        "run-xigua-ippool.py",
        # the node pipeline's assignment chain (upstream run-node-pipeline port)
        "proxy-auto-switch.py",
        "portable_paths.py",
        "gpt_switch_entry.py",
        "gpt_stability_policy.py",
        "named_mutex_lock.py",
    ):
        if _copy_file(
            os.path.join(repo, "scripts", name),
            os.path.join(layout.scripts_dir, name),
            mode=0o755,
        ):
            changed.append(name)
    for package in (node_pipeline_mod.NODE_PROVIDER_PACKAGE, node_pipeline_mod.NODE_OPS_PACKAGE):
        changed += _copy_tree(
            os.path.join(repo, "scripts", package),
            os.path.join(layout.scripts_dir, package),
            mode=0o644,
        )

    _install_cli(ctx)
    _install_runner(ctx)
    return changed


def _install_cli(ctx: Context) -> None:
    layout = ctx.layout
    source = os.path.join(_source_dir(), "bin", "mihomo-proxy-management")
    if not os.path.isfile(source):
        if os.path.isfile(layout.cli_entry):
            pass  # installed copy is already in place
        else:
            raise FailClosed(f"CLI wrapper source is missing: {source}")
    else:
        payload = read_text_strict(source)
        payload = payload.replace("__PACKAGE_DIR__", layout.share_python).replace(
            "__PYTHON__", ctx.python
        )
        if not is_noop(layout.cli_entry, payload, mode=0o755):
            write_atomic(layout.cli_entry, payload, mode=0o755)

    switch_source = os.path.join(_source_dir(), "bin", "mpm-switch")
    if not os.path.isfile(switch_source):
        if os.path.isfile(layout.switch_entry):
            return
        raise FailClosed(f"mpm-switch wrapper source is missing: {switch_source}")
    switch_payload = read_text_strict(switch_source)
    switch_payload = switch_payload.replace("__PACKAGE_DIR__", layout.share_python).replace(
        "__PYTHON__", ctx.python
    )
    if not is_noop(layout.switch_entry, switch_payload, mode=0o755):
        write_atomic(layout.switch_entry, switch_payload, mode=0o755)


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
        # The same absolute path the plist hands the runner: only the installed
        # binary qualifies, so ``mihomo -t`` can never run a different file
        # than the start.
        binary = context_mod.resolve_runtime_binary(ctx.layout)
        if not binary:
            raise FailClosed(preflight_mod.MIHOMO_MISSING_REASON)
        # Fail closed *before* mihomo runs: a missing geo database makes mihomo
        # fetch it from GitHub, which hangs an offline install.
        config_mod.ensure_geodata(text, ctx.layout.mihomo_home)
        with secure_tempdir(ctx.layout.staging_dir, "validate") as staging:
            candidate = os.path.join(staging, "config.yaml")
            write_atomic(candidate, text, mode=0o600, mkdir=False)
            config_mod.validate(
                binary, candidate, ctx.layout.mihomo_home, ctx.executor
            )

    return validate


def configure(
    ctx: Context,
    *,
    quiet: bool = False,
    mode: str | None = None,
    provider_nodes: list[str] | None = None,
    secrets: Secrets | None = None,
) -> Report:
    """Render, audit and atomically install ``config.yaml``.  Repeatable.

    ``secrets`` lets ``install`` reuse the values it just wrote: re-reading the
    0600 file immediately after writing it would re-run the root-ownership check
    against a file whose group is only fixed a moment later.
    """
    report = Report(command="configure")
    layout = ctx.layout
    # Fail closed before any read/write: without a usable installed binary
    # there is nothing the runner could execute, and an install-source
    # override must not be validated in its place (install re-runs configure
    # after the copy, when the installed path exists).
    if not context_mod.resolve_runtime_binary(layout):
        raise FailClosed(preflight_mod.MIHOMO_MISSING_REASON)
    if secrets is None:
        secrets = ctx.secrets()
    state = load_state(layout) or DeploymentState()
    target_mode = mode or state.mode or "tun"

    nodes = provider_nodes if provider_nodes is not None else _read_provider_nodes(ctx)
    if not nodes:
        raise FailClosed(
            "provider node list is empty; cannot validate the CURSOR/CODEBUDDY policy"
        )
    supplement_path = os.path.join(layout.providers, "nextin.yaml")
    supplement_nodes = []
    if os.path.isfile(supplement_path):
        supplement_nodes = config_mod.provider_node_names(read_text_strict(supplement_path))
        if not supplement_nodes:
            raise FailClosed("the local source 1 supplement contains no nodes")
    verdict = policy.enforce(nodes + supplement_nodes)
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
        source1_supplement_path=supplement_path if supplement_nodes else None,
        valid_nodes=os.path.isfile(layout.valid_provider_file),
    )

    validator = ctx.validator or _default_validator(ctx)
    validator(rendered)

    if is_noop(layout.config, rendered, mode=0o600):
        check_output_mode(layout.config)
        report.status = "UNCHANGED"
    else:
        write_atomic(layout.config, rendered, mode=0o600)
        report.status = "CHANGED"
    _own(ctx, layout.config)
    if target_mode == "tun":
        if not is_noop(layout.config_tun_backup, rendered, mode=0o600):
            write_atomic(layout.config_tun_backup, rendered, mode=0o600)
        _own(ctx, layout.config_tun_backup)

    state.mode = target_mode
    state.degraded = target_mode == "mixed"
    state.tun_ok = target_mode == "tun"
    if target_mode == "tun":
        state.reasons = []
        state.services = []
    _save_state(ctx, state)

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
        # the runner passes ``mihomo -d <home>``: the same directory
        # ``mihomo -t`` validates against, where geo data is installed and
        # where the rendered provider path (providers/subscription.yaml) lives
        home_dir=target(layout.mihomo_home),
        config=target(layout.config),
        binary=target(layout.binary),
        run_dir=target(layout.run_dir),
        log_out=target(layout.log_out),
        log_err=target(layout.log_err),
    )
    plist_mod.audit_text(rendered, label=LABEL, home_dir=target(layout.mihomo_home))
    if is_noop(layout.plist, rendered, mode=0o644):
        return False
    write_atomic(layout.plist, rendered, mode=0o644)
    return True


def _install_boot_plist(ctx: Context) -> bool:
    """Render the boot entry: same render+audit gate as the core plist.

    The audit is the reason this cannot be hand-written: it rejects a
    ``KeepAlive`` of any kind (a failing switch must not be retried every few
    seconds), a shell, ``caffeinate`` and any secret/URL in the document.
    """
    layout = ctx.layout
    target = layout.to_target_path
    rendered = plist_mod.render_from_file(
        layout.boot_plist_template,
        label=BOOT_LABEL,
        python=target(ctx.python),
        package_dir=target(layout.share_python),
        home_dir=target(layout.mihomo_home),
        config=target(layout.config),
        binary=target(layout.binary),
        run_dir=target(layout.run_dir),
        log_out=target(layout.log_boot_out),
        log_err=target(layout.log_boot_err),
    )
    plist_mod.audit_text(rendered, label=BOOT_LABEL, home_dir=target(layout.mihomo_home))
    if is_noop(layout.boot_plist, rendered, mode=0o644):
        return False
    write_atomic(layout.boot_plist, rendered, mode=0o644)
    return True


def _boot_launchd(ctx: Context) -> Launchd:
    """The boot-switch job: the one LaunchDaemon that is enabled at boot."""
    return Launchd(
        ctx.executor,
        plist=ctx.layout.boot_plist,
        label=BOOT_LABEL,
        sleeper=ctx.sleeper,
    )


def _install_feishu_sync_plist(ctx: Context) -> bool:
    """Render the Feishu sync job: same render+audit gate as the other plists.

    The audit rejects a restart policy and a boot run: the job is purely
    ``StartInterval``-driven and a failing tick waits for the next interval.
    """
    layout = ctx.layout
    target = layout.to_target_path
    rendered = plist_mod.render_from_file(
        layout.feishu_sync_plist_template,
        label=FEISHU_SYNC_LABEL,
        python=target(ctx.python),
        package_dir=target(layout.share_python),
        home_dir=target(layout.mihomo_home),
        config=target(layout.config),
        binary=target(layout.binary),
        run_dir=target(layout.run_dir),
        log_out=target(layout.log_feishu_out),
        log_err=target(layout.log_feishu_err),
        sync_interval=str(feishu_sync_mod.sync_interval_seconds(ctx.environ)),
    )
    plist_mod.audit_text(rendered, label=FEISHU_SYNC_LABEL, home_dir=target(layout.mihomo_home))
    if is_noop(layout.feishu_sync_plist, rendered, mode=0o644):
        return False
    write_atomic(layout.feishu_sync_plist, rendered, mode=0o644)
    return True


def _feishu_sync_launchd(ctx: Context) -> Launchd:
    """The Feishu speedtest sync job (StartInterval-driven, never at boot)."""
    return Launchd(
        ctx.executor,
        plist=ctx.layout.feishu_sync_plist,
        label=FEISHU_SYNC_LABEL,
        sleeper=ctx.sleeper,
    )


def _install_node_pipeline_plist(ctx: Context) -> bool:
    """Render the node pipeline job: same render+audit gate as the core plist.

    The audit (interval-job rules) rejects a boot run, a restart policy and
    any secret/URL in the document.
    """
    layout = ctx.layout
    target = layout.to_target_path
    rendered = plist_mod.render_from_file(
        layout.node_pipeline_plist_template,
        label=NODE_PIPELINE_LABEL,
        python=target(ctx.python),
        package_dir=target(layout.share_python),
        home_dir=target(layout.mihomo_home),
        config=target(layout.config),
        binary=target(layout.binary),
        run_dir=target(layout.run_dir),
        log_out=target(layout.log_node_pipeline_out),
        log_err=target(layout.log_node_pipeline_err),
        sync_interval=str(node_pipeline_mod.pipeline_interval_seconds(ctx.environ)),
    )
    plist_mod.audit_text(rendered, label=NODE_PIPELINE_LABEL, home_dir=target(layout.mihomo_home))
    if is_noop(layout.node_pipeline_plist, rendered, mode=0o644):
        return False
    write_atomic(layout.node_pipeline_plist, rendered, mode=0o644)
    return True


def _node_pipeline_launchd(ctx: Context) -> Launchd:
    """The node pipeline job (StartInterval-driven, never at boot)."""
    return Launchd(
        ctx.executor,
        plist=ctx.layout.node_pipeline_plist,
        label=NODE_PIPELINE_LABEL,
        sleeper=ctx.sleeper,
    )


def _install_node_pipeline_job(ctx: Context, report: Report) -> None:
    """Register the node pipeline like the feishu job: enable + bootstrap.

    No hold stamp is needed (no RunAtLoad, the first tick fires one interval
    after the load).  Missing Feishu credentials do NOT fail the install: the
    tick reports them and retries on the next interval.
    """
    job = _node_pipeline_launchd(ctx)
    if _install_node_pipeline_plist(ctx):
        report.add("node-pipeline LaunchDaemon plist installed")
    else:
        report.add("node-pipeline LaunchDaemon plist unchanged")
    if job.state().loaded:
        job.bootout()
    job.enable()
    if job.is_disabled():
        raise FailClosed(
            "install finished but the node-pipeline LaunchDaemon is still disabled; "
            "the valid-node assignment would never run"
        )
    job.bootstrap()
    report.add(
        "node-pipeline registered: collect -> probe/gate -> valid-node provider -> "
        f"stable GPT switch every {node_pipeline_mod.pipeline_interval_seconds(ctx.environ)}s "
        "(MPM_NODE_PIPELINE_INTERVAL_SECONDS), first run one interval from now"
    )


def _xigua_ippool_launchd(ctx: Context) -> Launchd:
    return Launchd(
        ctx.executor, plist=ctx.layout.xigua_ippool_plist,
        label=XIGUA_IPPOOL_LABEL, sleeper=ctx.sleeper,
    )


def _install_xigua_ippool_job(ctx: Context, report: Report) -> None:
    """Schedule the two-stage pool only when an explicit source is installed."""
    import plistlib

    layout = ctx.layout
    path = layout.xigua_ippool_plist
    source = os.path.join(layout.providers, "xigua-source.yaml")
    job = _xigua_ippool_launchd(ctx)
    if not os.path.isfile(source):
        if os.path.isfile(path):
            job.bootout()
            job.disable()
            remove_if_present(path)
        return
    check_secret_input(source, require_root_owned=ctx.system_profile)
    target = layout.to_target_path
    argv = [
        target(ctx.python), target(os.path.join(layout.scripts_dir, "run-xigua-ippool.py")),
        "--source-provider", target(source),
        "--runtime-root", target(os.path.join(layout.state_dir, "xigua-ippool")),
        "--final-provider", target(os.path.join(layout.providers, "xigua-ippool.yaml")),
        "--core", target(layout.binary),
        "--target", "https://www.gstatic.com/generate_204",
        "--target", "https://chatgpt.com/cdn-cgi/trace",
        "--env-file", target(layout.secret_file),
        "--require-project-core",
    ]
    document = {
        "Label": XIGUA_IPPOOL_LABEL,
        "ProgramArguments": argv,
        "StartInterval": 300,
        "ProcessType": "Background",
        "WorkingDirectory": target(layout.mihomo_home),
        "StandardOutPath": target(os.path.join(layout.logs, "xigua-ippool.out.log")),
        "StandardErrorPath": target(os.path.join(layout.logs, "xigua-ippool.err.log")),
    }
    rendered = plistlib.dumps(document).decode("utf-8")
    if not is_noop(path, rendered, mode=0o644):
        job.bootout()
        write_atomic(path, rendered, mode=0o644)
    job.enable()
    job.bootstrap()
    report.add("xigua-ippool registered: runs every 300s")


# ---- boot-switch hold stamp -----------------------------------------------


#: How long ``install`` waits for the boot entry to acknowledge the hold
#: stamp (job loaded + 0600 ack file + entry no longer running).
#: ``MPM_BOOT_ACK_DEADLINE_SECONDS`` overrides it.
BOOT_ACK_DEADLINE_ENV = "MPM_BOOT_ACK_DEADLINE_SECONDS"
BOOT_ACK_DEADLINE_DEFAULT = 30.0
BOOT_ACK_DEADLINE_RANGE = (5.0, 300.0)
BOOT_ACK_POLL_INTERVAL = 0.5


def boot_ack_deadline_seconds(environ: dict[str, str] | None = None) -> float:
    """Configured hold-ack wait budget, clamped to a sane range."""
    values = environ if environ is not None else dict(os.environ)
    raw = (values.get(BOOT_ACK_DEADLINE_ENV) or "").strip()
    if not raw:
        return BOOT_ACK_DEADLINE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return BOOT_ACK_DEADLINE_DEFAULT
    low, high = BOOT_ACK_DEADLINE_RANGE
    return min(high, max(low, value))


def _ensure_boot_hold(ctx: Context) -> None:
    """Make sure the 0600 hold stamp exists - create it, or take one over.

    ``RunAtLoad`` makes ``launchctl bootstrap`` run the boot entry at once;
    while this stamp exists the entry exits with zero side effects, which is
    what makes registering the job during ``install --no-start`` safe.

    A stamp kept by an earlier failed install (an unconfirmed bootout) must
    never be deleted here, not even to recreate it at once: the old job may
    still be loaded and its RunAtLoad entry may reach the stamp check inside
    that delete-then-recreate window, and a run that finds no stamp performs
    the whole network switch (Codex review boot5).  So the stamp is created
    with ``O_EXCL`` when absent and simply taken over when present.
    """
    try:
        descriptor = os.open(
            ctx.layout.boot_hold, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        return
    os.close(descriptor)
    _own(ctx, ctx.layout.boot_hold)


def _remove_boot_hold(ctx: Context) -> None:
    try:
        os.unlink(ctx.layout.boot_hold)
    except FileNotFoundError:
        pass


def _remove_boot_ack(ctx: Context) -> None:
    """Drop the ack file: a stale one must never satisfy this install's wait."""
    try:
        os.unlink(ctx.layout.boot_hold_ack)
    except FileNotFoundError:
        pass


def _boot_acknowledged(ctx: Context, boot: Launchd) -> bool:
    """Whether the boot entry has fully acknowledged the hold stamp.

    All three must hold: the job is loaded, the RunAtLoad process is no
    longer running, and the 0600 ack file the entry wrote on its hold exit
    exists.  "Loaded" alone is not enough - the process may not have started
    yet, and deleting the stamp then would let that run perform the switch.
    """
    state = boot.state()
    return bool(state.loaded and not state.running) and os.path.isfile(
        ctx.layout.boot_hold_ack
    )


def _wait_boot_acknowledged(ctx: Context, boot: Launchd, *, deadline: float) -> bool:
    """Poll until the boot entry acknowledged the hold stamp or time runs out."""
    limit = ctx.clock() + max(0.0, float(deadline))
    while True:
        try:
            if _boot_acknowledged(ctx, boot):
                return True
        except Exception:  # noqa: BLE001 - an unreadable launchd state is "not yet"
            pass
        remaining = limit - ctx.clock()
        if remaining <= 0:
            return False
        ctx.sleeper(min(BOOT_ACK_POLL_INTERVAL, remaining))


def _confirmed_bootout(boot: Launchd) -> tuple[bool, str]:
    """Boot the boot-switch job out and confirm the unload by reading back.

    Returns ``(unloaded, reason)``.  ``unloaded`` is True only when the
    bootout succeeded *and* a follow-up ``launchctl print`` reports the job
    no longer loaded ("service not found").  A bootout exit code alone is
    not proof (Codex review boot4), and neither is a print that cannot be
    read at all (Codex review boot5): the caller must keep the hold stamp
    whenever ``unloaded`` is False - a RunAtLoad entry may still be starting.
    """
    try:
        boot.bootout()
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        return False, _failure_reason(exc)
    last: Exception | None = None
    for _ in range(boot.attempts):
        try:
            if not boot.state().loaded:
                return True, ""
        except Exception as exc:  # noqa: BLE001 - an unreadable state is no proof
            last = exc
        boot.sleeper(boot.interval)
    if last is not None:
        return False, "the unload could not be confirmed: " + _failure_reason(last)
    return False, (
        "bootout returned but the job still reports loaded after every read-back"
    )


# ---- install --------------------------------------------------------------


_MIHOMO_SOURCE_MISSING = (
    "mihomo binary not found: set MPM_MIHOMO_BINARY or put mihomo on PATH "
    "(this installer never downloads)"
)


def _install_binary(ctx: Context) -> bool:
    """Copy a user-supplied mihomo into ``libexec`` (this installer never downloads).

    The install source is resolved separately from the runtime capability
    (``context.resolve_install_source``): ``MPM_MIHOMO_BINARY`` selects it
    explicitly, otherwise ``mihomo`` on the context's ``PATH`` is used; it must
    be an absolute regular executable file.  Geo databases found next to the
    source are copied into the **mihomo home** (``layout.support``), because
    that is the ``-d`` directory used by both ``mihomo -t``
    (``config.validate``) and the resident runner (``mihomo -d``), and the
    rendered provider path is below it too.
    """
    layout = ctx.layout
    source = context_mod.resolve_install_source(ctx.environ)
    if not source:
        if context_mod.is_usable_binary(layout.binary):
            return False
        override = (ctx.environ.get(context_mod.BINARY_ENV) or "").strip()
        if override:
            raise FailClosed(
                "mihomo binary is not a regular executable file: "
                + os.path.abspath(override)
            )
        if os.path.isfile(layout.binary):
            raise FailClosed(
                "mihomo binary is not a regular executable file: " + layout.binary
            )
        raise FailClosed(_MIHOMO_SOURCE_MISSING)

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
    for name in _GEO_DATA_FILES:
        candidate = os.path.join(source_dir, name)
        target = os.path.join(layout.mihomo_home, name)
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
    # install supplies the binary itself (override or PATH source), so the
    # platform preflight must not require the runtime mihomo to be installed
    # already - a first install from PATH only would otherwise be refused
    # before the copy.  Every other capability check stays strict.
    check = preflight_mod.preflight(ctx.facts, require_mihomo=False)
    for warning in check.warnings:
        report.warn(warning)
    # No runtime binary and no install source: fail before any artifact is
    # written (the copy re-checks the source at install time).
    if not context_mod.resolve_runtime_binary(layout) and not context_mod.resolve_install_source(
        ctx.environ
    ):
        raise FailClosed(_MIHOMO_SOURCE_MISSING)
    _prepare_dirs(ctx)
    # A previous root install may have left root:admin artifacts behind (BSD
    # group inheritance); repair them before the ownership check reads them.
    _own(
        ctx,
        layout.secret_file,
        layout.deployment_state,
        layout.network_backup,
        layout.config,
        layout.config_tun_backup,
    )
    secrets = ctx.secrets()
    # Always rewrite the 0600 secret file: save_secrets preserves the optional
    # FEISHU_* keys and refreshes the derived SUBSCRIPTION_URL_1 mirror, so a
    # re-install never loses credentials nor drifts the mirror away from the
    # URL it mirrors.
    save_secrets(layout, secrets)
    _own(ctx, layout.secret_file)

    changed = _install_static(ctx)
    report.data["installed_files"] = len(changed)
    if _install_binary(ctx):
        report.add("mihomo binary installed")
    # The facts were built before the copy existed (a PATH find is never a
    # runtime capability).  Re-register now so configure's ``mihomo -t`` and
    # start's preflight see the installed absolute path - never the source.
    ctx.facts = context_mod.register_binary(ctx.facts, layout)

    nodes = _ensure_provider(ctx, secrets)
    report.data["provider_nodes"] = len(nodes)
    _own(ctx, layout.provider_file)

    config_report = configure(ctx, quiet=quiet, provider_nodes=nodes, secrets=secrets)
    report.data["configure"] = config_report.status
    report.warnings.extend(config_report.warnings)

    if _install_plist(ctx):
        report.add("LaunchDaemon plist installed")
    else:
        report.add("LaunchDaemon plist unchanged")
    if _install_boot_plist(ctx):
        report.add("boot-switch LaunchDaemon plist installed")
    else:
        report.add("boot-switch LaunchDaemon plist unchanged")

    # KeepAlive{SuccessfulExit:false} implies RunAtLoad, so the plist alone
    # would start mihomo at every boot - exactly the 2026-11-14 outage.  The
    # persistent launchd override is the only reliable "do not auto-start".
    launchd = ctx.launchd()
    launchd.disable()
    if not launchd.is_disabled():
        raise FailClosed(
            "install finished but the LaunchDaemon is not disabled; refusing to "
            "leave a service that would start at boot"
        )
    report.add("LaunchDaemon disabled: it does not start at boot (use mpm-switch mihomo)")

    # The boot-switch job is the *only* boot entry.  It is enabled (never
    # disabled): boot must reach mihomo without a password dialog.  It is also
    # registered (bootstrapped) in the *current* launchd domain, so
    # ``launchctl kickstart system/<BOOT_LABEL>`` works right after the install
    # (the non-reboot acceptance run) and a re-install replaces an already
    # loaded definition instead of leaving launchd on the stale one.
    #
    # ``bootstrap`` with RunAtLoad=true would run the boot entry immediately -
    # and that entry performs the whole network switch.  The 0600 hold stamp
    # closes that hole, so the registration is safe for ``install --no-start``
    # too.  The order matters and is tested:
    #   1. make sure the stamp exists: create it with ``O_EXCL`` when absent,
    #      take a stamp kept by an earlier failed install over when present -
    #      it is never deleted and recreated, because the old job's RunAtLoad
    #      entry may reach the stamp check inside that hold-less window and
    #      perform the whole switch (Codex review boot5, 2026-11-15) - and
    #      drop a stale ack (an interrupted earlier install's ack must never
    #      satisfy this install's wait; only the hold protects, so removing
    #      an ack cannot unprotect anything);
    #   2. bootout a job still registered from an older definition and wait
    #      for the unload to be confirmed by ``launchctl print``; the stamp
    #      is in place for the whole time, so the old entry cannot switch;
    #      after the confirmed unload the ack is dropped *again* - the old
    #      entry may have written one (under the stamp's protection) between
    #      the removal in (1) and its bootout, and a stale ack must not
    #      satisfy the new definition's wait (Codex review boot6,
    #      2026-11-15);
    #   3. enable + bootstrap the new definition (RunAtLoad starts the entry);
    #   4. wait for the full acknowledgement: job loaded, the entry process no
    #      longer running, and the ack file present.  ``loaded`` alone is not
    #      enough: RunAtLoad may not have started the process yet, and
    #      removing the stamp in that window would let that run perform the
    #      whole switch while ``install --no-start`` is still returning
    #      (Codex review boot3, 2026-11-15);
    #   5. on timeout: fail closed - bootout the job, and only when the
    #      bootout succeeded *and* ``launchctl print`` confirms the job is no
    #      longer loaded ("service not found") remove the stamp and the ack;
    #      if the bootout fails, the job still reports loaded, or the print
    #      cannot be read at all, a RunAtLoad entry may still be starting, so
    #      the stamp (and the ack) are KEPT on disk - that entry then reads
    #      the hold and exits with zero side effects instead of switching the
    #      network behind a failed install (Codex reviews boot4 and boot5,
    #      2026-11-15);
    #   6. on success the hold and the ack are removed - the *next* entry run
    #      (a real boot or a manual kickstart) performs the switch again.
    # The stamp is only ever removed at the two proven-safe points (4) and
    # (5): every other failure path keeps it (fail closed).  The ack is
    # dropped at (1), after the confirmed unload in (2) - a stale one must
    # never satisfy the wait of (4) - and at the same two proven-safe points.
    # Install itself never kickstarts and never executes the switch.
    boot = _boot_launchd(ctx)
    _ensure_boot_hold(ctx)
    _remove_boot_ack(ctx)
    if boot.state().loaded:
        # A re-install: the old definition may still be loaded and its
        # RunAtLoad entry still starting.  Boot it out and wait for the
        # confirmed unload under the stamp's protection before the new
        # definition is registered (Codex review boot5, 2026-11-15).
        unloaded, reason = _confirmed_bootout(boot)
        if not unloaded:
            raise FailClosed(
                "a previous boot-switch job is still loaded and its unload is "
                "not confirmed (" + reason + "); the hold stamp and the ack "
                "are KEPT on disk so the still-starting entry reads the hold "
                "and exits without switching the network - delete them only "
                "after `launchctl print system/" + BOOT_LABEL + "` confirms "
                "the job is unregistered (not loaded / service not found)"
            )
        # The old job is confirmed unloaded, so its RunAtLoad entry is dead -
        # but it may have written an ack (under the hold's protection) in the
        # window between the removal above and its bootout.  That stale ack
        # must not survive into the new definition's wait: the wait would read
        # "loaded, not running, ack present" while the new entry has not even
        # spawned yet, remove the hold, and reopen the unprotected switch
        # window (Codex review boot6, 2026-11-15).  The hold stamp itself
        # stays in place.
        _remove_boot_ack(ctx)
    boot.enable()
    if boot.is_disabled():
        raise FailClosed(
            "install finished but the boot-switch LaunchDaemon is still disabled; "
            "the machine would boot with neither mihomo nor the boot entry"
        )
    boot.bootstrap()
    deadline = boot_ack_deadline_seconds(ctx.environ)
    if not _wait_boot_acknowledged(ctx, boot, deadline=deadline):
        detail = (
            "the boot-switch entry did not acknowledge the hold stamp within "
            f"{deadline:g}s (loaded, but no ack or still running); failing "
            "closed so no entry run can switch the network behind this install"
        )
        unloaded, reason = _confirmed_bootout(boot)
        if not unloaded:
            # A RunAtLoad entry may still be starting and has not read the
            # hold stamp yet: removing the stamp now would let that run
            # perform the whole switch.  Keep the stamp (and the ack) so
            # the entry exits without side effects once it reaches the
            # stamp check.
            raise FailClosed(
                detail
                + "; " + reason
                + "; the hold stamp and the ack are KEPT on disk so a "
                "still-starting entry reads the hold and exits without "
                "switching the network - delete them only after "
                f"`launchctl print system/{BOOT_LABEL}` confirms the job "
                "is unregistered (not loaded / service not found)"
            )
        _remove_boot_hold(ctx)
        _remove_boot_ack(ctx)
        raise FailClosed(
            detail
            + "; the job was booted out, so the hold stamp and the ack were "
            "removed and no entry run can start"
        )
    _remove_boot_hold(ctx)
    _remove_boot_ack(ctx)
    report.add(
        "boot-switch registered (loaded, not running, hold acknowledged): the "
        "switch runs at boot or via `launchctl kickstart system/" + BOOT_LABEL + "`"
    )

    # The Feishu speedtest sync job: enabled + bootstrapped like the boot entry
    # (it must survive a re-install too, so a still-loaded old definition is
    # booted out first).  No hold stamp is needed: without RunAtLoad the
    # bootstrap cannot trigger a run, and the first tick fires one interval
    # after the load.  Missing Feishu credentials do NOT fail the install -
    # the tick reports them until the operator copies them over.
    feishu = _feishu_sync_launchd(ctx)
    if _install_feishu_sync_plist(ctx):
        report.add("feishu-sync LaunchDaemon plist installed")
    else:
        report.add("feishu-sync LaunchDaemon plist unchanged")
    if feishu.state().loaded:
        feishu.bootout()
    feishu.enable()
    if feishu.is_disabled():
        raise FailClosed(
            "install finished but the feishu-sync LaunchDaemon is still disabled; "
            "the speedtest sync would never run"
        )
    feishu.bootstrap()
    report.add(
        "feishu-sync registered: the speedtest sync fires every "
        f"{feishu_sync_mod.sync_interval_seconds(ctx.environ)}s"
        " (MPM_FEISHU_SYNC_INTERVAL_SECONDS), first run one interval from now"
    )
    _install_xigua_ippool_job(ctx, report)
    _install_node_pipeline_job(ctx, report)

    if skip_start:
        report.status = "OK"
        report.add("install complete; service not started (--no-start)")
        return report

    start_report = start(ctx, quiet=True)
    report.status = start_report.status
    report.data["start"] = start_report.status
    report.messages.extend(start_report.messages)
    report.warnings.extend(start_report.warnings)
    if start_report.status == "FAILED":
        report.exit_code = start_report.exit_code
        report.warn("install finished but the core could not be started")
    elif not quiet:
        report.add("installed; management CLI at " + layout.cli_entry)
    return report


# ---- start / stop / restart ----------------------------------------------


@dataclass
class StopOutcome:
    """What a stop actually achieved; only ``safe`` allows Nextin.

    ``disabled``  - the ``launchctl disable`` command returned success;
    ``disabled_confirmed`` - ``launchctl print-disabled`` read the override back;
    ``stopped``   - the job is confirmed not running;
    ``orphan_free`` - no listener owned by *this project* on 7890/9090 and no
                      utun still carrying a 198.19/16 address while a project
                      core process runs (foreign owners such as Nextin's
                      ``ProxyTunnel`` do not count);
    ``warnings``  - non-fatal observations (e.g. an unidentifiable leftover
                    that a fully confirmed stop allows past).
    An unknown answer (command failure) is a failure, never a confirmation.
    """

    bootout: bool = False
    disabled: bool = False
    disabled_confirmed: bool = False
    stopped: bool = False
    orphan_free: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        """True only when the job cannot come back at boot *and* is not running."""
        return self.disabled and self.disabled_confirmed and self.stopped and self.orphan_free

    def to_dict(self) -> dict[str, Any]:
        return {
            "bootout": self.bootout,
            "disabled": self.disabled,
            "disabled_confirmed": self.disabled_confirmed,
            "stopped": self.stopped,
            "orphan_free": self.orphan_free,
            "safe": self.safe,
            "errors": [HOST.text(item) for item in self.errors],
            "warnings": [HOST.text(item) for item in self.warnings],
        }


def _orphan_core_problems(
    ctx: Context, *, confirmed_stopped: bool
) -> tuple[list[str], list[str]]:
    """Evidence that *this project's* mihomo still owns routing after a stop.

    Only resources confirmed to belong to the project count: a listener on
    7890/9090 must be the launchd PID or run the fixed executable path
    (``conflict.port_owner_identity``) - Nextin's ``ProxyTunnel`` and any
    other software may own the ports legitimately.  A 198.19/16 utun counts
    only while a project core process is alive (``pgrep -f`` of the fixed
    executable path, or the launchd PID).  When identity cannot be
    determined, a fully confirmed stop (launchd stopped, disable read back
    true, no project executable running) still allows the rollback with a
    warning: the machine must never be left offline by an unattributable
    leftover.  A failed or unreadable check is a problem (unknown is never
    "clean").

    Returns ``(problems, warnings)``; only ``problems`` block the rollback.
    """
    problems: list[str] = []
    warnings: list[str] = []
    job_pid = conflict_mod.launchd_pid(ctx.executor)
    core_state: str | None = None  # computed lazily, at most once

    def core_state_once() -> str:
        nonlocal core_state
        if core_state is None:
            core_state = conflict_mod.project_core_state(ctx.executor, job_pid=job_pid)
        return core_state

    for port in conflict_mod.PROJECT_PORTS:
        try:
            result = ctx.executor.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"]
            )
        except Exception as exc:  # noqa: BLE001 - unknown is not clean
            problems.append(f"port {port}: lsof failed: {_failure_reason(exc)}")
            continue
        if result.returncode not in (0, 1):
            # lsof exits 1 when nothing listens: that is "no owner".
            problems.append(f"port {port}: lsof failed (rc={result.returncode})")
            continue
        for owner in conflict_mod.parse_lsof_listeners(result.stdout or "", port):
            identity = conflict_mod.port_owner_identity(
                ctx.executor, owner, job_pid=job_pid
            )
            if identity == "project":
                problems.append(
                    f"port {port} is still listening after stop "
                    f"(pid {owner.pid} runs the project mihomo)"
                )
            elif identity == "unknown":
                if confirmed_stopped and core_state_once() == "stopped":
                    warnings.append(
                        f"port {port} is held by pid {owner.pid}, which cannot be "
                        "identified; mihomo is confirmed stopped and disabled, so it "
                        "is treated as foreign and Nextin may start"
                    )
                else:
                    problems.append(
                        f"port {port} is held by pid {owner.pid} and the process "
                        "identity cannot be determined"
                    )
            # "foreign": another program owns the port, not our leftover.
    try:
        result = ctx.executor.run(["ifconfig"])
    except Exception as exc:  # noqa: BLE001 - unknown is not clean
        problems.append("interfaces unreadable: " + _failure_reason(exc))
        return problems, warnings
    if result.returncode != 0:
        problems.append(f"ifconfig failed (rc={result.returncode})")
        return problems, warnings
    for name, address in conflict_mod.parse_utun_addresses(result.stdout or ""):
        if not conflict_mod.in_project_block(address):
            continue
        state = core_state_once()
        if state == "running":
            problems.append(
                f"utun {name} still holds project address {address} (198.19/16) "
                "and the project core is running"
            )
        elif state == "stopped":
            warnings.append(
                f"utun {name} holds a 198.19/16 address but no project core "
                "process is running; it is not treated as an orphan core"
            )
        else:
            problems.append(
                f"utun {name} holds project address {address} (198.19/16) and the "
                "project core state is unknown"
            )
    return problems, warnings


def _raise_unsafe(outcome: StopOutcome) -> NoReturn:
    failure = NotReady("; ".join(outcome.errors) or "could not confirm the core stopped")
    failure.stop_outcome = outcome  # type: ignore[attr-defined]
    raise failure


def _stop_core(ctx: Context) -> StopOutcome:
    """bootout + persistent disable: a stopped core never comes back at boot.

    ``disable`` runs even when ``bootout`` fails (``finally``): leaving the job
    enabled while something else takes over the routes is the 2026-11-14
    dual-stack state.  The disable override is read back via
    ``launchctl print-disabled`` and the port/utun checks only count
    resources that belong to *this project*.
    Every step is recorded separately, and the caller may only hand the
    machine to Nextin when :attr:`StopOutcome.safe` is true.
    """
    launchd = ctx.launchd()
    outcome = StopOutcome()
    try:
        launchd.bootout()
        outcome.bootout = True
    except Exception as exc:  # noqa: BLE001 - keep going: disable still matters
        outcome.errors.append("bootout: " + _failure_reason(exc))
    finally:
        try:
            launchd.disable()
            outcome.disabled = True
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            outcome.errors.append("disable: " + _failure_reason(exc))
    try:
        outcome.disabled_confirmed = launchd.is_disabled()
        if not outcome.disabled_confirmed:
            outcome.errors.append("disable override is not active")
    except Exception as exc:  # noqa: BLE001 - unreadable override is a failure
        outcome.errors.append("disable readback: " + _failure_reason(exc))
    try:
        outcome.stopped = launchd.wait_stopped()
    except Exception as exc:  # noqa: BLE001
        outcome.stopped = False
        outcome.errors.append("wait-stopped: " + _failure_reason(exc))
    if not outcome.stopped:
        outcome.errors.append("the core is still running after launchctl bootout")
    problems, warnings = _orphan_core_problems(
        ctx, confirmed_stopped=outcome.stopped and outcome.disabled_confirmed
    )
    outcome.orphan_free = not problems
    outcome.errors.extend(problems)
    outcome.warnings.extend(warnings)
    if not outcome.safe:
        _raise_unsafe(outcome)
    return outcome


def _stop_unloaded(ctx: Context) -> StopOutcome:
    """The same guarantees as :func:`_stop_core`, for a job that is not loaded.

    Even a stopped-but-not-loaded job must stay disabled (a stale ``enable``
    would let the next boot start it), the answer is read back, and the
    port/utun checks catch a *project* core that runs orphaned without
    launchd.
    """
    launchd = ctx.launchd()
    outcome = StopOutcome()
    try:
        launchd.disable()
        outcome.disabled = True
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        outcome.errors.append("disable: " + _failure_reason(exc))
    try:
        outcome.disabled_confirmed = launchd.is_disabled()
        if not outcome.disabled_confirmed:
            outcome.errors.append("disable override is not active")
    except Exception as exc:  # noqa: BLE001 - unreadable override is a failure
        outcome.errors.append("disable readback: " + _failure_reason(exc))
    try:
        outcome.stopped = launchd.wait_stopped()
    except Exception as exc:  # noqa: BLE001
        outcome.stopped = False
        outcome.errors.append("wait-stopped: " + _failure_reason(exc))
    problems, warnings = _orphan_core_problems(
        ctx, confirmed_stopped=outcome.stopped and outcome.disabled_confirmed
    )
    outcome.orphan_free = not problems
    outcome.errors.extend(problems)
    outcome.warnings.extend(warnings)
    if not outcome.safe:
        _raise_unsafe(outcome)
    return outcome


def _readiness(ctx: Context, secrets: Secrets) -> str:
    return readiness(ctx.controller(secrets.controller_secret), sleeper=ctx.sleeper)


#: Groups that must have at least one live node before a start counts as good.
#: GROKBOT needs no separate entry: its last fallback member is AUTO-FOREIGN,
#: so it is live whenever AUTO-FOREIGN is (an extra name here would only add a
#: failure mode, not coverage).
REQUIRED_HEALTHY_GROUPS = ("AUTO-FOREIGN", "GPT")

#: A url-test group reports ``alive: false`` until its first round of delay
#: tests finishes, which is *after* the provider verdict can already be OK.
#: Checking the groups exactly once raced that first test round and rolled a
#: healthy start back (2026-11-23).  Give them a short, deadline-bounded settle
#: window instead of failing on the first sample.
GROUP_SETTLE_SECONDS = 20.0
GROUP_SETTLE_POLL_SECONDS = 1.0


def _group_liveness_settled(
    client: ControllerClient,
    *,
    deadline_seconds: float,
    sleeper: Any,
    clock: Any,
) -> dict[str, bool | None]:
    """Poll the routing groups until they are all live or the window expires."""
    budget = max(0.0, float(deadline_seconds))
    deadline = clock() + budget
    while True:
        groups = _group_liveness(client)
        if all(value is True for value in groups.values()):
            return groups
        remaining = deadline - clock()
        if remaining <= 0:
            return groups
        sleeper(min(GROUP_SETTLE_POLL_SECONDS, remaining))


def _group_liveness(client: ControllerClient) -> dict[str, bool | None]:
    """``alive`` flag of the routing groups (``None`` when not reported)."""
    try:
        payload = client.proxies()
    except (ControllerError, FailClosed):
        return {name: None for name in REQUIRED_HEALTHY_GROUPS}
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        return {name: None for name in REQUIRED_HEALTHY_GROUPS}
    result: dict[str, bool | None] = {}
    for name in REQUIRED_HEALTHY_GROUPS:
        info = proxies.get(name)
        if isinstance(info, dict) and "alive" in info:
            result[name] = bool(info["alive"])
        else:
            result[name] = None
    return result


def _health_provider(ctx: Context) -> str:
    """Provider that feeds AUTO-FOREIGN, i.e. the one the start gate must trust.

    Assignment mode renders AUTO-FOREIGN over ``node-valid`` whenever that pool
    exists; gating on ``subscription`` then kept auto-start hostage to a stale
    source that is mostly dead (2026-11-23: 5 of 40 alive).
    """
    if os.path.isfile(ctx.layout.valid_provider_file):
        return config_mod.VALID_PROVIDER_NAME
    return config_mod.PROVIDER_NAME


def _health_check(
    ctx: Context, secrets: Secrets, *, mode: str, deadline: float | None = None
) -> HealthReport:
    """Provider health plus live routing groups (AUTO-FOREIGN and GPT).

    The provider check alone is not enough: a provider can have live nodes
    while both routing groups are empty, which is exactly how PROXY ended up
    stuck on a dead node.  A missing/unknown group verdict fails closed.

    The provider verdict is deadline-bounded (``evaluate_with_deadline``): the
    healthcheck request is only *triggered* with a short timeout and the
    verdict waits for ``alive_count >= 1`` instead of failing on a timeout.
    """
    client = ctx.controller(secrets.controller_secret)
    verdict = evaluate_with_deadline(
        client,
        provider=_health_provider(ctx),
        mode=mode,
        running=True,
        deadline_seconds=(
            health_deadline_seconds(ctx.environ) if deadline is None else float(deadline)
        ),
        sleeper=ctx.sleeper,
        clock=ctx.clock,
    )
    if not verdict.ok:
        return verdict
    budget = health_deadline_seconds(ctx.environ) if deadline is None else float(deadline)
    groups = _group_liveness_settled(
        client,
        deadline_seconds=min(GROUP_SETTLE_SECONDS, budget),
        sleeper=ctx.sleeper,
        clock=ctx.clock,
    )
    verdict.data["groups"] = {name: value for name, value in groups.items()}
    empty = [name for name, alive in groups.items() if not alive]
    if empty:
        reasons = list(verdict.reasons)
        for name in empty:
            state = "has no live node" if groups[name] is False else "liveness is unknown"
            reasons.append(f"group {name} {state}")
        return HealthReport(HEALTH_FAILED, reasons, verdict.data)
    return verdict


def _health_failure(verdict: HealthReport, *, mode: str) -> str:
    reasons = "; ".join(verdict.reasons) or "health check failed"
    return HOST.text(f"{mode} core is not healthy: {reasons}")[:200]


def _failure_reason(exc: BaseException) -> str:
    detail = exc.message if isinstance(exc, MpmError) else (str(exc) or type(exc).__name__)
    return HOST.text(detail)[:200]


def _apply_tun_dns(ctx: Context, report: Report) -> None:
    """Point the active service's DNS at the TUN gateway (best effort).

    mDNSResponder binds DNS sockets to the network service, so a DHCP resolver
    is queried outside the TUN and returns poisoned answers for blocked
    domains.  A failure here only degrades blocked-site access, so it is a
    warning rather than a reason to tear down a healthy core.
    """
    try:
        service = tun_dns_mod.apply(ctx.executor, ctx.layout.tun_dns_backup)
        report.add(
            f"system DNS on '{service}' pointed at the TUN gateway ({tun_dns_mod.TUN_DNS})"
        )
    except Exception as exc:  # noqa: BLE001 - the core itself is healthy
        report.warn("could not point system DNS at the TUN gateway: " + _failure_reason(exc))


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


def _require_no_conflict(ctx: Context) -> None:
    """Refuse to start a second network stack (Nextin etc.)."""
    verdict = ctx.conflicts()
    if not verdict.ok:
        raise FailClosed(conflict_mod.describe(verdict))


def _failed_start(
    ctx: Context, report: Report, reason: str, *, target_mode: str = "tun"
) -> Report:
    """Stop, restore the proxy snapshot, and report FAILED.

    This deliberately does **not** fall back to mixed mode: the 2026-11-14
    outage was made worse by silently taking over the system proxy.
    """
    report.status = "FAILED"
    report.exit_code = int(ExitCode.NOT_READY)
    report.warn(reason)
    report.add("start failed; mihomo was stopped (no mixed-mode/system-proxy fallback)")
    try:
        _stop_core(ctx)
    except Exception as exc:  # noqa: BLE001 - report the original failure first
        report.warn("stop after failure failed: " + _failure_reason(exc))
    try:
        restored = _restore_proxy_if_any(ctx)
        if restored:
            report.add("restored the system proxy on: " + ", ".join(restored))
    except Exception as exc:  # noqa: BLE001
        report.warn("proxy restore after failure failed: " + _failure_reason(exc))
    state = load_state(ctx.layout) or DeploymentState()
    if target_mode == "mixed":
        # The live config is mixed, but a failed mixed start must not make the
        # next plain ``start`` (TUN) reuse it.
        try:
            configure(ctx, quiet=True, mode="tun")
            state.mode = "tun"
        except Exception as exc:  # noqa: BLE001 - stopped is still safe
            report.warn("could not restore the TUN config after failure: " + _failure_reason(exc))
    else:
        state.mode = "tun"
    state.degraded = False
    state.tun_ok = False
    state.reasons = [HOST.text(reason)]
    state.services = []
    _save_state(ctx, state)
    return report


def _start_mixed(
    ctx: Context,
    report: Report,
    *,
    quiet: bool = False,
    health_guard: Callable[[], None] | None = None,
) -> Report:
    """Explicit mixed mode: the only path that touches the system proxy."""
    layout = ctx.layout
    proxy = ctx.proxy_client()
    launchd = ctx.launchd()
    modified: list[str] = []
    try:
        snapshot = proxy.snapshot()
        save_snapshot(layout.network_backup, snapshot)
        _own(ctx, layout.network_backup)

        launchd.enable()
        if launchd.state().loaded:
            launchd.kickstart()
        else:
            launchd.bootstrap()
        if not launchd.wait_running():
            raise NotReady("mixed-mode core did not reach running state")
        if health_guard is not None:
            # Runs before any controller call: an external VPN that came back
            # must roll back immediately, not after a slow health gate.
            health_guard()
        secrets = ctx.secrets()
        report.data["version"] = _readiness(ctx, secrets)
        verdict = _health_check(ctx, secrets, mode="mixed")
        report.data["health"] = verdict.to_dict()
        if not verdict.ok:
            raise NotReady(_health_failure(verdict, mode="mixed"))
        modified = proxy.apply(snapshot)
        snapshot.stage = STAGE_APPLIED
        save_snapshot(layout.network_backup, snapshot, overwrite=True)
        _own(ctx, layout.network_backup)
        # Same transaction rule as TUN: a job left enabled would auto-start at
        # the next boot and race the VPN.
        launchd.disable()
        if not launchd.is_disabled():
            raise NotReady(
                "the persistent disable override did not take effect after start"
            )
    except (MpmError, ControllerError, NetworkProxyError, OSError) as exc:
        # _failed_start stops the core and restores (then deletes) the snapshot
        return _failed_start(ctx, report, _failure_reason(exc), target_mode="mixed")

    state = load_state(layout) or DeploymentState()
    state.mode = "mixed"
    state.degraded = True
    state.tun_ok = False
    state.reasons = ["explicit mixed mode (configure --mode mixed)"]
    state.services = list(modified)
    _save_state(ctx, state)

    report.status = "DEGRADED"
    report.data["mode"] = "mixed"
    report.data["services"] = list(modified)
    ctx.shout(
        "mixed mode requested explicitly: 127.0.0.1:7890; system proxy set on: "
        + ", ".join(modified)
    )
    if not quiet:
        report.add("mixed mode active; `stop` restores the saved proxy settings")
    return report


def start(
    ctx: Context,
    *,
    quiet: bool = False,
    mode: str | None = None,
    health_guard: Callable[[], None] | None = None,
) -> Report:
    """Start the core.  TUN by default; mixed only when explicitly requested.

    ``health_guard`` runs after launchd reports the job running and *before*
    the controller readiness/health gate.  ``mpm-switch`` uses it to abort a
    start whose external VPN came back, so the rollback does not wait for a
    slow provider healthcheck first.
    """
    report = Report(command="start")
    layout = ctx.layout
    target_mode = mode or "tun"
    if target_mode not in ("tun", "mixed"):
        raise FailClosed(f"unknown start mode '{target_mode}' (expected tun or mixed)")

    check = preflight_mod.preflight(ctx.facts)
    for warning in check.warnings:
        report.warn(warning)

    # The facts are a snapshot from context construction: re-check the
    # installed binary itself so a stale mihomo capability can never bootstrap
    # the runner while the file the plist executes is missing/unusable.
    if not context_mod.resolve_runtime_binary(layout):
        raise FailClosed(preflight_mod.MIHOMO_MISSING_REASON)

    launchd = ctx.launchd()
    current = launchd.state()
    state = load_state(layout) or DeploymentState()

    if not os.path.isfile(layout.plist):
        raise FailClosed("LaunchDaemon plist is not installed; run install first")

    # Before any launchd action: a connected Nextin (or any foreign port /
    # default utun) means starting mihomo would split routes and DNS.  This is
    # checked even when the core is already running: that is exactly the
    # dual-stack state the 2026-11-14 outage left behind.
    _require_no_conflict(ctx)

    if current.running:
        # A boot-started core (or a previous start) must not come back at the
        # next boot either.  Same readback rule as the fresh paths below: an
        # override that cannot be confirmed is a failed start, and the safety
        # stop transaction runs instead of returning a false UNCHANGED.
        try:
            launchd.disable()
            if not launchd.is_disabled():
                raise NotReady(
                    "the persistent disable override did not take effect after start"
                )
        except (MpmError, ControllerError, NetworkProxyError, OSError) as exc:
            return _failed_start(ctx, report, _failure_reason(exc), target_mode=target_mode)
        if target_mode == "tun":
            # A running core can still have lost its TUN DNS (DHCP renew,
            # service switch): re-apply it so ``start`` stays idempotent.
            _apply_tun_dns(ctx, report)
        report.status = "UNCHANGED"
        if state.degraded and target_mode == "mixed":
            report.status = "DEGRADED"
            report.warn("; ".join(state.reasons) or "degraded mode recorded")
        if not quiet:
            report.add("core already running; no second instance started")
        return report

    # An unfinished proxy transaction (from an older mixed session) is
    # restored before a fresh attempt, so the host never starts half-applied.
    restored = _restore_proxy_if_any(ctx)
    if restored:
        report.add("restored an unfinished proxy transaction: " + ", ".join(restored))

    if not os.path.isfile(layout.config) or state.mode != target_mode:
        configure(ctx, quiet=True, mode=target_mode)

    if target_mode == "mixed":
        return _start_mixed(ctx, report, quiet=quiet, health_guard=health_guard)

    before = ctx.tun()
    after = before
    version = ""
    failure = ""
    try:
        launchd.enable()
        if current.loaded:
            launchd.kickstart()
        else:
            launchd.bootstrap()
        if not launchd.wait_running():
            raise NotReady("LaunchDaemon did not reach running state")

        if health_guard is not None:
            # An external VPN that reconnected while mihomo started must roll
            # back now - before the deadline-bounded health gate spends its
            # budget on a controller that is about to be torn down.
            health_guard()

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

        # The job stays loaded and running; only future loads (next boot) are
        # disabled.  A crash in this boot session is still restarted by
        # KeepAlive.  This is part of the transaction: a job left *enabled*
        # after a successful start would come back at the next boot, which is
        # the outage this project exists to prevent.  An unreadable override is
        # a failure, never a confirmation.
        launchd.disable()
        if not launchd.is_disabled():
            raise NotReady(
                "the persistent disable override did not take effect after start"
            )
    except (MpmError, ControllerError, NetworkProxyError, OSError) as exc:
        failure = _failure_reason(exc)

    if failure:
        return _failed_start(ctx, report, failure)

    # mDNSResponder binds DNS sockets to the network service, so a DHCP
    # resolver is queried outside the TUN and returns poisoned answers for
    # blocked domains.  The previous servers are snapshotted and restored by
    # ``stop``.
    _apply_tun_dns(ctx, report)

    state.mode = "tun"
    state.degraded = False
    state.tun_ok = True
    state.reasons = []
    state.services = []
    _save_state(ctx, state)
    report.data["mode"] = "tun"
    report.data["tun"] = after.to_dict()
    report.data["new_utun_interfaces"] = [
        name
        for name in after.interfaces
        if name not in before.interfaces and name.startswith("utun")
    ]
    if not quiet:
        report.add(f"active in TUN mode, controller ready (mihomo {version})")
        report.add("LaunchDaemon disabled: the next boot starts with Nextin, not mihomo")
    return report


def stop(ctx: Context, *, quiet: bool = False) -> Report:
    """Stop the core and confirm it: disable + readback + stopped + no orphan.

    Always returns a structured :class:`StopOutcome` in ``data["stop_outcome"]``;
    when the core is not confirmed stopped *and* disabled the report is
    ``FAILED`` and callers must not hand the machine to Nextin.
    """
    report = Report(command="stop")
    launchd = ctx.launchd()
    state = launchd.state()
    outcome: StopOutcome | None = None
    try:
        outcome = _stop_core(ctx) if state.loaded else _stop_unloaded(ctx)
    except NotReady as exc:
        attached = getattr(exc, "stop_outcome", None)
        if attached is None:
            raise
        outcome = attached
    assert outcome is not None  # narrow for the type checker
    report.data["stop_outcome"] = outcome.to_dict()
    report.warnings.extend(outcome.warnings)

    if not outcome.safe:
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        for error in outcome.errors:
            report.warn("stop not confirmed: " + error)
        report.warn(
            "mihomo is not confirmed stopped and disabled; do not connect Nextin yet"
        )
        report.warn(
            "resolve the leftover core (listeners on ports 7890/9090, a utun with a "
            "198.19/16 address), then run `mpm-switch nextin` again or handle it manually"
        )
        return report

    restored = _restore_proxy_if_any(ctx)
    if restored:
        report.add("system proxy restored on: " + ", ".join(restored))
    try:
        services = tun_dns_mod.restore(ctx.executor, ctx.layout.tun_dns_backup)
        if services:
            report.add("system DNS restored on: " + ", ".join(services))
    except Exception as exc:  # noqa: BLE001 - the core is already stopped safely
        report.warn("could not restore system DNS: " + _failure_reason(exc))
    deployment = load_state(ctx.layout)
    if deployment is not None:
        deployment.degraded = False
        deployment.reasons = []
        deployment.services = []
        _save_state(ctx, deployment)
    report.status = "OK" if state.loaded else "UNCHANGED"
    if not quiet:
        report.add("core stopped" if state.loaded else "core already stopped")
    return report


def restart(ctx: Context, *, quiet: bool = False) -> Report:
    report = Report(command="restart")
    stopped = stop(ctx, quiet=True)
    outcome = stopped.data.get("stop_outcome") or {}
    if stopped.status == "FAILED" or not outcome.get("safe"):
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        report.warn("stop did not confirm a stopped and disabled core; not restarting")
        report.warnings.extend(stopped.warnings)
        return report
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


def _node_pipeline_status(ctx: Context) -> dict[str, Any]:
    """Launchd state of the node pipeline job (read-only, never throws)."""
    try:
        job = _node_pipeline_launchd(ctx)
        state = job.state()
        disabled = job.is_disabled()
    except Exception as exc:  # noqa: BLE001 - degraded, not fatal
        return {"loaded": False, "running": False, "disabled": None, "error": _failure_reason(exc)}
    return {
        "loaded": state.loaded,
        "running": state.running,
        "disabled": disabled,
        "last_exit": state.last_exit,
    }


def _feishu_sync_status(ctx: Context) -> dict[str, Any]:
    """Launchd state of the Feishu speedtest sync job (read-only, never throws).

    ``status`` must keep working on an older install without the job and on a
    machine where ``launchctl`` refuses to talk to us - every failure degrades
    to a reported field instead of an exception.
    """
    try:
        job = _feishu_sync_launchd(ctx)
        state = job.state()
        disabled = job.is_disabled()
    except Exception as exc:  # noqa: BLE001 - degraded, not fatal
        return {"loaded": False, "running": False, "disabled": None, "error": _failure_reason(exc)}
    return {
        "loaded": state.loaded,
        "running": state.running,
        "disabled": disabled,
        "last_exit": state.last_exit,
    }


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
        "feishu_sync": _feishu_sync_status(ctx),
        "node_pipeline": _node_pipeline_status(ctx),
    }

    client: ControllerClient | None = None
    try:
        secrets = ctx.secrets()
        client = ctx.controller(secrets.controller_secret)
    except FailClosed as exc:
        report.warn(f"controller secret not readable: {HOST.text(str(exc))[:120]}")

    health = evaluate_with_deadline(
        client,
        provider=_health_provider(ctx),
        mode=state.mode,
        running=launchd_state.running,
        degraded_reasons=state.reasons if state.degraded else [],
        deadline_seconds=health_deadline_seconds(ctx.environ),
        sleeper=ctx.sleeper,
        clock=ctx.clock,
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
    verdict = evaluate_with_deadline(
        client,
        provider=_health_provider(ctx),
        mode=state.mode,
        running=launchd_state.running,
        degraded_reasons=state.reasons if state.degraded else [],
        deadline_seconds=health_deadline_seconds(ctx.environ),
        sleeper=ctx.sleeper,
        clock=ctx.clock,
    )
    report.status = verdict.state
    report.data = verdict.to_dict()
    report.data["mode"] = state.mode
    for reason in verdict.reasons:
        report.warn(reason)
    return report


# ---- groups diagnostics ---------------------------------------------------


def _group_line(row: dict[str, Any]) -> str:
    """One human-readable ``groups`` row (sanitising happens at the streams)."""
    parts = [str(row.get("name", "?")), str(row.get("type") or "?")]
    if row.get("matched_nodes") is not None:
        parts.append("matched=" + str(row["matched_nodes"]))
    if row.get("now") is not None:
        parts.append("now=" + str(row["now"]))
    if row.get("alive") is not None:
        parts.append("alive=" + ("yes" if row["alive"] else "no"))
    members = row.get("members_alive") or {}
    if members:
        rendered = ",".join(
            name + ("+" if alive else "-") if alive is not None else name + "?"
            for name, alive in members.items()
        )
        parts.append("members=" + rendered)
    return "  ".join(parts)


def groups(ctx: Context) -> Report:
    """Per-group routing report (read-only): who matches, who is alive, who wins.

    Answers the "why does everything exit Hong Kong / which other regions are
    usable" question without touching any state:

    * for every provider-backed filter group it counts the provider nodes the
      filter matches - **zero matches means mihomo silently degrades the group
      to COMPATIBLE (direct)**, which is exactly how a region pool disappears;
    * when the controller is reachable it also reports each group's alive
      members and current selection.

    The controller being down is a degraded report, not an error: the match
    counts come from the provider file alone.
    """
    report = Report(command="groups")
    layout = ctx.layout
    names = _read_provider_nodes(ctx)
    if not names:
        report.warn(
            "provider file has no nodes; run install/refresh first "
            f"({HOST.text(layout.provider_file)})"
        )
    try:
        specs = config_mod.group_specs(read_text_strict(layout.config))
    except Exception as exc:  # noqa: BLE001 - a broken config is a reported failure
        raise FailClosed("rendered config unreadable: " + _failure_reason(exc)) from exc

    supplement_names = []
    supplement_path = os.path.join(layout.providers, "nextin.yaml")
    if any(spec["uses_supplement"] for spec in specs) and os.path.isfile(supplement_path):
        supplement_names = config_mod.provider_node_names(read_text_strict(supplement_path))
    report.data["provider_nodes"] = len(names) + len(supplement_names)

    controller: dict[str, dict[str, Any]] = {}
    try:
        secrets = ctx.secrets()
        payload = ctx.controller(secrets.controller_secret).proxies()
        proxies = payload.get("proxies") if isinstance(payload, dict) else None
        if isinstance(proxies, dict):
            controller = {name: item for name, item in proxies.items() if isinstance(item, dict)}
    except (FailClosed, ControllerError) as exc:
        report.warn(
            "controller unavailable; showing filter match counts only: "
            + _failure_reason(exc)
        )

    rows: list[dict[str, Any]] = []
    for spec in specs:
        row: dict[str, Any] = dict(spec)
        if spec["uses_provider"] and spec["filter"]:
            try:
                group_names = names + supplement_names if spec["uses_supplement"] else names
                row["matched_nodes"] = policy.count_matches(group_names, spec["filter"])
            except FailClosed:
                row["matched_nodes"] = None
            if row["matched_nodes"] == 0:
                row["warning"] = "no node matches: mihomo degrades this group to COMPATIBLE (direct)"
                report.warn(
                    f"{spec['name']}: no node matches its filter; mihomo degrades "
                    "it to COMPATIBLE (direct)"
                )
        members: dict[str, bool | None] = {}
        for member in spec["proxies"]:
            item = controller.get(member)
            members[member] = None if item is None else bool(item.get("alive"))
        if members:
            row["members_alive"] = members
        item = controller.get(spec["name"])
        if item is not None:
            row["now"] = item.get("now")
            alive = item.get("alive")
            row["alive"] = None if alive is None else bool(alive)
        rows.append(row)
        report.add(_group_line(row))
    report.data["groups"] = rows
    report.status = "OK"
    return report


# ---- update-sources -------------------------------------------------------


#: Exit codes of ``convert_sub.py`` in multi-source mode (see its docs):
#: 0 all sources fetched, 5 partial success with atomic output, 1 empty pool.
CONVERT_PARTIAL_EXIT = 5


def update_sources(
    ctx: Context,
    *,
    output: str | None = None,
    min_nodes: int | None = None,
    quiet: bool = False,
    load_module: Callable[[str], Any] | None = None,
) -> Report:
    """Fetch every configured subscription source and merge a candidate pool.

    The macOS counterpart of Windows' ``update-subscription.ps1``: reads
    ``SUBSCRIPTION_URL_1..3`` (+ per-source UA) from the 0600 secret file,
    fetches/converts/merges them with the **installed** ``convert_sub.py``
    (including the local supplement for source 1 when present), and writes the
    merged candidate atomically as 0600.  The candidate is an evaluation
    artifact - it never replaces the live provider; promoting it is a separate,
    explicit decision.

    Exit codes follow the converter: 0 all sources OK, 5 partial success
    (output still written), 1 empty pool / below threshold.  The converter
    never prints a subscription URL, and the URL never appears in argv - only
    the env *file* is named.
    """
    report = Report(command="update-sources")
    layout = ctx.layout
    try:
        secrets = ctx.secrets()
        report.data["sources"] = sorted(secrets.extra_subscription_urls)
    except FailClosed as exc:
        report.warn("secret file unreadable: " + _failure_reason(exc))
        report.data["sources"] = []
    destination = output or os.path.join(layout.providers, "candidate.yaml")
    supplement = os.path.join(layout.providers, "nextin.yaml")
    protected = (layout.provider_file, supplement, layout.config, layout.secret_file)
    if os.path.realpath(destination) in {os.path.realpath(path) for path in protected}:
        raise FailClosed("candidate output must not replace a live configuration or source file")
    argv = ["--sources-env-file", layout.secret_file, "-o", destination]
    if os.path.isfile(supplement):
        argv += ["--source1-supplement", supplement]
    if min_nodes is not None:
        argv += ["--min-nodes", str(min_nodes)]
    module = (load_module or config_mod.load_convert_module)(layout.convert_sub)
    try:
        code = int(module.main(argv))
    except SystemExit as exc:  # defensive: argparse on our argv never exits
        code = int(exc.code or 0)
    except FailClosed:
        raise
    except Exception as exc:  # noqa: BLE001 - converter failures are reported, sanitized
        raise FailClosed("subscription conversion failed: " + _failure_reason(exc)) from exc

    report.data["exit_code"] = code
    report.data["output"] = HOST.text(destination)
    if code in (0, CONVERT_PARTIAL_EXIT):
        if not os.path.isfile(destination):
            raise FailClosed(
                "converter reported success but wrote no candidate file: "
                + HOST.text(destination)
            )
        report.add("candidate pool written: " + HOST.text(destination))
        report.status = "OK"
        if code == CONVERT_PARTIAL_EXIT:
            report.status = "DEGRADED"
            report.exit_code = code
            report.warn(
                "some sources failed but the merged candidate was written "
                "(exit 5; do not treat partial success as full success)"
            )
    else:
        report.status = "FAILED"
        report.exit_code = code
        report.add(f"no candidate pool written (converter exit {code})")
    return report


# ---- uninstall ------------------------------------------------------------


def uninstall(ctx: Context, *, purge: bool = False, quiet: bool = False) -> Report:
    report = Report(command="uninstall")
    layout = ctx.layout
    stopped = stop(ctx, quiet=True)
    if stopped.status == "FAILED":
        report.warn("stop did not fully confirm; uninstall continues but check for an orphan core")
    # ``stop`` already disables (and reads the override back); repeat explicitly
    # so uninstall never leaves an enabled override behind even if the job was
    # not loaded.
    ctx.launchd().disable()

    # The boot entry must go too: an enabled boot-switch whose plist was deleted
    # would try to run a missing program at every boot.  Both steps are best
    # effort here (uninstall must keep removing files even if launchd refuses)
    # but they are reported.
    boot = _boot_launchd(ctx)
    try:
        boot.bootout()
    except Exception as exc:  # noqa: BLE001 - reported, uninstall continues
        report.warn("boot-switch bootout failed: " + _failure_reason(exc))
    try:
        boot.disable()
    except Exception as exc:  # noqa: BLE001 - reported, uninstall continues
        report.warn("boot-switch disable failed: " + _failure_reason(exc))

    # The Feishu sync job goes too: an enabled job whose plist was deleted
    # would be an orphan launchd definition.  Best effort like the boot entry.
    feishu = _feishu_sync_launchd(ctx)
    try:
        feishu.bootout()
    except Exception as exc:  # noqa: BLE001 - reported, uninstall continues
        report.warn("feishu-sync bootout failed: " + _failure_reason(exc))
    try:
        feishu.disable()
    except Exception as exc:  # noqa: BLE001 - reported, uninstall continues
        report.warn("feishu-sync disable failed: " + _failure_reason(exc))

    if os.path.isfile(layout.xigua_ippool_plist):
        xigua = _xigua_ippool_launchd(ctx)
        try:
            xigua.bootout()
            xigua.disable()
        except Exception as exc:  # noqa: BLE001 - reported, uninstall continues
            report.warn("xigua-ippool shutdown failed: " + _failure_reason(exc))

    pipeline = _node_pipeline_launchd(ctx)
    try:
        pipeline.bootout()
        pipeline.disable()
    except Exception as exc:  # noqa: BLE001 - reported, uninstall continues
        report.warn("node-pipeline shutdown failed: " + _failure_reason(exc))

    removed: list[str] = []

    def discard(path: str) -> None:
        if not layout.is_project_path(path):
            raise FailClosed(f"refusing to delete a path outside the project prefixes: {path}")
        if remove_if_present(path):
            removed.append(path)

    discard(layout.plist)
    discard(layout.boot_plist)
    discard(layout.feishu_sync_plist)
    discard(layout.xigua_ippool_plist)
    discard(layout.node_pipeline_plist)
    discard(layout.cli_entry)
    discard(layout.switch_entry)
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
