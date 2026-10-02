"""Lifecycle engine - the one implementation behind every subcommand (D10=B).

Design invariants:

* the CLI wrapper holds no logic; everything goes through this module
* every operation is idempotent: a second run either reports "unchanged" or
  re-verifies, and never creates a second unit, instance or config change
* validation happens in a temp file; the live config is only replaced by an
  atomic rename once temp -> 0600 -> static audit -> ``mihomo -t`` all pass
* a failed validation never overwrites the previous config and never restarts
* the service is addressed through ``systemctl`` only, scoped to our unit
* every printed line is sanitised on the way out
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from . import config as config_mod
from . import preflight as preflight_mod
from . import state as state_mod
from . import supply as supply_mod
from . import unit as unit_mod
from .atomicio import (
    check_output_mode,
    check_secret_input,
    ensure_dir,
    is_noop,
    read_text_strict,
    remove_if_present,
    secure_tempdir,
    write_atomic,
)
from .controller import Controller, ControllerError
from .errors import ExitCode, FailClosed, NotReady
from .executor import Executor
from .paths import PROJECT, UNIT_NAME, Layout, managed_dirs
from .sanitize import HOST, describe_url, sanitize
from .secrets_io import SecretInputs, resolve as resolve_secrets
from .systemdctl import Systemd

SUBCOMMANDS = (
    "preflight",
    "install",
    "configure",
    "start",
    "stop",
    "restart",
    "status",
    "test",
    "update-subscription",
    "uninstall",
)


@dataclass
class Report:
    """Outcome of one operation, safe to print and safe to serialise."""

    command: str
    status: str  # OK | DEGRADED | CHANGED | UNCHANGED | FAILED
    messages: list[str] = field(default_factory=list)
    data: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # override the exit code for FAILED reports (e.g. UNSUPPORTED=4); None = 1
    exit_code: int | None = None

    def add(self, message: str) -> None:
        self.messages.append(HOST.text(message))

    def warn(self, message: str) -> None:
        self.warnings.append(HOST.text(message))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": state_mod.STATE_VERSION,
            "command": self.command,
            "status": self.status,
            "messages": [HOST.text(m) for m in self.messages],
            "warnings": [HOST.text(w) for w in self.warnings],
            "data": _safe(self.data),
        }


def _safe(value: Any) -> Any:
    """Recursively sanitise anything destined for stdout or --json."""
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return HOST.text(value)
    return value


@dataclass
class Context:
    """Everything a lifecycle operation needs, fully injectable."""

    layout: Layout
    executor: Executor
    facts: preflight_mod.HostFacts
    out: TextIO = sys.stdout
    err: TextIO = sys.stderr
    lock_path: str = ""
    environ: dict[str, str] = field(default_factory=dict)
    secret_file: str | None = None
    enable: bool = False
    sleeper: Callable[[float], None] | None = None
    controller_factory: Callable[[str], Controller] | None = None
    force_download: bool = False
    fetch_override: Callable[[str, str], None] | None = None
    dry_run: bool = False
    proc_root: str = "/proc"
    quiet: bool = False
    # production is always the system profile (root-owned secrets).  Tests that
    # run unprivileged switch this off; nothing else changes.
    system_profile: bool = True

    # ---- helpers -------------------------------------------------------
    def say(self, message: str) -> None:
        self.out.write(sanitize(message).rstrip("\n") + "\n")

    def sleep(self, seconds: float) -> None:
        (self.sleeper or time.sleep)(seconds)

    def shout(self, message: str) -> None:
        """Loud banner, used for DEGRADED so it can never be missed."""
        self.out.write("\n" + "!" * 72 + "\n" + sanitize(message).rstrip("\n") + "\n" + "!" * 72 + "\n")

    @property
    def systemd(self) -> Systemd:
        return Systemd(
            self.executor,
            unit=UNIT_NAME,
            root=self.layout.root,
            sleeper=self.sleeper or time.sleep,
        )

    def preflight(self, *, strict: bool = True) -> preflight_mod.Preflight:
        return preflight_mod.evaluate(self.facts) if not strict else preflight_mod.preflight(self.facts)

    def make_controller(self, secret: str) -> Controller:
        if self.controller_factory is not None:
            return self.controller_factory(secret)
        return Controller(secret=secret)


# ---- shared preparation -----------------------------------------------------


def _prepare_dirs(ctx: Context) -> list[str]:
    created: list[str] = []
    for path, mode in managed_dirs(ctx.layout):
        existed = os.path.isdir(path)
        ensure_dir(path, mode)
        if not existed:
            created.append(path)
    return created


def _load_lock(ctx: Context) -> supply_mod.Lock:
    path = ctx.lock_path
    if not path or not os.path.isfile(path):
        raise FailClosed("supply lock file is missing; install cannot verify any download")
    return supply_mod.load_lock(path)


def _read_controller_secret(ctx: Context) -> str:
    """Read the stored controller secret for authenticated status/test calls."""
    path = ctx.layout.controller_secret_file
    if not os.path.isfile(path) or os.path.islink(path):
        return ""
    check_secret_input(path, require_root_owned=ctx.system_profile)
    return read_text_strict(path).strip()


def _can_read_secrets(ctx: Context) -> bool:
    """Unprivileged callers get a partial view instead of an error (section 9).

    A symlink or an over-wide secret file is *also* treated as unreadable:
    ``status`` never follows a link it did not create and never trusts a file
    whose permissions the project would have refused to write.
    """
    path = ctx.layout.controller_secret_file
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    try:
        if os.stat(path).st_mode & 0o077:
            return False
        os.close(os.open(path, os.O_RDONLY))
    except OSError:
        return False
    return True


# ---- configure --------------------------------------------------------------


def _render(
    ctx: Context,
    preflight: preflight_mod.Preflight,
    inputs: SecretInputs,
    *,
    cn_health: str = state_mod.HEALTH_OK,
) -> str:
    overrides = config_mod.load_overrides(_override_paths(ctx))
    return config_mod.render(inputs, preflight, overrides, cn_health=cn_health)


def _override_paths(ctx: Context) -> list[str]:
    directory = ctx.layout.overrides_dir
    if not os.path.isdir(directory):
        return []
    names = sorted(
        name for name in os.listdir(directory) if name.endswith(".conf") and not name.startswith(".")
    )
    paths = []
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path):
            raise FailClosed(f"refusing symlinked override file: {path}")
        paths.append(path)
    return paths


def _validate_with_mihomo(ctx: Context, candidate: str) -> tuple[bool, str]:
    """Run ``mihomo -t`` against *this candidate file* if the binary is present.

    The candidate - not the live ``config.yaml`` - is what must be validated:
    on a first install there is no live config yet, and on every later install
    the live file is the *previous* one, so testing it would approve a stale
    document and break the atomic replace flow.  The test-suite uses a fake
    executor, so no real binary is ever run.
    """
    binary = ctx.layout.binary
    if not os.path.exists(binary):
        return False, "binary not installed yet"
    result = ctx.executor.run([binary, "-t", "-f", candidate, "-d", ctx.layout.var_lib])
    if result.ok:
        return True, ""
    # config test failures must never echo config contents
    return False, f"mihomo -t exit={result.returncode}"


def _degraded_record(
    inputs: SecretInputs, preflight: preflight_mod.Preflight, existing: state_mod.Degraded
) -> state_mod.Degraded:
    """Build the record a ``configure`` run persists.

    A persisted CN ``DEGRADED`` is *never* reset to OK here: only an actual
    authenticated health check (``status`` / ``test`` / ``update-subscription``)
    may clear it, so a plain re-render cannot silently re-claim a healthy CN.
    """
    if inputs.cn_url:
        cn = (
            state_mod.HEALTH_DEGRADED
            if existing.cn == state_mod.HEALTH_DEGRADED
            else state_mod.HEALTH_OK
        )
        cn_details = list(existing.cn_details) if cn == state_mod.HEALTH_DEGRADED else []
    else:
        cn = state_mod.HEALTH_DISABLED
        cn_details = ["CN provider not configured (no CN rules rendered)"]
    return state_mod.Degraded(
        active=preflight.mode != "tun",
        mode=preflight.mode,
        reasons=list(preflight.degraded_reasons),
        cn=cn,
        cn_details=cn_details,
    )


def configure(ctx: Context, *, check_only: bool = False, quiet: bool = False) -> Report:
    """Render, validate and atomically install ``config.yaml``.  Repeatable."""
    report = Report(command="configure", status="OK")
    layout = ctx.layout
    preflight = ctx.preflight(strict=not check_only)
    inputs = resolve_secrets(
        layout, env_file=ctx.secret_file, environ=ctx.environ, system=ctx.system_profile
    )

    # the *persisted* CN health decides how CN traffic is routed: a provider
    # that has no live node gets an explicit refuse, never a dead-node pick
    existing = state_mod.load_degraded(layout)
    cn_live = existing.cn != state_mod.HEALTH_DEGRADED
    rendered = _render(
        ctx,
        preflight,
        inputs,
        cn_health=existing.cn if inputs.cn_url else state_mod.HEALTH_DISABLED,
    )
    config_mod.audit(
        rendered, cn_configured=bool(inputs.cn_url), cn_live=cn_live
    )

    plan = state_mod.Plan(
        mode=preflight.mode,
        asset_suffix=preflight.asset_suffix,
        foreign_provider=config_mod.FOREIGN_PROVIDER,
        cn_provider=config_mod.CN_PROVIDER if inputs.cn_url else "",
        cn_configured=bool(inputs.cn_url),
        foreign_description=describe_url(inputs.foreign_url),
        cn_description=describe_url(inputs.cn_url) if inputs.cn_url else "disabled",
        ports={
            "mixed": config_mod.MIXED_PORT,
            "controller": config_mod.CONTROLLER_PORT,
            "dns": config_mod.DNS_PORT,
        },
        config_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    )

    if check_only:
        # ExecStartPre path: read-only.  Verify that the on-disk config matches
        # what we would render, and write *nothing* - not state, not backups.
        if not os.path.isfile(layout.config):
            raise FailClosed("config.yaml has not been rendered yet (run configure)")
        current = read_text_strict(layout.config)
        if current != rendered:
            raise FailClosed("config.yaml on disk differs from the rendered config; run configure")
        check_output_mode(layout.config)
        report.status = "UNCHANGED"
        return report

    degraded = _degraded_record(inputs, preflight, state_mod.load_degraded(layout))

    if is_noop(layout.config, rendered, mode=0o600):
        check_output_mode(layout.config)
        state_mod.save_plan(layout, plan)
        state_mod.save_degraded(layout, degraded)
        report.status = "UNCHANGED"
        if not quiet:
            report.add("config unchanged; nothing rewritten")
        return report

    # temp file -> 0600 -> static audit -> mihomo -t -> atomic replace
    ensure_dir(layout.var_lib, 0o750)
    with secure_tempdir(layout.staging_dir, "render") as staging:
        candidate = os.path.join(staging, "config.yaml")
        write_atomic(candidate, rendered, mode=0o600, mkdir=False)
        check_output_mode(candidate)
        text = read_text_strict(candidate)
        config_mod.audit(text, cn_configured=bool(inputs.cn_url), cn_live=cn_live)
        ok, detail = _validate_with_mihomo(ctx, candidate)
        if not ok:
            # never touch the live config on validation failure
            report.warn(f"config test skipped or failed: {detail}")
            if detail.startswith("mihomo -t"):
                raise FailClosed(f"rendered config rejected by mihomo -t: {detail}")
        state_mod.backup(layout, layout.config)
        os.replace(candidate, layout.config)
        os.chmod(layout.config, 0o600)
        check_output_mode(layout.config)

    state_mod.save_plan(layout, plan)
    state_mod.save_degraded(layout, degraded)
    report.status = "CHANGED" if os.path.isfile(layout.config) else "OK"
    if not quiet:
        report.add(f"config rendered ({preflight.mode}) at mode 0600")
        if not inputs.cn_url:
            report.add("CN feature: disabled")
        if degraded.active:
            report.add("DEGRADED: " + "; ".join(degraded.reasons))
    return report


# ---- install ----------------------------------------------------------------


def install(ctx: Context, *, skip_download: bool = False, quiet: bool = False) -> Report:
    """preflight -> secrets -> dirs -> binary+geo -> config -> unit -> start."""
    report = Report(command="install", status="OK")
    layout = ctx.layout
    preflight = ctx.preflight()
    resolve_secrets(layout, env_file=ctx.secret_file, environ=ctx.environ, system=ctx.system_profile)
    lock = _load_lock(ctx)
    # config/secret/state directories exist before anything is written into them
    _prepare_dirs(ctx)

    if not skip_download:
        asset = lock.binary_for(preflight.asset_suffix)
        if asset.name != lock.asset(asset.key).name:  # pragma: no cover - defensive
            raise FailClosed("lock resolution is inconsistent")
        fetch = _make_fetcher(ctx)
        try:
            outcome = supply_mod.install_verified(
                layout=layout,
                lock=lock,
                asset_suffix=preflight.asset_suffix,
                fetch=fetch,
            )
        except FailClosed as exc:
            # nothing installed, service not started
            report.status = "FAILED"
            report.add(f"supply verification failed; aborting before install: {HOST.text(str(exc))}")
            raise
        report.data["binary"] = {
            "version": outcome.version,
            "asset": outcome.asset,
            "sha256": outcome.sha256,
            "geo": outcome.geo,
        }
        if not quiet:
            report.add(f"verified binary {outcome.asset} sha256:{outcome.sha256}")
    else:
        if not os.path.exists(layout.binary):
            raise FailClosed("--skip-download requires an already-installed binary")
        report.data["binary"] = {"version": lock.tag, "asset": "skipped"}

    config_report = configure(ctx, quiet=quiet)
    report.data["mode"] = config_report.status

    # install static resources + CLI entry + unit
    _install_static(ctx)
    _install_cli(ctx)
    unit_text = unit_mod.render(
        binary=layout.binary,
        state_dir=layout.var_lib,
        run_dir=layout.run,
        cli_entry=layout.cli_entry,
        tun=preflight.mode == "tun",
    )
    if is_noop(layout.unit, unit_text, mode=0o644):
        report.add("unit unchanged")
    else:
        write_atomic(layout.unit, unit_text, mode=0o644)
    systemd = ctx.systemd
    systemd.daemon_reload()
    verified, detail = systemd.verify()
    if not verified and detail != "systemd-analyze not available":
        raise FailClosed(f"systemd-analyze verify failed: {sanitize(detail)[:200]}")
    report.data["systemd_analyze_verify"] = "PASS" if verified else "NOT_RUN"
    if not verified:
        report.warn("systemd-analyze unavailable; unit content audit used instead")

    if ctx.enable:
        systemd.enable()
        report.add("unit enabled")
    else:
        report.add("enable state left untouched (pass --enable to opt in)")

    start_report = start(ctx, quiet=quiet)
    report.data["start"] = start_report.status
    report.data["degraded"] = preflight.mode != "tun"
    if preflight.mode != "tun":
        report.status = "DEGRADED"
        ctx.shout(
            "DEGRADED: TUN unavailable, running mixed-port only. "
            + "; ".join(preflight.degraded_reasons)
            + " (system DNS and routing were NOT modified)"
        )
        report.add("DEGRADED reasons: " + "; ".join(preflight.degraded_reasons))
    if not quiet:
        report.add("installed; management CLI at " + layout.cli_entry)
    return report


def _source_dir(ctx: Context) -> str:
    """The ``linux/`` directory this package was shipped in (overridable)."""
    configured = os.environ.get("MPM_SOURCE_DIR", "")
    if configured and os.path.isdir(configured):
        return configured
    # linux/python/mpm/lifecycle.py -> linux/
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _copy_tree(src: str, dest: str, *, mode: int) -> list[str]:
    """Idempotent copy of ``*.py`` from ``src`` into ``dest``; returns changes."""
    if not os.path.isdir(src):
        raise FailClosed(f"source package directory is missing: {src}")
    changed: list[str] = []
    ensure_dir(dest, 0o755)
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py"):
            continue
        source = os.path.join(src, name)
        if not os.path.isfile(source):
            continue
        target = os.path.join(dest, name)
        payload = read_text_strict(source)
        if is_noop(target, payload, mode=mode):
            continue
        write_atomic(target, payload, mode=mode)
        changed.append(name)
    return changed


def _install_static(ctx: Context) -> None:
    """Install the non-secret artifacts: package, lock, templates.

    Everything here is world-readable by design and contains no secret.  The
    copies are idempotent, so a second ``install`` rewrites nothing.
    """
    layout = ctx.layout
    source = _source_dir(ctx)
    ensure_dir(layout.share_python, 0o755)
    ensure_dir(os.path.dirname(layout.installed_lock), 0o755)
    ensure_dir(layout.template_dir, 0o755)

    pkg_src = os.path.join(source, "python", "mpm")
    if os.path.abspath(pkg_src) != os.path.abspath(layout.share_python):
        _copy_tree(pkg_src, layout.share_python, mode=0o644)

    lock_src = os.path.join(source, "supply", "mihomo.lock.json")
    if os.path.isfile(lock_src):
        payload = read_text_strict(lock_src)
        if not is_noop(layout.installed_lock, payload, mode=0o644):
            write_atomic(layout.installed_lock, payload, mode=0o644)

    unit_tpl = os.path.join(source, "systemd", UNIT_NAME + ".template")
    if os.path.isfile(unit_tpl):
        target = os.path.join(layout.template_dir, UNIT_NAME + ".template")
        payload = read_text_strict(unit_tpl)
        if not is_noop(target, payload, mode=0o644):
            write_atomic(target, payload, mode=0o644)
    else:
        write_atomic(
            os.path.join(layout.template_dir, UNIT_NAME + ".template"),
            _UNIT_PLACEHOLDER,
            mode=0o644,
        )


_UNIT_PLACEHOLDER = "# rendered per-host by the installer; see mpm/unit.py\n"


def _install_cli(ctx: Context) -> None:
    """Install the thin entry wrapper (no logic lives in it)."""
    layout = ctx.layout
    ensure_dir(layout.usr_local_bin, 0o755)
    source = os.environ.get("MPM_ENTRY_SCRIPT", "") or os.path.join(
        _source_dir(ctx), "bin", PROJECT
    )
    if os.path.isfile(source):
        payload = read_text_strict(source)
    else:
        # fallback: the same three-line wrapper, generated
        payload = (
            "#!/bin/sh\n"
            "# Thin entry point - all logic lives in the Python package.\n"
            'exec "${MPM_PYTHON:-python3}" -c "import sys; sys.path.insert(0, '
            + repr(layout.share_python)
            + '); from mpm.cli import main; raise SystemExit(main())" "$@"\n'
        )
    if is_noop(layout.cli_entry, payload, mode=0o755):
        return
    write_atomic(layout.cli_entry, payload, mode=0o755)


def _make_fetcher(ctx: Context) -> supply_mod.Fetcher:
    """Return the download function; overridable so tests use mock HTTP."""
    if ctx.fetch_override is not None:
        return ctx.fetch_override

    def fetch(url: str, dest: str) -> None:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url, headers={"User-Agent": "mihomo-proxy-management-supply"}
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with open(dest, "wb") as handle:
                    for block in iter(lambda: response.read(1 << 20), b""):
                        handle.write(block)
        except (urllib.error.URLError, OSError) as exc:
            raise FailClosed(f"download failed: {HOST.text(str(exc))}") from exc

    return fetch


# ---- start / stop / restart -------------------------------------------------


def _assert_ports_free(ctx: Context, *, ignore_own_unit: bool = True) -> None:
    """Refuse to start when a foreign process holds a port (never kill it)."""
    ports = [config_mod.MIXED_PORT, config_mod.CONTROLLER_PORT, config_mod.DNS_PORT]
    occupants = preflight_mod.port_occupants(ctx.executor, ports, proc_root=ctx.proc_root)
    if not occupants:
        return
    blockers = []
    for port, owner in sorted(occupants.items()):
        if ignore_own_unit and owner == UNIT_NAME:
            continue
        blockers.append(f"{port}:{owner}")
    if blockers:
        raise FailClosed(
            "port(s) already held by another owner (" + ", ".join(blockers) + "); "
            "refusing to start and will not terminate that process"
        )


def _read_config_ports(ctx: Context) -> None:
    check_output_mode(ctx.layout.config)


def _ready(ctx: Context, *, timeout: float = 20.0, attempts: int = 20) -> dict[str, object]:
    """Authenticated readiness probe; unauthenticated probe is never attempted."""
    secret = _read_controller_secret(ctx)
    if not secret:
        raise FailClosed("controller secret unavailable; cannot verify readiness")
    controller = ctx.make_controller(secret)
    last_error = ""
    for _ in range(attempts):
        try:
            payload = controller.version()
            if isinstance(payload, dict) and payload.get("version"):
                return {"version": str(payload["version"])}
            last_error = "empty /version response"
        except (ControllerError, FailClosed) as exc:
            last_error = HOST.text(str(exc))[:120]
        ctx.sleep(0.5)
    raise NotReady(f"authenticated readiness check failed ({last_error})")


def start(ctx: Context, *, quiet: bool = False) -> Report:
    report = Report(command="start", status="OK")
    systemd = ctx.systemd
    state = systemd.state()
    if state.is_active:
        report.status = "UNCHANGED"
        if not quiet:
            report.add("unit already active; no second instance started")
        degraded = state_mod.load_degraded(ctx.layout)
        if degraded.status() == state_mod.HEALTH_DEGRADED:
            report.status = "DEGRADED"
            ctx.shout("DEGRADED: " + "; ".join(degraded.all_reasons()))
        return report

    if not systemd.unit_installed():
        raise FailClosed("unit is not installed; run install first")
    if not os.path.isfile(ctx.layout.config):
        raise FailClosed("config.yaml missing; run configure before start")
    _read_config_ports(ctx)
    _assert_ports_free(ctx)
    try:
        systemd.start()
    except FailClosed as exc:
        raise FailClosed(f"systemctl start failed: {HOST.text(str(exc))}") from exc
    if not systemd.wait_active():
        raise NotReady("unit did not become active")
    ready = _ready(ctx)
    report.data["version"] = ready["version"]
    degraded = state_mod.load_degraded(ctx.layout)
    if degraded.status() == state_mod.HEALTH_DEGRADED:
        report.status = "DEGRADED"
        ctx.shout("DEGRADED: running mixed-port only - " + "; ".join(degraded.all_reasons()))
    if not quiet:
        report.add(f"active, controller ready (mihomo {ready['version']})")
    return report


def stop(ctx: Context, *, quiet: bool = False) -> Report:
    """Idempotent stop; only ``systemctl stop`` on our unit."""
    report = Report(command="stop", status="OK")
    systemd = ctx.systemd
    if not systemd.state().is_active:
        report.status = "UNCHANGED"
        if not quiet:
            report.add("unit already inactive")
        return report
    systemd.stop()
    if not systemd.wait_stopped():
        raise NotReady("unit is still active after systemctl stop")
    if not quiet:
        report.add("unit stopped")
    return report


def restart(ctx: Context, *, quiet: bool = False) -> Report:
    """stop -> confirm stopped -> start -> readiness.  Never checks before start."""
    report = Report(command="restart", status="OK")
    stop_report = stop(ctx, quiet=True)
    if not stop_report.status in ("OK", "UNCHANGED"):  # pragma: no cover
        raise NotReady("stop phase failed")
    start_report = start(ctx, quiet=True)
    report.status = start_report.status
    report.data.update(start_report.data)
    if not quiet:
        report.add("restarted (stop confirmed before start)")
    return report


# ---- status -----------------------------------------------------------------


def status(ctx: Context) -> Report:
    report = Report(command="status", status="OK")
    layout = ctx.layout
    systemd = ctx.systemd
    unit_state = systemd.state()
    degraded = state_mod.load_degraded(layout)
    plan = state_mod.load_plan(layout)
    can_read = _can_read_secrets(ctx)

    data: dict[str, object] = {
        "unit": unit_state.to_dict(),
        "deployment_status": degraded.status(),
        "mode": plan.mode or "unknown",
        "degraded": degraded.active,
        "degraded_reasons": [HOST.text(r) for r in degraded.reasons],
        "cn": degraded.cn,
        "providers": {
            "foreign": plan.foreign_description or "unknown",
            "cn": plan.cn_description or "disabled",
        },
        "ports": plan.ports or {},
        "tun": plan.mode == "tun",
        "binary": {"installed": os.path.exists(layout.binary), "pinned_tag": plan.mihomo_tag},
        "config": {
            "present": os.path.isfile(layout.config),
            "mode_ok": _mode_ok(layout.config, 0o600),
            "sha256": plan.config_sha256,
        },
        "supplied_by_state": bool(plan.mode),
    }

    if not can_read:
        # partial view: systemd/port facts only, and say so explicitly
        missing = [
            "version",
            "proxies",
            "providers.nodes",
            "cn.node_health",
            "controller",
        ]
        data["privilege"] = {
            "controller_secret_readable": False,
            "partial_view": True,
            "missing_fields": missing,
            "note": "run as root (or with read access to the 0600 secret file) for the full view",
        }
        report.data = data
        report.status = "OK" if unit_state.is_active else state_mod.HEALTH_DEGRADED
        report.warn("partial status: controller secret not readable")
        return report

    controller = None
    try:
        controller = ctx.make_controller(_read_controller_secret(ctx))
        payload = controller.version()
        data["version"] = payload.get("version", "unknown")
    except (ControllerError, FailClosed) as exc:
        data["controller"] = {"reachable": False, "error": HOST.text(str(exc))[:120]}
        report.warn("controller not reachable")

    if controller is not None:
        try:
            providers = controller.providers()
            entries = providers.get("providers") if isinstance(providers, dict) else None
            summary = _provider_summary(controller, entries if isinstance(entries, dict) else {})
            data["providers"]["detail"] = summary
            cn_state, cn_changed = _record_cn_health(layout, plan, summary)
            data["cn"] = cn_state
            if cn_changed and os.path.isfile(layout.config):
                # the recorded health just moved: the live config's CN rules must
                # move with it, or CN traffic would still select a dead node
                synced, detail = _sync_cn_routing(ctx)
                if synced:
                    report.warn(
                        "CN health changed: the rendered CN rules now match it "
                        "(restart to load them into mihomo)"
                    )
                    degraded = state_mod.load_degraded(layout)
                else:
                    data["cn_routing_sync"] = {"ok": False, "error": detail}
                    report.warn(f"CN rules could not be re-rendered: {detail}")
        except (ControllerError, FailClosed) as exc:
            data["providers"]["detail"] = {"error": HOST.text(str(exc))[:120]}

    report.data = data
    if degraded.status() == state_mod.HEALTH_DEGRADED:
        report.status = state_mod.HEALTH_DEGRADED
    elif not unit_state.is_active:
        report.status = "INACTIVE"
    return report


def _node_alive_flags(controller) -> dict[str, bool]:
    """Per-node liveness as the controller reports it (``/proxies``).

    Missing entries simply stay absent: the caller then falls back to the
    provider's own ``alive`` list, and to *unknown* when neither is available.
    """
    flags: dict[str, bool] = {}
    try:
        payload = controller.proxies()
    except (ControllerError, FailClosed):
        return flags
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        return flags
    for name, info in proxies.items():
        if isinstance(info, dict) and "alive" in info:
            flags[str(name)] = bool(info["alive"])
    return flags


def _provider_nodes(info: dict[str, Any]) -> list[tuple[str, bool | None]]:
    """Normalise a provider's node list into ``(name, alive)`` pairs.

    mihomo reports nodes either as bare names (``vehicle``) or as objects with a
    ``name``/``alive`` pair (``all``); both shapes are accepted here so liveness
    is never guessed from the count.
    """
    raw = info.get("all") or info.get("vehicle") or info.get("proxies") or []
    nodes: list[tuple[str, bool | None]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            name = str(item.get("name") or "")
            alive = bool(item["alive"]) if "alive" in item else None
        else:
            name = str(item)
            alive = None
        if name:
            nodes.append((name, alive))
    return nodes


def _provider_summary(controller, entries: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Node count *and* live-node count per provider (D8b=2).

    Counting nodes alone is not a health statement: a provider can list nodes
    that are all dead.  ``alive_count`` is ``None`` when neither the provider
    entry nor ``/proxies`` carries liveness information, which is treated as
    unhealthy (fail closed) rather than as "assume fine".
    """
    flags = _node_alive_flags(controller)
    summary: dict[str, dict[str, Any]] = {}
    for name, info in entries.items():
        if not isinstance(info, dict):
            continue
        nodes = _provider_nodes(info)
        known = [
            (flags[node] if node in flags else alive)
            for node, alive in nodes
        ]
        known = [flag for flag in known if flag is not None]
        if not known and isinstance(info.get("alive"), list):
            alive_names = {str(item) for item in info["alive"]}
            known = [node in alive_names for node, _alive in nodes]
        alive_count = sum(1 for flag in known if flag) if known else None
        summary[str(name)] = {
            "node_count": len(nodes),
            "alive_count": alive_count,
            "updated_at": str(info.get("updatedAt") or ""),
        }
    return summary


