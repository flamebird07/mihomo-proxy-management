"""``mpm-switch``: move the machine between Nextin and this project.

Both directions are fail-safe: whatever goes wrong, the machine must end up
with Nextin connected (the state that always has Internet), never with two
network stacks fighting.

``mpm-switch mihomo`` (ordinary user)
  1. ask for the macOS administrator password and run the root phase through
     ``osascript``.  **Nothing else happens in this phase**: if the user
     cancels the dialog, Nextin has not been touched at all and the command
     simply exits non-zero;
  2. the password dialog runs a small shell wrapper (see below) so a crashed
     or ``SIGKILL``-ed root phase still rolls back;
  3. if the dialog fails (cancel or exception) the machine is checked: if
     Nextin is not connected and mihomo is confirmed *not* running, Nextin is
     started again; if mihomo may still be running, nothing is started (two
     stacks are worse than one) and the operator is told to run
     ``mpm-switch nextin``.

Root phase of ``switch mihomo``
  1. disconnect Nextin **as the invoking user** (``launchctl asuser <uid>
     sudo -u <user> scutil --nc stop <vpn>``); a root process cannot operate
     or even observe the user's NetworkExtension session, so every status
     poll runs through ``asuser`` too;
  2. conflict check -> ``lifecycle.start`` -> 60 s probe;
  3. every failure converges on one rollback: ``bootout`` + persistent
     ``disable`` (always, even if bootout failed) + disable readback +
     confirm not running + no project-owned orphan core (only listeners
     identified as this project's on 7890/9090, and a 198.19/16 utun while
     a project core process runs) +
     restore the proxy snapshot + start Nextin as the user + wait for
     ``(Connected)``.  Nextin is only started on a fully confirmed stop.

Shell wrapper (``SIGKILL`` safety)
  The password dialog executes::

      <cli> switch mihomo --user U --uid N --vpn-name V --marker <tmp>; rc=$?;
      [ -f <tmp> ] || <cli> switch rollback --user U --uid N --vpn-name V; exit $rc

  The marker is written by the Python root phase *only* after the probe
  passed, so a missing marker means "roll back", no matter how the phase died.
  ``switch rollback`` is idempotent.  Every argument is ``shlex.quote``-d and
  the AppleScript string escaping is kept.

``mpm-switch nextin``
  privileged ``<cli> stop`` (bootout + disable + proxy restore), then start the
  VPN as the user, wait for ``(Connected)`` and run a short probe.  Nextin is
  not started while mihomo may still be running.

All commands go through the injected executor; tests replace ``scutil``,
``launchctl``, ``osascript`` and the probe.
"""

from __future__ import annotations

import getpass
import os
import shlex
import sys
import tempfile
from typing import Callable, Sequence, TextIO

from mpm_common.errors import ExitCode, FailClosed, MpmError, NotReady
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST, sanitize

from . import conflict as conflict_mod
from . import lifecycle, probe as probe_mod
from .launchdctl import Launchd

DEFAULT_VPN_NAME = "Nextin"
VPN_NAME_ENV = "MPM_VPN_NAME"

POLL_ATTEMPTS = 30
POLL_INTERVAL = 1.0


def _reason(exc: BaseException) -> str:
    detail = exc.message if isinstance(exc, MpmError) else (str(exc) or type(exc).__name__)
    return HOST.text(detail)[:200]


# ---- VPN primitives -------------------------------------------------------


def resolve_vpn_name(environ: dict[str, str] | None = None) -> str:
    values = environ if environ is not None else dict(os.environ)
    return (values.get(VPN_NAME_ENV) or DEFAULT_VPN_NAME).strip() or DEFAULT_VPN_NAME


def connected_vpns(executor: Executor) -> list[conflict_mod.VpnConnection]:
    result = executor.run(["scutil", "--nc", "list"])
    if not result.ok:
        raise RuntimeError(
            "cannot read VPN state: "
            + HOST.text((result.stderr or result.stdout or "").strip())[:120]
        )
    return conflict_mod.parse_scutil_nc_list(result.stdout)