def _cn_reason(info: dict[str, Any] | None) -> str:
    if not info:
        return "CN provider is not visible through the controller"
    if int(info.get("node_count") or 0) == 0:
        return "CN provider has no live nodes"
    if info.get("alive_count") is None:
        return "CN provider node liveness is unknown"
    if int(info["alive_count"]) == 0:
        return "CN provider has no live nodes"
    return ""


def _record_cn_health(layout, plan, summary: dict[str, dict[str, Any]]) -> tuple[str, bool]:
    """Persist the CN health decision; return ``(state, changed)``.

    A failed health check always persists DEGRADED.  A *successful* authenticated
    health check is the only thing that can turn a persisted CN DEGRADED back to
    OK, and a CN provider that is not configured at all reports DISABLED.
    """
    degraded = state_mod.load_degraded(layout)
    previous = degraded.cn
    if not plan.cn_configured:
        if degraded.cn != state_mod.HEALTH_DISABLED:
            degraded.cn = state_mod.HEALTH_DISABLED
            degraded.cn_details = ["CN provider not configured (no CN rules rendered)"]
            state_mod.save_degraded(layout, degraded)
        return state_mod.HEALTH_DISABLED, previous != state_mod.HEALTH_DISABLED

    info = summary.get(config_mod.CN_PROVIDER)
    state = _cn_health(None, plan, summary)
    reason = _cn_reason(info)
    if state == state_mod.HEALTH_DEGRADED:
        if degraded.cn != state_mod.HEALTH_DEGRADED or reason not in degraded.cn_details:
            degraded.cn = state_mod.HEALTH_DEGRADED
            degraded.cn_details = [reason + "; CN-EXIT will not select a dead node and "
                                   "does not fall back to DIRECT or a foreign node"]
            state_mod.save_degraded(layout, degraded)
    elif degraded.cn == state_mod.HEALTH_DEGRADED:
        # cleared by a real health check, not by a re-render
        degraded.cn = state_mod.HEALTH_OK
        degraded.cn_details = []
        state_mod.save_degraded(layout, degraded)
    return state, degraded.cn != previous