def connected_vpns_as_user(
    executor: Executor, *, uid: int, user: str
) -> list[conflict_mod.VpnConnection]:
    """Connected VPNs as seen from the *user's* GUI session.

    A root process cannot reliably read the user's NetworkExtension sessions:
    its own ``scutil --nc list`` may show nothing (or stale state) while the
    user's VPN is actually up.  Every root-phase status check therefore goes
    through ``launchctl asuser`` exactly like ``start``/``stop`` do.
    """
    result = executor.run(_as_user_argv(uid, user, "scutil", "--nc", "list"))
    if not result.ok:
        raise RuntimeError(
            f"cannot read VPN state as {user}: "
            + HOST.text((result.stderr or result.stdout or "").strip())[:120]
        )
    return conflict_mod.parse_scutil_nc_list(result.stdout)


def vpn_is_connected(executor: Executor, name: str) -> bool:
    return any(item.name == name for item in connected_vpns(executor))


def vpn_is_connected_as_user(
    executor: Executor, name: str, *, uid: int, user: str
) -> bool:
    return any(
        item.name == name
        for item in connected_vpns_as_user(executor, uid=uid, user=user)
    )


def _poll(
    predicate: Callable[[], bool],
    *,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> bool:
    for index in range(max(1, attempts)):
        if predicate():
            return True
        if index + 1 < attempts:
            sleeper(interval)
    return predicate()


def _scutil_nc(executor: Executor, action: str, name: str) -> None:
    """Issue ``scutil --nc <action> <name>`` and fail on a non-zero return code.

    The polling below would eventually report a failure too, but with a
    misleading "still connected" message; the return code says what actually
    went wrong (permissions, unknown service, ...).
    """
    result = executor.run(["scutil", "--nc", action, name])
    if not result.ok:
        detail = HOST.text((result.stderr or result.stdout or "").strip())[:160]
        raise RuntimeError(f"scutil --nc {action} failed (rc={result.returncode}): {detail}")


def start_vpn(
    executor: Executor,
    name: str,
    *,
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_INTERVAL,
    sleeper: Callable[[float], None],
) -> bool:
    """``scutil --nc start`` (never deletes or reconfigures the VPN)."""
    _scutil_nc(executor, "start", name)
    return _poll(
        lambda: vpn_is_connected(executor, name),
        attempts=attempts,
        interval=interval,
        sleeper=sleeper,
    )


def _as_user_argv(uid: int, user: str, *command: str) -> list[str]:
    """Run a command in the logged-in user's session from a root process."""
    return ["launchctl", "asuser", str(int(uid)), "sudo", "-u", user, *command]


def start_vpn_as_user(
    executor: Executor,
    *,
    name: str,
    uid: int,
    user: str,
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_INTERVAL,
    sleeper: Callable[[float], None],
) -> bool:
    result = executor.run(_as_user_argv(uid, user, "scutil", "--nc", "start", name))
    if not result.ok:
        detail = HOST.text((result.stderr or result.stdout or "").strip())[:160]
        raise RuntimeError(f"could not start {name} as {user}: {detail}")
    # Polling reads the user's session too: a root-side ``scutil --nc list``
    # cannot see the NetworkExtension and would report "not connected" for a
    # VPN that is still coming up.
    return _poll(
        lambda: vpn_is_connected_as_user(executor, name, uid=uid, user=user),
        attempts=attempts,
        interval=interval,
        sleeper=sleeper,
    )


def stop_vpn_as_user(
    executor: Executor,
    *,
    name: str,
    uid: int,
    user: str,
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_INTERVAL,
    sleeper: Callable[[float], None],
) -> bool:
    """Disconnect Nextin from the root phase, in the user's session.

    The NetworkExtension belongs to the user; neither ``scutil --nc stop`` nor
    the status polling may run as plain root, so both are wrapped in
    ``launchctl asuser``.  Polling root's own list would instantly claim
    "disconnected" while the user's session is still tearing down - the
    dual-stack window this project exists to close.
    """
    result = executor.run(_as_user_argv(uid, user, "scutil", "--nc", "stop", name))
    if not result.ok:
        detail = HOST.text((result.stderr or result.stdout or "").strip())[:160]
        raise RuntimeError(f"could not stop {name} as {user}: {detail}")
    return _poll(
        lambda: not vpn_is_connected_as_user(executor, name, uid=uid, user=user),
        attempts=attempts,
        interval=interval,
        sleeper=sleeper,
    )


def core_may_be_running(executor: Executor) -> bool:
    """Whether *this project's* mihomo may still own routing, without root.

    ``launchctl print system/<label>`` is not guaranteed to be readable by an
    ordinary user, so this looks at the project ports and at utuns carrying a
    198.19/16 address - but only resources *confirmed* to belong to the
    project count: a listener must be the launchd PID or run the fixed
    executable path (Nextin's ``ProxyTunnel`` is foreign), and a 198.19/16
    utun counts only while a project core process is alive.  A broken probe
    or an unidentifiable owner still answers "may be running": never risk a
    second stack because the check itself failed.  Return codes are checked:
    only ``lsof``'s "nothing listens" exit (1, empty output) counts as a
    confirmed "not running"; every other unknown failure assumes running.
    """
    try:
        job_pid = conflict_mod.launchd_pid(executor)
        for port in conflict_mod.PROJECT_PORTS:
            result = executor.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"]
            )
            if result.returncode not in (0, 1):
                # lsof exits 1 when nothing listens; anything else is unknown.
                return True
            for owner in conflict_mod.parse_lsof_listeners(result.stdout or "", port):
                identity = conflict_mod.port_owner_identity(
                    executor, owner, job_pid=job_pid
                )
                if identity != "foreign":
                    # ours or unidentifiable: assume the worst
                    return True
        ifconfig_result = executor.run(["ifconfig"])
        if ifconfig_result.returncode != 0:
            return True
        addresses = conflict_mod.parse_utun_addresses(ifconfig_result.stdout or "")
        for _name, address in addresses:
            if conflict_mod.in_project_block(address):
                # a 198.19/16 utun is ours only while a project core process runs
                return conflict_mod.project_core_state(
                    executor, job_pid=job_pid
                ) != "stopped"
    except Exception:  # noqa: BLE001 - unknown means "assume running"
        return True
    return False