def _sync_cn_routing(ctx: Context, *, quiet: bool = True) -> tuple[bool, str]:
    """Re-render the live config so CN routing matches the recorded CN health.

    Without this, a CN provider that just died would keep a live config whose CN
    rules still point at CN-EXIT - i.e. traffic would be sent to a dead node.
    ``configure`` reads the persisted health, so a plain re-render switches the
    CN rules to the explicit refuse (and back once a health check passes).
    A failure here is reported, never swallowed: the caller decides how loudly.
    """
    try:
        configure(ctx, quiet=quiet)
        return True, ""
    except FailClosed as exc:
        return False, HOST.text(str(exc))[:160]


def _cn_health(controller, plan, summary) -> str:
    """CN is DEGRADED when configured but empty/dead; never silently OK."""
    if not plan.cn_configured:
        return "DISABLED"
    info = summary.get(config_mod.CN_PROVIDER) if isinstance(summary, dict) else None
    if not info:
        return state_mod.HEALTH_DEGRADED
    try:
        count = int(info.get("node_count") or 0)
    except (TypeError, ValueError):
        return state_mod.HEALTH_DEGRADED
    if count == 0:
        return state_mod.HEALTH_DEGRADED
    alive = info.get("alive_count")
    if alive is None:
        return state_mod.HEALTH_DEGRADED
    try:
        return "OK" if int(alive) > 0 else state_mod.HEALTH_DEGRADED
    except (TypeError, ValueError):
        return state_mod.HEALTH_DEGRADED