# ---- privileged hop -------------------------------------------------------


def _applescript(shell_command: str) -> str:
    escaped = shell_command.replace("\\", "\\\\").replace('"', '\\"')
    return f'do shell script "{escaped}" with administrator privileges'


def shell_wrapper(
    cli: str,
    arguments: Sequence[str],
    *,
    marker: str,
    rollback_arguments: Sequence[str],
) -> str:
    """Fail-safe shell wrapper executed by the password dialog.

    The marker only exists after the probe passed, so ``[ -f marker ] ||``
    runs the idempotent rollback when the root phase crashed, was killed, or
    exited non-zero.  ``exit $rc`` keeps the original verdict.
    """
    command = shlex.join([cli, *arguments])
    rollback = shlex.join([cli, *rollback_arguments])
    return (
        f"{command}; rc=$?; "
        f"[ -f {shlex.quote(marker)} ] || {rollback}; "
        f"exit $rc"
    )


def run_privileged(
    executor: Executor,
    cli: str,
    *arguments: str,
    runner: Callable[[str], int] | None = None,
    emit: Callable[[str], None] | None = None,
    shell_command: str | None = None,
) -> int:
    """Run ``<cli> ...`` as root through the macOS password dialog."""
    command = shell_command if shell_command is not None else shlex.join([cli, *arguments])
    if runner is not None:
        return int(runner(command))
    result = executor.run(["osascript", "-e", _applescript(command)])
    if emit is not None:
        for line in (result.stdout or "").splitlines():
            if line.strip():
                emit(line)
        for line in (result.stderr or "").splitlines():
            if line.strip():
                emit("[stderr] " + line)
    return int(result.returncode)


def _new_marker() -> str:
    handle, path = tempfile.mkstemp(prefix="mpm-switch-", suffix=".ok")
    os.close(handle)
    # The marker must only exist after a *successful* root phase; a pre-created
    # file would make the shell wrapper skip the rollback.
    os.unlink(path)
    return path