def _mode_ok(path: str, expected: int) -> bool:
    if not os.path.isfile(path):
        return False
    return stat.S_IMODE(os.stat(path).st_mode) == expected


# ---- test (local functional test, D12=A) ------------------------------------


def run_test(ctx: Context) -> Report:
    """Local, authenticated, offline-capable functional test.

    Never uses a real egress-IP service and never requires a live subscription:
    we check the controller contract, provider isolation in the rendered config
    and the recorded degraded/CN state.
    """
    report = Report(command="test", status="OK")
    layout = ctx.layout
    preflight_result = ctx.preflight(strict=False)
    checks: list[dict[str, str]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "result": "PASS" if passed else "FAIL", "detail": sanitize(detail)})
        if not passed:
            report.status = "FAILED"

    if not os.path.isfile(layout.config):
        check("config.rendered", False, "config.yaml missing")
        report.data["checks"] = checks
        return report

    text = read_text_strict(layout.config)
    check("config.mode_0600", _mode_ok(layout.config, 0o600), "expected 0600")
    persisted = state_mod.load_degraded(layout)
    cn_configured = f"{config_mod.CN_PROVIDER}:" in text
    try:
        # the audit must agree with the recorded CN health: a configured-but-dead
        # provider legitimately renders the explicit refuse, not CN-EXIT
        config_mod.audit(
            text,
            cn_configured=cn_configured,
            cn_live=persisted.cn != state_mod.HEALTH_DEGRADED,
        )
        check("config.audit", True)
    except FailClosed as exc:
        check("config.audit", False, str(exc))
    check("config.loopback_only", "allow-lan: false" in text and "127.0.0.1" in text)
    check(
        "config.provider_isolation",
        config_mod.FOREIGN_PROVIDER in text and "use:" in text,
    )

    secret = _read_controller_secret(ctx)
    check("controller.secret_present", bool(secret))
    if secret:
        try:
            controller = ctx.make_controller(secret)
            version = controller.version()
            check("controller.version", bool(version.get("version")), "no version field")
            proxies = controller.proxies()
            check("controller.proxies", isinstance(proxies, dict) and "proxies" in proxies)
            providers = controller.providers()
            entries = providers.get("providers", {}) if isinstance(providers, dict) else {}
            entries = entries if isinstance(entries, dict) else {}
            summary = _provider_summary(controller, entries)
            foreign = summary.get(config_mod.FOREIGN_PROVIDER) or {}
            check(
                "provider.foreign.nodes",
                bool(foreign.get("node_count"))
                and (foreign.get("alive_count") or 0) > 0,
                "no live foreign node visible (dead nodes are not a pass)",
            )
            plan = state_mod.load_plan(layout)
            # run_test is an authenticated health check, so it is allowed to
            # both set and clear the persisted CN state.
            cn_state, cn_changed = _record_cn_health(layout, plan, summary)
            if cn_changed and os.path.isfile(layout.config):
                synced, detail = _sync_cn_routing(ctx, quiet=True)
                check(
                    "config.cn_routing_matches_health",
                    synced,
                    detail or "CN rules re-rendered to match the recorded health",
                )
                if synced:
                    report.warn(
                        "CN health changed: the rendered CN rules now match it "
                        "(restart to load them into mihomo)"
                    )
            if plan.cn_configured:
                check(
                    "provider.cn.exit",
                    cn_state == "OK",
                    "CN provider configured but has zero or dead nodes (DEGRADED, no fallback)",
                )
            else:
                check("provider.cn.disabled", True, "CN disabled by configuration")
        except (ControllerError, FailClosed) as exc:
            check("controller.reachable", False, str(exc))

    degraded = state_mod.load_degraded(layout)
    check("state.degraded_recorded", (not degraded.active) or bool(degraded.reasons), "no reason stored")
    check("preflight.mode", preflight_result.mode in ("tun", "mixed"), preflight_result.mode)

    report.data["checks"] = checks
    report.data["status"] = report.status
    # A recorded DEGRADED explains *why* the deployment is degraded; it must
    # never mask a FAILed check, and it must never turn a FAILED report back
    # into a soft DEGRADED (mixed/TUN degradation is not a free pass).
    if degraded.status() == state_mod.HEALTH_DEGRADED and report.status != "FAILED":
        report.status = state_mod.HEALTH_DEGRADED
    if degraded.status() == state_mod.HEALTH_DEGRADED:
        ctx.shout("DEGRADED: " + "; ".join(degraded.all_reasons()))
    return report