def _write_marker(path: str | None) -> None:
    if not path:
        return
    # O_EXCL: never truncate an existing file, and a marker someone pre-created
    # must not be mistaken for "the probe passed" (that would skip the rollback).
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("ok\n")


def _remove_marker(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


# ---- status ---------------------------------------------------------------


def _uid_of(ctx: lifecycle.Context) -> int:
    getter = getattr(os, "getuid", None)
    value = int(getter()) if getter is not None else 0
    if value == 0:
        # root launched through osascript: the invoking user is passed
        # explicitly, with SUDO_UID as a fallback.
        try:
            return int(os.environ.get("SUDO_UID") or 0)
        except ValueError:
            return 0
    return value


def _user_of() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry in some sandboxes
        return ""


def _default_interface(executor: Executor) -> str:
    return conflict_mod.parse_default_interface(
        executor.run(["route", "-n", "get", "default"]).stdout
    )


def _system_proxy_summary(ctx: lifecycle.Context) -> list[str]:
    try:
        proxy = ctx.proxy_client()
        lines: list[str] = []
        for entry in proxy.list_services():
            if entry.disabled:
                continue
            current = proxy.read_service(entry)
            if current.web.enabled:
                lines.append(f"{entry.name}: {current.web.server}:{current.web.port}")
            else:
                lines.append(f"{entry.name}: off")
        return lines
    except Exception as exc:  # noqa: BLE001 - status is best effort
        return ["unavailable (" + HOST.text(str(exc))[:80] + ")"]


def status(ctx: lifecycle.Context) -> lifecycle.Report:
    report = lifecycle.Report(command="switch")
    name = resolve_vpn_name(ctx.environ)
    try:
        connections = connected_vpns(ctx.executor)
    except Exception as exc:  # noqa: BLE001 - status must still print something
        connections = []
        report.warn("cannot read VPN state: " + HOST.text(str(exc))[:160])
    launchd = Launchd(ctx.executor, plist=ctx.layout.plist, sleeper=ctx.sleeper)
    state = launchd.state()
    try:
        disabled = launchd.is_disabled()
    except Exception:  # noqa: BLE001
        disabled = None

    vpn_lines = [f"{item.name} ({item.bundle})" if item.bundle else item.name for item in connections]
    report.data = {
        "vpn_name": name,
        "vpn_connected": [item.to_dict() for item in connections],
        "mihomo": {
            "loaded": state.loaded,
            "running": state.running,
            "disabled": disabled,
        },
        "default_interface": _default_interface(ctx.executor),
        "system_proxy": _system_proxy_summary(ctx),
    }
    report.add("Nextin: " + (", ".join(vpn_lines) if vpn_lines else "not connected"))
    report.add(
        "mihomo: "
        + ("running" if state.running else ("loaded" if state.loaded else "stopped"))
        + (" (disabled at boot)" if disabled else "")
    )
    report.add("default route: " + (report.data["default_interface"] or "unknown"))
    for line in report.data["system_proxy"]:
        report.add("system proxy " + line)
    report.status = "OK"
    return report


# ---- rollback -------------------------------------------------------------


def _root_rollback(
    ctx: lifecycle.Context, *, name: str, uid: int, user: str
) -> bool:
    """Leave the machine with Nextin in charge.

    Returns ``True`` only when mihomo is confirmed stopped *and* disabled
    (structured :class:`~mpm_macos.lifecycle.StopOutcome`: disable read back,
    job stopped, no listener owned by the project on 7890/9090 and no
    198.19/16 utun held while a project core process runs) and Nextin is
    connected.  Any unknown or failed check refuses to start Nextin: that is
    the dual-stack state this project exists to avoid.
    """
    stopped = False
    outcome_errors: list[str] = []
    try:
        stopped_report = lifecycle.stop(ctx, quiet=True)
        outcome = stopped_report.data.get("stop_outcome") or {}
        stopped = bool(outcome.get("safe")) and stopped_report.status != "FAILED"
        outcome_errors = [str(item) for item in outcome.get("errors") or []]
        for warning in stopped_report.warnings:
            ctx.err.write(sanitize("[WARN] stop: " + warning + "\n"))
    except Exception as exc:  # noqa: BLE001 - keep going: VPN is the priority
        ctx.err.write(
            sanitize("[WARN] stop during rollback failed: " + _reason(exc)) + "\n"
        )
    if not stopped:
        for detail in outcome_errors[:4]:
            ctx.err.write(sanitize("[FAIL] stop not confirmed: " + detail + "\n"))
        ctx.err.write(
            sanitize(
                f"[FAIL] mihomo is not confirmed stopped; not starting {name}\n"
            )
        )
        ctx.err.write(
            sanitize(
                "[FAIL] run `mpm-switch nextin` once mihomo has stopped, or "
                "resolve the leftover core manually\n"
            )
        )
        return False
    try:
        ok = start_vpn_as_user(
            ctx.executor,
            name=name,
            uid=uid,
            user=user,
            sleeper=ctx.sleeper,
        )
    except Exception as exc:  # noqa: BLE001 - report instead of crashing the rollback
        ok = False
        ctx.err.write(sanitize("[WARN] reconnect failed: " + _reason(exc) + "\n"))
    if ok:
        ctx.say("已自动切回 " + name)
    else:
        ctx.err.write(sanitize(f"[FAIL] could not reconnect {name}; open it manually\n"))
    return ok


def rollback(
    ctx: lifecycle.Context,
    *,
    uid: int | None = None,
    user: str | None = None,
    vpn_name: str | None = None,
) -> lifecycle.Report:
    """Internal ``switch rollback``: idempotent, safe to run at any time."""
    report = lifecycle.Report(command="switch")
    name = vpn_name or resolve_vpn_name(ctx.environ)
    uid_value = _uid_of(ctx) if uid is None else int(uid)
    user_value = _user_of() if user is None else user
    if _root_rollback(ctx, name=name, uid=uid_value, user=user_value):
        report.status = "OK"
        report.add(f"{name} is connected; mihomo is stopped")
    else:
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        report.warn("rollback incomplete; run `mpm-switch nextin`")
    return report


# ---- switch: mihomo -------------------------------------------------------


def switch_to_mihomo_root(
    ctx: lifecycle.Context,
    *,
    uid: int | None = None,
    user: str | None = None,
    vpn_name: str | None = None,
    marker: str | None = None,
    probe_runner: Callable[[], probe_mod.ProbeReport] | None = None,
    force_fail: bool | None = None,
    probe_window: float = probe_mod.WINDOW_SECONDS,
    probe_interval: float = probe_mod.ROUND_INTERVAL,
) -> lifecycle.Report:
    """Root phase: disconnect Nextin -> conflict check -> start -> probe.

    Every failure - including a conflict-check exception - converges on
    :func:`_root_rollback`; nothing can return while mihomo is half-started.
    """
    report = lifecycle.Report(command="switch")
    name = vpn_name or resolve_vpn_name(ctx.environ)
    uid_value = _uid_of(ctx) if uid is None else int(uid)
    user_value = _user_of() if user is None else user
    default_code = int(ExitCode.NOT_READY)
    started: lifecycle.Report | None = None

    try:
        # 1. Nextin first: the user's NetworkExtension can only be operated
        #    from the user's session.  If this fails, mihomo is not touched.
        report.add("disconnecting " + name)
        if not stop_vpn_as_user(
            ctx.executor,
            name=name,
            uid=uid_value,
            user=user_value,
            sleeper=ctx.sleeper,
        ):
            raise FailClosed(
                f"{name} is still connected; aborted before starting mihomo"
            )

        # 2. conflict check (inside the guarded region: a broken detector must
        #    still roll back instead of returning early).
        verdict = ctx.conflicts()
        if not verdict.ok:
            raise FailClosed(conflict_mod.describe(verdict))

        # 3. start
        started = lifecycle.start(ctx, quiet=True)
        report.data["start"] = started.status
        if started.status == "FAILED":
            default_code = int(started.exit_code or ExitCode.NOT_READY)
            raise NotReady("; ".join(started.warnings) or "start failed")

        # 4. probe against this start's own utun interfaces
        utuns = tuple(started.data.get("new_utun_interfaces") or ())
        probe_report = (
            probe_runner()
            if probe_runner is not None
            else probe_mod.run_probe(
                ctx.executor,
                window=probe_window,
                interval=probe_interval,
                sleeper=ctx.sleeper,
                mihomo_utuns=utuns,
                force_fail=force_fail,
            )
        )
        report.data["probe"] = probe_report.to_dict()
        for line in probe_report.lines():
            report.add(line)
        if not probe_report.ok:
            raise NotReady("probe failed: " + (probe_report.reason or "unknown"))

        try:
            # Informational only, and bounded by the same probe deadline so a
            # slow trace cannot stretch the switch past its window.
            location = probe_mod.exit_location(ctx.executor, deadline=probe_report.deadline)
        except Exception:  # noqa: BLE001 - the exit location is informational
            location = ""
        if location:
            report.add("exit: " + location)
            report.data["exit_location"] = location

        # 5. only now may the wrapper skip the rollback
        _write_marker(marker)
    except Exception as exc:  # noqa: BLE001 - every failure ends in rollback
        report.status = "FAILED"
        report.exit_code = int(getattr(exc, "exit_code", 0) or default_code)
        report.warn("switch failed: " + _reason(exc))
        if started is not None:
            for warning in started.warnings:
                report.warn(warning)
        if not _root_rollback(ctx, name=name, uid=uid_value, user=user_value):
            report.warn("rollback incomplete; run `mpm-switch nextin` once mihomo stops")
        return report

    report.status = "OK"
    report.add("mihomo is in charge; probe passed")
    return report


def _recover_after_failed_hop(
    ctx: lifecycle.Context,
    report: lifecycle.Report,
    *,
    name: str,
    attempts: int,
    interval: float,
) -> lifecycle.Report:
    """The password phase failed: make sure the machine is not left without VPN."""
    running = core_may_be_running(ctx.executor)
    try:
        connected = vpn_is_connected(ctx.executor, name)
    except Exception as exc:  # noqa: BLE001 - cannot verify -> do not start anything
        report.warn("cannot read VPN state: " + _reason(exc))
        connected = False
    if connected:
        report.add(name + " is connected")
        return report

    report.warn(f"{name} is not connected; run `mpm-switch nextin` to switch back")
    if running:
        report.warn(
            "mihomo may still be running; not starting "
            + name
            + " to avoid two network stacks"
        )
        return report
    try:
        started = start_vpn(
            ctx.executor,
            name,
            attempts=attempts,
            interval=interval,
            sleeper=ctx.sleeper,
        )
    except Exception as exc:  # noqa: BLE001
        started = False
        report.warn("could not start " + name + ": " + _reason(exc))
    if started:
        report.add("已自动切回 " + name)
    else:
        report.warn("could not reconnect " + name + "; open it manually")
    return report


def switch_to_mihomo(
    ctx: lifecycle.Context,
    *,
    privileged: Callable[[str], int] | None = None,
    probe_runner: Callable[[], probe_mod.ProbeReport] | None = None,
    force_fail: bool | None = None,
    uid: int | None = None,
    user: str | None = None,
    vpn_name: str | None = None,
    marker: str | None = None,
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_INTERVAL,
) -> lifecycle.Report:
    """User phase: password dialog only; Nextin is left alone until root acts."""
    if ctx.geteuid() == 0:
        return switch_to_mihomo_root(
            ctx,
            uid=uid,
            user=user,
            vpn_name=vpn_name,
            marker=marker,
            probe_runner=probe_runner,
            force_fail=force_fail,
        )

    report = lifecycle.Report(command="switch")
    name = vpn_name or resolve_vpn_name(ctx.environ)
    uid_value = _uid_of(ctx) if uid is None else int(uid)
    user_value = _user_of() if user is None else user

    drill = (
        force_fail
        if force_fail is not None
        else os.environ.get("MPM_PROBE_FORCE_FAIL") == "1"
    )
    marker_path = marker or _new_marker()
    arguments = [
        "switch",
        "mihomo",
        "--user",
        user_value,
        "--uid",
        str(uid_value),
        "--vpn-name",
        name,
        "--marker",
        marker_path,
    ]
    if drill:
        arguments.append("--drill-fail")
    rollback_arguments = [
        "switch",
        "rollback",
        "--user",
        user_value,
        "--uid",
        str(uid_value),
        "--vpn-name",
        name,
    ]
    shell_command = shell_wrapper(
        ctx.layout.cli_entry,
        arguments,
        marker=marker_path,
        rollback_arguments=rollback_arguments,
    )

    output: list[str] = []
    error = ""
    code = 1
    try:
        code = run_privileged(
            ctx.executor,
            ctx.layout.cli_entry,
            *arguments,
            runner=privileged,
            emit=output.append,
            shell_command=shell_command,
        )
    except Exception as exc:  # noqa: BLE001 - an osascript failure is a failed hop
        code = 1
        error = _reason(exc)
    for line in output:
        report.add(line)

    succeeded = code == 0 and os.path.isfile(marker_path)
    _remove_marker(marker_path)
    if succeeded:
        report.status = "OK"
        report.add("mihomo is in charge (probe passed in the privileged phase)")
        return report

    report.status = "FAILED"
    report.exit_code = int(ExitCode.NOT_READY)
    if error:
        report.warn("privileged phase raised: " + error)
    elif code == 0:
        report.warn(
            "privileged phase returned 0 without the success marker; treating it as failed"
        )
    else:
        report.warn("privileged switch failed; Nextin was not touched by the user phase")
    return _recover_after_failed_hop(
        ctx, report, name=name, attempts=attempts, interval=interval
    )


# ---- switch: nextin -------------------------------------------------------


def switch_to_nextin(
    ctx: lifecycle.Context,
    *,
    privileged: Callable[[str], int] | None = None,
    probe_runner: Callable[[], probe_mod.ProbeReport] | None = None,
    uid: int | None = None,
    user: str | None = None,
    vpn_name: str | None = None,
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_INTERVAL,
) -> lifecycle.Report:
    report = lifecycle.Report(command="switch")
    name = vpn_name or resolve_vpn_name(ctx.environ)
    uid_value = _uid_of(ctx) if uid is None else int(uid)
    user_value = _user_of() if user is None else user

    stopped_ok = False
    if ctx.geteuid() == 0:
        try:
            stopped = lifecycle.stop(ctx, quiet=True)
            report.data["stop"] = stopped.status
            outcome = stopped.data.get("stop_outcome") or {}
            stopped_ok = bool(outcome.get("safe")) and stopped.status != "FAILED"
            report.warnings.extend(stopped.warnings)
        except Exception as exc:  # noqa: BLE001 - reconnect Nextin regardless
            report.warn("stop failed: " + _reason(exc))
    else:
        try:
            code = run_privileged(ctx.executor, ctx.layout.cli_entry, "stop", runner=privileged)
        except Exception as exc:  # noqa: BLE001
            code = 1
            report.warn("privileged stop failed: " + _reason(exc))
        stopped_ok = code == 0
        if not stopped_ok:
            report.warn("privileged stop failed; checking whether mihomo still runs")

    if not stopped_ok:
        # A stop that cannot confirm "disabled + stopped + no orphan core" is
        # unknown, and unknown never gets a second network stack.
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        if core_may_be_running(ctx.executor):
            report.warn(
                "mihomo may still be running; not starting "
                + name
                + " (two network stacks are worse than one)"
            )
        else:
            report.warn("mihomo is not confirmed stopped and disabled; not starting " + name)
        report.warn(
            "resolve the leftover core (listeners on ports 7890/9090, a utun with a "
            "198.19/16 address), then run `mpm-switch nextin` again or handle it manually"
        )
        return report

    try:
        started = start_vpn_as_user(
            ctx.executor,
            name=name,
            uid=uid_value,
            user=user_value,
            attempts=attempts,
            interval=interval,
            sleeper=ctx.sleeper,
        )
    except Exception as exc:  # noqa: BLE001
        started = False
        report.warn("could not start " + name + ": " + _reason(exc))
    if not started:
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        report.warn(f"could not connect {name}; open it manually")
        return report

    report.add(name + " is connected")
    try:
        probe_report = (
            probe_runner()
            if probe_runner is not None
            else probe_mod.run_probe(
                ctx.executor,
                window=20.0,
                interval=probe_mod.ROUND_INTERVAL,
                sleeper=ctx.sleeper,
                # Nextin's own fake-ip range is expected here; only mihomo's
                # direction needs the "must be our TUN" guard.
                fake_ip_must_be_mihomo=False,
            )
        )
    except Exception as exc:  # noqa: BLE001 - Nextin is connected; probe is advisory
        report.warn("probe after switching back failed: " + HOST.text(str(exc))[:160])
        report.status = "OK"
        return report
    report.data["probe"] = probe_report.to_dict()
    for line in probe_report.lines():
        report.add(line)
    if not probe_report.ok:
        report.warn("probe after switching back failed: " + (probe_report.reason or ""))
    report.status = "OK"
    return report


# ---- mpm-switch entry point ----------------------------------------------


def _build_context(argv_root: str | None = None) -> lifecycle.Context:
    from . import preflight as preflight_mod
    from .paths import Layout

    layout = Layout(argv_root or os.environ.get("MPM_ROOT", "/"))
    return lifecycle.Context(
        layout=layout,
        facts=preflight_mod.collect_facts(),
        executor=Executor(),
        environ=dict(os.environ),
        system_profile=True,
        python=os.environ.get("MPM_PYTHON") or sys.executable,
    )


def _print_report(ctx: lifecycle.Context, report: lifecycle.Report) -> int:
    for message in report.messages:
        ctx.say(message)
    for warning in report.warnings:
        ctx.err.write(sanitize("[WARN] " + warning) + "\n")
    if report.status == "FAILED":
        return int(report.exit_code or ExitCode.FAILURE)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    context_factory: Callable[[], lifecycle.Context] | None = None,
) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="mpm-switch")
    parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("mihomo", "nextin", "status", "probe", "rollback"),
    )
    parser.add_argument("--root", help="alternate filesystem root (staging/tests only)")
    parser.add_argument("--drill-fail", action="store_true", help="force the probe to fail (rollback drill)")
    parser.add_argument("--user", help="invoking user (root phase)")
    parser.add_argument("--uid", type=int, help="invoking uid (root phase)")
    parser.add_argument("--vpn-name", help="VPN service name (default: Nextin / MPM_VPN_NAME)")
    parser.add_argument(
        "--marker",
        help="success marker written by the root phase (internal; used by the shell wrapper)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    ctx = context_factory() if context_factory is not None else _build_context(args.root)
    if stdout is not None:
        ctx.out = stdout
    if stderr is not None:
        ctx.err = stderr

    if args.action == "status":
        return _print_report(ctx, status(ctx))
    if args.action == "probe":
        probe_report = probe_mod.run_probe(
            ctx.executor, sleeper=ctx.sleeper, force_fail=True if args.drill_fail else None
        )
        for line in probe_report.lines():
            ctx.say(line)
        if not probe_report.ok:
            ctx.err.write(sanitize("[WARN] " + probe_report.reason) + "\n")
            return int(ExitCode.NOT_READY)
        return 0
    if args.action == "mihomo":
        return _print_report(
            ctx,
            switch_to_mihomo(
                ctx,
                probe_runner=None,
                force_fail=True if args.drill_fail else None,
                uid=args.uid,
                user=args.user,
                vpn_name=args.vpn_name,
                marker=args.marker,
            ),
        )
    if args.action == "rollback":
        return _print_report(
            ctx,
            rollback(ctx, uid=args.uid, user=args.user, vpn_name=args.vpn_name),
        )
    return _print_report(
        ctx,
        switch_to_nextin(ctx, uid=args.uid, user=args.user, vpn_name=args.vpn_name),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