# ---- update-subscription ----------------------------------------------------


def update_subscription(ctx: Context, *, provider: str | None = None) -> Report:
    """Refresh provider(s) through the authenticated controller.

    A failure returns non-zero; we never restart the service to fake success.
    """
    report = Report(command="update-subscription", status="OK")
    layout = ctx.layout
    if not _can_read_secrets(ctx):
        raise FailClosed("controller secret not readable; run as root")
    secret = _read_controller_secret(ctx)
    if not secret:
        raise FailClosed("controller secret missing; nothing was refreshed")
    plan = state_mod.load_plan(layout)

    targets = [config_mod.FOREIGN_PROVIDER]
    if plan.cn_configured:
        targets.append(config_mod.CN_PROVIDER)
    if provider:
        if provider not in targets:
            raise FailClosed(
                f"unknown provider '{provider}'; configured: {', '.join(targets)}"
            )
        targets = [provider]

    controller = ctx.make_controller(secret)
    results: dict[str, str] = {}
    failures = 0
    for name in targets:
        try:
            controller.refresh_provider(name)
            controller.healthcheck(name)
            results[name] = "refreshed"
        except (ControllerError, FailClosed) as exc:
            results[name] = "failed"
            failures += 1
            report.warn(f"provider {name} refresh failed: {HOST.text(str(exc))[:120]}")

    if plan.cn_configured:
        cn_changed = False
        try:
            info = controller.provider(config_mod.CN_PROVIDER)
            summary = _provider_summary(controller, {config_mod.CN_PROVIDER: info})
            cn_state, cn_changed = _record_cn_health(layout, plan, summary)
            if cn_state == state_mod.HEALTH_DEGRADED:
                report.warn(
                    "CN provider has zero or dead nodes after refresh: CN routing is DEGRADED "
                    "(no DIRECT/foreign fallback, CN-EXIT will not select a dead node)"
                )
                failures += 1
            else:
                report.add("CN provider has live nodes after refresh")
        except (ControllerError, FailClosed) as exc:
            # unreadable CN state is not "healthy": record it as degraded
            report.warn(f"CN provider state unreadable: {HOST.text(str(exc))[:120]}")
            degraded = state_mod.load_degraded(layout)
            cn_changed = degraded.cn != state_mod.HEALTH_DEGRADED
            degraded.cn = state_mod.HEALTH_DEGRADED
            degraded.cn_details = ["CN provider liveness unreadable after refresh; "
                                   "CN-EXIT does not fall back to DIRECT or a foreign node"]
            state_mod.save_degraded(layout, degraded)
            failures += 1
        if cn_changed and os.path.isfile(layout.config):
            # keep the rendered CN rules in step with the health we just measured
            synced, detail = _sync_cn_routing(ctx, quiet=True)
            if not synced:
                report.warn(f"CN rules could not be re-rendered: {detail}")
                failures += 1

    report.data["providers"] = results
    if failures:
        report.status = "FAILED"
        # the warnings carry *why* (e.g. the CN refuse semantics), so they are
        # repeated in the error text instead of being lost with the report
        detail = "; ".join(report.warnings)
        raise FailClosed(
            "subscription refresh failed; not pretending success"
            + (f" [{detail}]" if detail else "")
        )
    report.add("providers refreshed via authenticated controller")
    return report


# ---- uninstall / purge ------------------------------------------------------


def _secrets_laid_out(layout: Layout) -> list[str]:
    """Everything install produced that is secret or secret-derived."""
    candidates = [
        layout.subscription_env,
        layout.controller_secret_file,
        layout.config,
        layout.providers,
        layout.geoip,
        layout.geosite,
        os.path.join(layout.var_lib, "cache.db"),
        os.path.join(layout.var_lib, "Country.mmdb"),
        os.path.join(layout.var_lib, "geoip.metadb"),
        layout.backups_dir,
        layout.staging_dir,
        os.path.join(layout.var_lib, "fake-ip.db"),
    ]
    return candidates


def uninstall(ctx: Context, *, purge: bool = False, assume_yes: bool = False) -> Report:
    """Disable + remove unit, binaries, state and secrets; keep overrides.d.

    ``purge`` additionally removes everything under the project's own prefixes,
    after verifying each path is inside them.  Foreign routing/firewall/DNS
    state is only reported, never deleted.  Explicit confirmation is required
    for purge; ``ctx.dry_run`` (used by the test-suite) touches nothing.
    """
    if purge and not assume_yes:
        raise FailClosed(
            "purge deletes every project configuration file; re-run with --yes to confirm"
        )
    report = Report(command="uninstall", status="OK")
    layout = ctx.layout
    systemd = ctx.systemd

    if os.path.isfile(layout.unit):
        systemd.disable_now()
        report.add("unit disabled (--now)")
    else:
        report.add("unit not present; nothing to disable")

    removed: list[str] = []

    def discard(path: str) -> None:
        if ctx.dry_run:
            if os.path.exists(path):
                removed.append(path)
            return
        if remove_if_present(path):
            removed.append(path)

    for path in (layout.unit, layout.unit_dropin_dir):
        discard(path)
    if not ctx.dry_run:
        systemd.daemon_reload()

    if purge:
        targets = [
            layout.cli_entry,
            layout.libexec,
            layout.share,
            layout.etc,
            layout.var_lib,
            layout.run,
        ]
        for path in targets:
            canonical = os.path.normpath(os.path.abspath(path))
            if not layout.is_project_path(canonical):
                raise FailClosed(f"refusing to delete a path outside the project prefixes: {canonical}")
            discard(canonical)
        report.add("purge: all project configuration, state and overrides removed")
    else:
        for path in _secrets_laid_out(layout):
            if not layout.is_project_path(path):
                raise FailClosed(f"refusing to delete a path outside the project prefixes: {path}")
            discard(path)
        for path in (layout.cli_entry, layout.libexec, layout.share):
            discard(path)
        report.add("kept /etc/mihomo-proxy-management/overrides.d (non-secret user overrides)")
        report.add("kept user/group and journal history")

    report.data["removed"] = [HOST.text(os.path.relpath(p, layout.root)) for p in removed]
    report.data["removed_count"] = len(removed)
    report.data["dry_run"] = ctx.dry_run

    advisory = _foreign_state_advisories(ctx)
    if advisory:
        report.warnings.extend(advisory)
        report.warn("left untouched: routes/firewall/DNS entries we did not create")
    return report


def _foreign_state_advisories(ctx: Context) -> list[str]:
    """Report (never delete) routing/DNS state that is not ours to own."""
    notes: list[str] = []
    if os.path.exists("/dev/net/tun"):
        notes.append(
            "if mihomo auto-route left routes behind, review `ip route` manually; "
            "this project never edits routing directly (D4=A) and will not delete unknown state"
        )
    if os.path.islink("/etc/resolv.conf"):
        notes.append("/etc/resolv.conf is a symlink; systemd-resolved was not touched")
    return notes


# ---- preflight command ------------------------------------------------------


def run_preflight(ctx: Context) -> Report:
    report = Report(command="preflight", status="OK")
    result = ctx.preflight(strict=False)
    report.data = result.to_dict()
    lock = None
    try:
        lock = _load_lock(ctx)
        report.data["lock"] = lock.summary()
    except FailClosed as exc:
        report.warn(str(exc))
    report.status = "OK" if result.supported else "FAILED"
    if not result.supported:
        report.exit_code = ExitCode.UNSUPPORTED
    if result.supported and result.mode != "tun":
        report.status = "DEGRADED"
        report.add("TUN prerequisites unmet; install would run in mixed-port mode")
    for reason in result.reasons:
        report.warn(reason)
    for reason in result.degraded_reasons:
        report.warn("TUN degraded: " + reason)
    return report


__all__ = [
    "Context",
    "Report",
    "SUBCOMMANDS",
    "configure",
    "install",
    "run_preflight",
    "run_test",
    "start",
    "status",
    "stop",
    "restart",
    "uninstall",
    "update_subscription",
]
