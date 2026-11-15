"""``mpm-switch``: move the machine between Nextin and this project.

Both directions are fail-safe: whatever goes wrong, the machine must end up
with Nextin connected (the state that always has Internet), never with two
network stacks fighting.

``mpm-switch mihomo`` (ordinary user)
  0. **Always On VPN (On Demand) preflight** (2026-11-14): ``scutil --nc show
     <vpn>`` is read in the user's own session *before anything else*.
     ``OnDemandEnabled: TRUE`` means the system reconnects the VPN ~1.3 s after
     a stop, so the switch is refused outright: no stop, no password dialog,
     no proxy change.  An unreadable flag is refused too (fail closed), except
     when scutil reports "No service" and no VPN is connected.  This project
     never modifies Always On VPN (On Demand) - no plist edit, no shortcut, no
     UI scripting;
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
     ``mpm-switch nextin``;
  4. a *timeout* is handled defensively: ``osascript`` runs without a timeout,
     so this should not happen, but if one is reported it says nothing about
     the root child behind it.  The user phase then writes nothing at all (no
     ``scutil --nc start/stop``, no ``launchctl``, no ``networksetup``), only
     reports the read-only state and points at ``mpm-switch nextin``.

Root phase of ``switch mihomo``
  1. re-read Always On VPN (On Demand) as the invoking user and refuse (before
     any stop) when it is enabled or unreadable: the refusal returns directly,
     *not* through the rollback, because nothing has been touched yet;
  2. disconnect Nextin **as the invoking user** (``launchctl asuser <uid>
     sudo -u <user> scutil --nc stop <vpn>``); a root process cannot operate
     or even observe the user's NetworkExtension session, so every status
     poll runs through ``asuser`` too;
  3. wait for the stop to *finish* inside one deadline
     (:data:`STOP_TEARDOWN_SECONDS`, polled every 0.5 s): the state must read
     ``Disconnected`` (``Disconnecting``/``Stopping`` mean the old session is
     still tearing down), no process outside this project may listen on
     7890/9090 any more, and every external listener PID recorded *before* the
     stop (Nextin's ``ProxyTunnel`` and friends) must be gone.  The 2026-11-15
     rollback raced exactly this: the ``(Connected)`` flag went away while
     Nextin's ``ProxyTunnel`` still held 7890 - and a plugin that releases the
     port a moment before it exits is the same race one layer down.  A foreign
     port that is still taken, or a foreign PID that is still alive, is
     fail-closed, never ignored;
  4. require a **stable disconnect window** (default 4 s, clamped to 3-5 s,
     ``MPM_STABLE_DISCONNECT_SECONDS``): the state must read ``Disconnected``
     on every poll of the whole window (not merely "absent from the Connected
     list": ``Disconnecting``/``Stopping``/an unreadable state are not a
     confirmation).  A reconnect inside it is reported as ``Nextin
     reconnected within Ns after stop (Always On VPN / On Demand?)`` and rolls
     back *without* starting mihomo;
  5. conflict check -> ``lifecycle.start`` (whose health gate runs after the
     job is up and before any controller call, re-checking that no external
     VPN came back) -> 60 s probe;
  6. every failure converges on one rollback: ``bootout`` + persistent
     ``disable`` (always, even if bootout failed) + disable readback +
     confirm not running + no project-owned orphan core (only listeners
     identified as this project's on 7890/9090, and a 198.19/16 utun while
     a project core process runs) +
     restore the proxy snapshot + start Nextin as the user + wait for
     ``(Connected)``.  Nextin is only started on a fully confirmed stop.
     The Always On VPN (On Demand) flag is recorded (never changed) in the
     report.

Connecting the VPN (rollback, ``switch nextin``, the user-phase recovery)
  ``scutil --nc start`` is a request, not a command: while the previous
  session is still ``Disconnecting``/``Stopping`` nesessionmanager drops it.
  :func:`connect_vpn` therefore waits for ``Disconnected`` before the first
  start, re-sends it when the state has not reached
  ``Connecting``/``Connected`` within 5 s, and keeps doing that until a total
  60 s deadline, accepting only a ``Connected`` read.

Shell wrapper (``SIGKILL`` safety)
  The password dialog executes::

      <cli> switch mihomo --user U --uid N --vpn-name V --marker <tmp>; rc=$?;
      [ "$rc" -eq 5 ] || [ -f <tmp> ] ||
          <cli> switch rollback --user U --uid N --vpn-name V; exit $rc

  The marker is written by the Python root phase *only* once there is nothing
  to undo - after the probe passed, or after an Always On VPN (On Demand)
  refusal that touched nothing - so a missing marker means "roll back", no
  matter how the phase died.  A refusal additionally exits with
  :data:`REFUSED_EXIT_CODE` (5), so the wrapper skips the rollback even when
  the marker write itself failed; the user phase recognises the code (or the
  refusal marker, or the sentinel in the output) and reports the refusal
  instead of running its recovery.
  ``switch rollback`` is idempotent.  Every argument is ``shlex.quote``-d and
  the AppleScript string escaping is kept.

Privileged hop: no timeout
  One ``osascript`` covers the password dialog *and* the whole root phase, so
  it is started with ``timeout=None``: the executor never kills the authorized
  process, and a killed dialog can therefore never leave an orphan root
  behind.  The 2026-11-15 12:57 failure was exactly the executor's 60 s
  default killing the dialog while the operator was still typing, with the
  root child continuing behind it.
  The operator may cancel the password dialog at any time; ``osascript`` then
  returns non-zero and the ordinary failure path applies (nothing was
  touched).  Every command *inside* the root phase keeps its own bounded
  deadline, so the hop cannot hang forever on a wedged external command.
  A timeout is still handled defensively: should a custom runner return
  :data:`TIMEOUT_EXIT_CODE` or the executor raise ``TimeoutExpired`` anyway,
  the user phase reports ``特权阶段超时，状态未知`` and **fails closed** - no
  ``scutil --nc start/stop``, no ``launchctl``, no ``networksetup``.  A
  read-only snapshot (mihomo listeners/utun, the launchd job, the Nextin
  state) is reported for diagnosis only; the operator runs ``mpm-switch
  nextin``, which does the full fallback behind its own dialog.

``mpm-switch nextin``
  privileged ``<cli> stop`` (bootout + disable + proxy restore), then start the
  VPN as the user, wait for ``(Connected)`` and run a short probe.  Nextin is
  not started while mihomo may still be running.  Only ``scutil --nc start``
  is used; Always On VPN (On Demand) is read for the report and never
  modified.

All commands go through the injected executor; tests replace ``scutil``,
``launchctl``, ``osascript`` and the probe.
"""

from __future__ import annotations

import getpass
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TextIO

from mpm_common.errors import ExitCode, FailClosed, MpmError, NotReady
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST, sanitize

from . import conflict as conflict_mod
from . import lifecycle, probe as probe_mod
from .launchdctl import Launchd, parse_print

DEFAULT_VPN_NAME = "Nextin"
VPN_NAME_ENV = "MPM_VPN_NAME"

#: How long the VPN must stay disconnected after ``scutil --nc stop`` before
#: mihomo may start.  Nextin's Always On VPN (On Demand) reconnects ~1.3 s after
#: the stop, so a short window catches it; the value is clamped to a usable
#: range.
STABLE_DISCONNECT_ENV = "MPM_STABLE_DISCONNECT_SECONDS"
STABLE_DISCONNECT_DEFAULT = 4.0
STABLE_DISCONNECT_RANGE = (3.0, 5.0)
#: Poll interval for the stable window (must be <= 0.5 s).
STABLE_DISCONNECT_POLL = 0.5

#: One budget for "the stop is really finished": the state must read
#: ``Disconnected`` (not ``Disconnecting``/``Stopping``), the NetworkExtension's
#: own listener (Nextin's ``ProxyTunnel`` on 7890) must be gone and 7890/9090
#: must be free.  The 2026-11-15 rollback treated the first "not (Connected)"
#: as "disconnected" and ran the conflict check ~0.3 s after the stop, while
#: ``ProxyTunnel`` only exited ~0.3 s later.
STOP_TEARDOWN_SECONDS = 15.0
STOP_TEARDOWN_POLL = 0.5

#: ``scutil --nc start`` is a request, not a command: while the previous
#: session is still tearing down nesessionmanager logs ``Skip`` and drops it
#: (2026-11-15 12:09:48 rollback).  A start is re-sent when the state has not
#: reached ``Connecting``/``Connected`` after this long, inside one total
#: budget.
START_CONNECT_TIMEOUT = 5.0
START_DEADLINE_SECONDS = 60.0
START_POLL_INTERVAL = 0.5

# ---- privileged hop --------------------------------------------------------
#
# The password dialog and the root phase share one ``osascript`` process.  It
# is run with ``timeout=None`` (see :func:`run_privileged`): no static sum of
# per-command deadlines can keep up with the real call chain, and a wrong sum
# kills the authorized process and leaves an orphan root behind.  Each command
# inside the root phase is bounded on its own by the executor's default
# deadline, and the operator can cancel the dialog at any time.
#
# :data:`TIMEOUT_EXIT_CODE` is kept for the defensive path only: a custom
# runner or an unexpected executor timeout must still fail closed.
#: Return code the executor reports when it killed a command at its deadline
#: (``mpm_common.executor.Executor`` follows the ``timeout(1)`` convention).
TIMEOUT_EXIT_CODE = 124


class PrivilegedTimeout(RuntimeError):
    """The privileged phase reported a timeout; its state is unknown.

    Not expected any more - ``run_privileged`` passes ``timeout=None`` so the
    authorized process is never killed - but a custom runner or an unexpected
    executor timeout must still be treated as *unknown*: the root child may
    have run to completion, or may still be running.
    """


#: Both languages on purpose: the operator may read either.
ON_DEMAND_REFUSAL = (
    "请在 {name} App 里关闭 Always On VPN（scutil 显示为 On Demand）后再切换 / "
    "disable Always On VPN (On Demand) in the {name} app first"
)

#: Exit code of a root-phase refusal that changed nothing (Always On VPN /
#: On Demand preflight).
#: It is distinct from every :class:`~mpm_common.errors.ExitCode` so the shell
#: wrapper can tell "refused, nothing to undo" from "failed, roll back" *without
#: depending on a marker file*: a refusal must not run ``lifecycle.stop`` /
#: ``launchctl disable`` / ``scutil --nc start`` even when the marker could not
#: be written.
REFUSED_EXIT_CODE = 5

#: Stable text of a refusal report.  The user phase scans the privileged
#: output for it, because ``do shell script`` turns a non-zero exit into a
#: generic AppleScript error and the marker write may have failed.
REFUSAL_SENTINEL = "refused before touching anything"


def _reason(exc: BaseException) -> str:
    detail = exc.message if isinstance(exc, MpmError) else (str(exc) or type(exc).__name__)
    return HOST.text(detail)[:200]


# ---- VPN primitives -------------------------------------------------------


def resolve_vpn_name(environ: dict[str, str] | None = None) -> str:
    values = environ if environ is not None else dict(os.environ)
    return (values.get(VPN_NAME_ENV) or DEFAULT_VPN_NAME).strip() or DEFAULT_VPN_NAME


def _as_user_argv(uid: int, user: str, *command: str) -> list[str]:
    """Run a command in the logged-in user's session from a root process."""
    return ["launchctl", "asuser", str(int(uid)), "sudo", "-u", user, *command]


def _session(uid: int | None, user: str | None, *command: str) -> list[str]:
    """``command`` in the invoking user's session when a uid is given."""
    if uid is None:
        return list(command)
    return _as_user_argv(int(uid), user or "", *command)


def _nc_list_text(
    executor: Executor, *, uid: int | None = None, user: str | None = None
) -> str:
    """Raw ``scutil --nc list`` output from the right session."""
    who = f" as {user}" if uid is not None else ""
    result = executor.run(_session(uid, user, "scutil", "--nc", "list"))
    if not result.ok:
        raise RuntimeError(
            f"cannot read VPN state{who}: "
            + HOST.text((result.stderr or result.stdout or "").strip())[:120]
        )
    return result.stdout or ""


def connected_vpns(executor: Executor) -> list[conflict_mod.VpnConnection]:
    return conflict_mod.parse_scutil_nc_list(_nc_list_text(executor))


def connected_vpns_as_user(
    executor: Executor, *, uid: int, user: str
) -> list[conflict_mod.VpnConnection]:
    """Connected VPNs as seen from the *user's* GUI session.

    A root process cannot reliably read the user's NetworkExtension sessions:
    its own ``scutil --nc list`` may show nothing (or stale state) while the
    user's VPN is actually up.  Every root-phase status check therefore goes
    through ``launchctl asuser`` exactly like ``start``/``stop`` do.
    """
    return conflict_mod.parse_scutil_nc_list(
        _nc_list_text(executor, uid=uid, user=user)
    )


def vpn_is_connected(executor: Executor, name: str) -> bool:
    return any(item.name == name for item in connected_vpns(executor))


def vpn_is_connected_as_user(
    executor: Executor, name: str, *, uid: int, user: str
) -> bool:
    return any(
        item.name == name
        for item in connected_vpns_as_user(executor, uid=uid, user=user)
    )


def vpn_state(
    executor: Executor, name: str, *, uid: int | None = None, user: str | None = None
) -> str:
    """``connected``/``connecting``/``disconnecting``/``stopping``/
    ``disconnected``, or ``unknown``.

    ``scutil --nc status`` answers directly; some builds only carry the state
    in ``scutil --nc list`` (``* (Disconnecting)``), which is the fallback.
    ``unknown`` is never a confirmation of anything: an unreadable state must
    not be read as "disconnected" (that is the 2026-11-15 race) nor as
    "connected".
    """
    result = None
    try:
        result = executor.run(_session(uid, user, "scutil", "--nc", "status", name))
    except Exception:  # noqa: BLE001 - an unreadable status is "unknown"
        result = None
    if result is not None and result.ok:
        state = conflict_mod.parse_nc_status(result.stdout or "")
        if state != "unknown":
            return state
    try:
        text = _nc_list_text(executor, uid=uid, user=user)
    except Exception:  # noqa: BLE001
        return "unknown"
    return conflict_mod.parse_scutil_nc_states(text).get(name, "unknown")


def port_listeners(
    executor: Executor, ports: Sequence[int] = conflict_mod.PROJECT_PORTS
) -> list[conflict_mod.PortOwner]:
    """Every listener on ``ports``; an unreadable answer raises.

    ``lsof`` exits 1 when nothing listens - that is "no owner", not an error.
    Any other return code is unknown, and unknown is never "the port is free".
    """
    owners: list[conflict_mod.PortOwner] = []
    for port in ports:
        result = executor.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"])
        if result.returncode not in (0, 1):
            raise RuntimeError(f"lsof failed for port {port} (rc={result.returncode})")
        owners.extend(conflict_mod.parse_lsof_listeners(result.stdout or "", port))
    return owners


def stop_leftovers(
    executor: Executor, ports: Sequence[int] = conflict_mod.PROJECT_PORTS
) -> tuple[str, ...]:
    """Listeners that are *not* this project, as ``port: command (pid)``.

    A listener owned by this project (our launchd PID or the fixed executable
    path) is not a leftover: ``mpm-switch mihomo`` may legitimately run while
    mihomo is already in charge.  Everything else - Nextin's ``ProxyTunnel``
    included - is a leftover, and an owner whose identity cannot be resolved
    counts as one too (unknown is never "the port is free").
    """
    job_pid = conflict_mod.launchd_pid(executor)
    leftovers: list[str] = []
    for owner in port_listeners(executor, ports):
        identity = conflict_mod.port_owner_identity(
            executor, owner, job_pid=job_pid
        )
        if identity == "project":
            continue
        leftovers.append(
            f"{owner.port}: {owner.command or 'unknown'}"
            + (f" (pid {owner.pid})" if owner.pid is not None else "")
        )
    return tuple(leftovers)


def external_listener_pids(
    executor: Executor, ports: Sequence[int] = conflict_mod.PROJECT_PORTS
) -> tuple[int, ...]:
    """PIDs of the listeners on ``ports`` that are not this project's.

    Called *before* ``scutil --nc stop`` so the wait afterwards can prove that
    the NetworkExtension's own process (Nextin's ``ProxyTunnel`` on 7890) has
    exited, not merely that it released the port: a plugin that frees 7890 a
    moment before it dies would otherwise let the conflict check run against a
    still-live network stack.  An unidentifiable owner is treated like a
    foreign one - unknown is never "ours" - and an unreadable port list
    raises, because a stop that cannot be verified must not be confirmed.
    """
    job_pid = conflict_mod.launchd_pid(executor)
    pids: set[int] = set()
    for owner in port_listeners(executor, ports):
        if owner.pid is None:
            continue
        identity = conflict_mod.port_owner_identity(
            executor, owner, job_pid=job_pid
        )
        if identity == "project":
            continue
        pids.add(int(owner.pid))
    return tuple(sorted(pids))


def process_alive(executor: Executor, pid: int) -> bool:
    """Whether ``pid`` still exists.

    ``ps -p <pid>`` prints nothing and/or exits non-zero once the process is
    gone; both count as "exited".  An unreadable answer counts as *alive*:
    unknown is never "the old network stack is gone", or the stop would be
    confirmed while it still runs.
    """
    try:
        result = executor.run(["ps", "-o", "pid=", "-p", str(int(pid))])
    except Exception:  # noqa: BLE001 - unknown is never "exited"
        return True
    if result.returncode != 0:
        return False
    return bool((result.stdout or "").strip())


# ---- Always On VPN (On Demand) --------------------------------------------


@dataclass(frozen=True)
class OnDemandState:
    """``OnDemandEnabled`` read from ``scutil --nc show``.

    ``value`` is ``True``/``False`` when the flag was parsed and ``None`` when
    it could not be read (command failed, line missing, contradictory lines).
    ``service_missing`` records scutil's "No service": the VPN does not exist
    at all, which is the only ``None`` that may still proceed.
    """

    value: bool | None = None
    detail: str = ""
    service_missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "detail": HOST.text(self.detail),
            "service_missing": self.service_missing,
        }


@dataclass(frozen=True)
class OnDemandVerdict:
    """Result of the preflight: an empty ``reason`` means "may proceed"."""

    reason: str = ""
    label: str = "unknown"  # enabled | disabled | unknown

    @property
    def blocked(self) -> bool:
        return bool(self.reason)


def _read_on_demand(executor: Executor, argv: list[str]) -> OnDemandState:
    result = executor.run(argv)
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    # "No service" is recognised from the *merged* output whatever the return
    # code is: it is the single exception that lets the switch continue, so it
    # must not depend on scutil's exit status (a version that reports the
    # condition on stdout while exiting 0 would otherwise be read as "the flag
    # is unreadable" and fail closed).
    service_missing = conflict_mod.is_missing_service_output(text)
    if not result.ok:
        detail = HOST.text(text.strip())[:160] or f"scutil exited {result.returncode}"
        return OnDemandState(None, detail, service_missing=service_missing)
    value = conflict_mod.parse_on_demand_enabled(result.stdout or "")
    return OnDemandState(
        value,
        "" if value is not None else "OnDemandEnabled not reported",
        service_missing=service_missing,
    )


def read_on_demand(executor: Executor, name: str) -> OnDemandState:
    """``scutil --nc show`` in the *current* session (the user phase)."""
    return _read_on_demand(executor, ["scutil", "--nc", "show", name])


def read_on_demand_as_user(
    executor: Executor, name: str, *, uid: int, user: str
) -> OnDemandState:
    """``scutil --nc show`` through ``launchctl asuser`` (the root phase)."""
    return _read_on_demand(
        executor, _as_user_argv(uid, user, "scutil", "--nc", "show", name)
    )


def on_demand_refusal(
    name: str, state: OnDemandState, *, connections: Sequence[Any]
) -> str:
    """Operator message when the switch must not run; ``""`` when it may.

    * ``TRUE`` -> refuse: the system will reconnect the VPN right after the
      stop, so mihomo would race a live NetworkExtension;
    * ``None`` -> refuse (fail closed): an unreadable flag may well be on;
    * ``None`` + "No service" + nothing connected -> allow: the service does
      not exist, so there is nothing that could reconnect behind us;
    * ``FALSE`` -> allow.

    Nothing here ever *changes* Always On VPN (On Demand).
    """
    if state.value is False:
        return ""
    if state.value is True:
        return ON_DEMAND_REFUSAL.format(name=name)
    if state.service_missing and not connections:
        return ""
    detail = state.detail or "OnDemandEnabled could not be read"
    return ON_DEMAND_REFUSAL.format(name=name) + (
        "（无法读取 Always On VPN（On Demand）状态 / "
        "cannot read the Always On VPN (On Demand) state: " + detail + "）"
    )


def _refusal_for(
    name: str, state: OnDemandState, connections: Sequence[Any], failure: str
) -> OnDemandVerdict:
    label = "unknown" if state.value is None else ("enabled" if state.value else "disabled")
    if failure:
        reason = ON_DEMAND_REFUSAL.format(name=name) + (
            "（无法读取 VPN 连接状态 / cannot read VPN state: " + failure + "）"
        )
        return OnDemandVerdict(reason, label)
    return OnDemandVerdict(on_demand_refusal(name, state, connections=connections), label)


def on_demand_gate(executor: Executor, name: str) -> OnDemandVerdict:
    """User-phase preflight (plain ``scutil``: already the user's session)."""
    state = read_on_demand(executor, name)
    try:
        connections: Sequence[Any] = connected_vpns(executor)
        failure = ""
    except Exception as exc:  # noqa: BLE001 - unreadable state is a refusal
        connections, failure = [], _reason(exc)
    return _refusal_for(name, state, connections, failure)


def on_demand_gate_as_user(
    executor: Executor, name: str, *, uid: int, user: str
) -> OnDemandVerdict:
    """Root-phase preflight (``launchctl asuser``: the user's session)."""
    state = read_on_demand_as_user(executor, name, uid=uid, user=user)
    try:
        connections: Sequence[Any] = connected_vpns_as_user(executor, uid=uid, user=user)
        failure = ""
    except Exception as exc:  # noqa: BLE001 - unreadable state is a refusal
        connections, failure = [], _reason(exc)
    return _refusal_for(name, state, connections, failure)


def _on_demand_label(state: OnDemandState) -> str:
    if state.value is True:
        return "enabled"
    if state.value is False:
        return "disabled"
    return "unknown"


def _record_on_demand(
    data: dict[str, Any], reader: Callable[[], OnDemandState]
) -> None:
    """Record the flag for the report; never changes it, never fails."""
    try:
        state = reader()
    except Exception:  # noqa: BLE001 - recording must not break a switch
        data["on_demand"] = "unknown"
        return
    data["on_demand"] = _on_demand_label(state)


# ---- stable disconnect window ---------------------------------------------


def stable_disconnect_seconds(environ: dict[str, str] | None = None) -> float:
    """Stable-window length, clamped to ``[3, 5]`` seconds (default 4)."""
    values = environ if environ is not None else dict(os.environ)
    raw = (values.get(STABLE_DISCONNECT_ENV) or "").strip()
    if not raw:
        return STABLE_DISCONNECT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return STABLE_DISCONNECT_DEFAULT
    low, high = STABLE_DISCONNECT_RANGE
    return min(high, max(low, value))


@dataclass(frozen=True)
class StableWindow:
    """Outcome of the post-stop quiet window."""

    ok: bool
    state: str = "unknown"
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "elapsed": round(self.elapsed, 3),
        }


def wait_stable_disconnect(
    executor: Executor,
    *,
    name: str,
    uid: int,
    user: str,
    duration: float,
    interval: float = STABLE_DISCONNECT_POLL,
    sleeper: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
) -> StableWindow:
    """Whether the VPN stayed *``Disconnected``* for the whole ``duration``.

    Polling reads the *user's* session (``launchctl asuser``); root's own
    ``scutil --nc list`` cannot see the NetworkExtension.  Every poll must
    read ``disconnected``: "absent from the Connected list" is not enough, so
    ``Disconnecting``/``Stopping`` (still tearing down, Always On VPN may come
    back next) and an unreadable state end the window as a failure instead of
    passing it.  ``sleeper``/``clock`` are injected so tests never sleep.
    """
    started = clock()
    deadline = started + max(0.0, float(duration))
    while True:
        state = vpn_state(executor, name, uid=uid, user=user)
        if state != "disconnected":
            return StableWindow(False, state, clock() - started)
        remaining = deadline - clock()
        if remaining <= 0:
            return StableWindow(True, state, clock() - started)
        sleeper(min(float(interval), remaining))


@dataclass(frozen=True)
class StopWait:
    """Outcome of "the VPN stop is finished" (see :func:`wait_vpn_fully_stopped`)."""

    ok: bool
    state: str = "unknown"
    reconnected: bool = False
    leftover: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    watched: tuple[int, ...] = ()
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "reconnected": self.reconnected,
            "leftover": list(self.leftover),
            "pending": list(self.pending),
            "watched": list(self.watched),
            "elapsed": round(self.elapsed, 3),
        }


def wait_vpn_fully_stopped(
    executor: Executor,
    *,
    name: str,
    uid: int,
    user: str,
    watched_pids: Sequence[int] = (),
    deadline_seconds: float = STOP_TEARDOWN_SECONDS,
    interval: float = STOP_TEARDOWN_POLL,
    sleeper: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
) -> StopWait:
    """Whether the stop *finished*, not merely "is no longer Connected".

    ``scutil --nc list`` drops ``(Connected)`` as soon as the tunnel goes
    down, while the NetworkExtension's ``ProxyTunnel`` keeps listening on 7890
    for a moment longer.  The 2026-11-15 rollback checked the ports inside that
    window, saw a foreign listener and failed closed.  A stop is complete only
    when, inside one deadline:

    * the state is ``Disconnected`` - ``Disconnecting``/``Stopping`` mean the
      old session is still tearing down;
    * no process *outside this project* listens on 7890/9090 any more (the
      tunnel plugin exited; a project core that already owns the ports is not
      a leftover);
    * every PID in ``watched_pids`` - the external listeners recorded *before*
      the stop - no longer exists.  A plugin that releases 7890 a moment
      before it exits is the same race one layer down: the port is free while
      the old network stack still runs;
    * all of the above hold for the same poll.

    A VPN that comes back after having been gone is reported as
    ``reconnected`` (Always On VPN / On Demand); a VPN that never leaves is
    reported with its state and no ``reconnected`` flag.  Unreadable answers
    never count as confirmation.
    """
    started = clock()
    deadline = started + max(0.0, float(deadline_seconds))
    watched = tuple(sorted({int(pid) for pid in watched_pids}))
    seen_gone = False
    state = "unknown"
    leftover: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    while True:
        state = vpn_state(executor, name, uid=uid, user=user)
        if state in ("disconnected", "disconnecting", "stopping"):
            seen_gone = True
        elif state == "connected" and seen_gone:
            return StopWait(
                False, state, reconnected=True, watched=watched,
                elapsed=clock() - started,
            )
        try:
            leftover = stop_leftovers(executor)
        except Exception as exc:  # noqa: BLE001 - unknown is not "free"
            leftover = ("port state unreadable: " + _reason(exc),)
        pending = tuple(
            f"pid {pid} still alive" for pid in watched if process_alive(executor, pid)
        )
        if state == "disconnected" and not leftover and not pending:
            return StopWait(True, state, watched=watched, elapsed=clock() - started)
        remaining = deadline - clock()
        if remaining <= 0:
            return StopWait(
                False,
                state,
                leftover=leftover,
                pending=pending,
                watched=watched,
                elapsed=clock() - started,
            )
        sleeper(min(float(interval), remaining))


def stop_vpn_as_user(
    executor: Executor,
    *,
    name: str,
    uid: int,
    user: str,
    deadline_seconds: float = STOP_TEARDOWN_SECONDS,
    interval: float = STOP_TEARDOWN_POLL,
    sleeper: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
) -> StopWait:
    """Disconnect Nextin from the root phase and wait for the teardown.

    The NetworkExtension belongs to the user; neither ``scutil --nc stop`` nor
    the status polling may run as plain root, so both are wrapped in
    ``launchctl asuser``.  The whole wait after the command shares one deadline:
    "the stop command returned" is not "the stop is finished" (see
    :func:`wait_vpn_fully_stopped`).

    The external listeners on 7890/9090 are recorded *before* the stop so the
    wait can also prove that the plugin process itself exited, not just that it
    released the ports.  A port list that cannot be read raises here, before
    anything is stopped: an unverifiable stop is never confirmed.
    """
    try:
        watched = external_listener_pids(executor)
    except Exception as exc:  # noqa: BLE001 - cannot verify -> do not stop
        raise RuntimeError(
            "cannot record the listeners on 7890/9090 before stopping "
            + name
            + ": "
            + _reason(exc)
            + "; nothing was stopped"
        ) from exc
    result = executor.run(_session(uid, user, "scutil", "--nc", "stop", name))
    if not result.ok:
        detail = HOST.text((result.stderr or result.stdout or "").strip())[:160]
        raise RuntimeError(f"could not stop {name} as {user}: {detail}")
    return wait_vpn_fully_stopped(
        executor,
        name=name,
        uid=uid,
        user=user,
        watched_pids=watched,
        deadline_seconds=deadline_seconds,
        interval=interval,
        sleeper=sleeper,
        clock=clock,
    )


def connect_vpn(
    executor: Executor,
    *,
    name: str,
    uid: int | None = None,
    user: str | None = None,
    sleeper: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float = START_DEADLINE_SECONDS,
    connect_timeout: float = START_CONNECT_TIMEOUT,
    interval: float = START_POLL_INTERVAL,
) -> bool:
    """``scutil --nc start`` with a state machine, verified ``Connected``.

    ``scutil --nc start`` is only a request: while the previous session is
    still ``Disconnecting``/``Stopping`` nesessionmanager logs ``Skip`` and
    drops it (2026-11-15 12:09:48 rollback), and a dropped start looks exactly
    like a slow connect.  So:

    * a start is never sent into a teardown - the state must first read
      ``Disconnected`` (or be unreadable, where waiting would never end);
    * if the state has not reached ``Connecting``/``Connected`` within
      ``connect_timeout`` (5 s), ``start`` is sent again;
    * the whole loop is bounded by ``deadline_seconds`` (60 s), and only a
      ``Connected`` read counts as success.

    With ``uid``/``user`` every command runs through ``launchctl asuser``:
    root cannot see the user's NetworkExtension.  ``sleeper``/``clock`` are
    injected, so tests never really sleep.
    """
    deadline = clock() + max(0.0, float(deadline_seconds))
    issued_at: float | None = None
    while True:
        state = vpn_state(executor, name, uid=uid, user=user)
        if state == "connected":
            return True
        now = clock()
        if state in ("disconnected", "unknown") and (
            issued_at is None or now - issued_at >= float(connect_timeout)
        ):
            result = executor.run(_session(uid, user, "scutil", "--nc", "start", name))
            if not result.ok:
                detail = HOST.text((result.stderr or result.stdout or "").strip())[:160]
                who = f" as {user}" if uid is not None else ""
                raise RuntimeError(f"could not start {name}{who}: {detail}")
            issued_at = clock()
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(float(interval), remaining))


def start_vpn(
    executor: Executor,
    name: str,
    *,
    sleeper: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float = START_DEADLINE_SECONDS,
    connect_timeout: float = START_CONNECT_TIMEOUT,
    interval: float = START_POLL_INTERVAL,
) -> bool:
    """Connect in the *current* session (the user phase's recovery path)."""
    return connect_vpn(
        executor,
        name=name,
        sleeper=sleeper,
        clock=clock,
        deadline_seconds=deadline_seconds,
        connect_timeout=connect_timeout,
        interval=interval,
    )


def start_vpn_as_user(
    executor: Executor,
    *,
    name: str,
    uid: int,
    user: str,
    sleeper: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float = START_DEADLINE_SECONDS,
    connect_timeout: float = START_CONNECT_TIMEOUT,
    interval: float = START_POLL_INTERVAL,
) -> bool:
    """Connect from the root phase, in the user's session (see :func:`connect_vpn`)."""
    return connect_vpn(
        executor,
        name=name,
        uid=uid,
        user=user,
        sleeper=sleeper,
        clock=clock,
        deadline_seconds=deadline_seconds,
        connect_timeout=connect_timeout,
        interval=interval,
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

    The marker means "there is nothing to undo": it is written after the probe
    passed, and also by an Always On VPN (On Demand) refusal that never touched
    the machine.
    A missing marker therefore means "roll back" for a crashed, killed or
    non-zero root phase.

    The refusal exit code (:data:`REFUSED_EXIT_CODE`) short-circuits that rule:
    a refusal that changed nothing must not be followed by ``lifecycle.stop``,
    ``launchctl disable`` and ``scutil --nc start`` even when the marker write
    itself failed.  ``exit $rc`` keeps the original verdict.
    """
    command = shlex.join([cli, *arguments])
    rollback = shlex.join([cli, *rollback_arguments])
    return (
        f"{command}; rc=$?; "
        f'[ "$rc" -eq {REFUSED_EXIT_CODE} ] || [ -f {shlex.quote(marker)} ] || {rollback}; '
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
    """Run ``<cli> ...`` as root through the macOS password dialog.

    The dialog stays alive for the password prompt *and* the root phase, so
    ``osascript`` is run with ``timeout=None``: the executor must never kill
    the authorized process.  A static deadline cannot cover the real call
    chain (start, two stops/rollbacks, proxy restore, ...) and killing the
    dialog leaves the root child running as an orphan - the 2026-11-15 12:57
    failure.  Each command inside the root phase keeps its own bounded
    deadline, and the operator can cancel the dialog at any time, which
    ``osascript`` reports as a non-zero exit and the ordinary failure path.

    A timeout is still handled defensively: if a custom runner or an
    unexpected executor timeout reports :data:`TIMEOUT_EXIT_CODE`,
    :class:`PrivilegedTimeout` is raised instead of returning a code -
    "killed" is not "cancelled", because the root child may outlive the
    dialog and the caller has to treat the root phase as unknown.
    """
    command = shell_command if shell_command is not None else shlex.join([cli, *arguments])
    if runner is not None:
        return int(runner(command))
    timed_out_message = (
        "the privileged phase reported a timeout; its state is unknown"
    )
    try:
        result = executor.run(
            ["osascript", "-e", _applescript(command)], timeout=None
        )
    except subprocess.TimeoutExpired as exc:
        raise PrivilegedTimeout(timed_out_message) from exc
    if emit is not None:
        for line in (result.stdout or "").splitlines():
            if line.strip():
                emit(line)
        for line in (result.stderr or "").splitlines():
            if line.strip():
                emit("[stderr] " + line)
    if result.returncode == TIMEOUT_EXIT_CODE:
        raise PrivilegedTimeout(timed_out_message)
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
            clock=ctx.clock,
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
    # Informational only: the rollback never changes Always On VPN (On Demand).
    _record_on_demand(
        report.data,
        lambda: read_on_demand_as_user(
            ctx.executor, name, uid=uid_value, user=user_value
        ),
    )
    return report


# ---- switch: mihomo -------------------------------------------------------


def _refuse(
    report: lifecycle.Report,
    name: str,
    verdict: OnDemandVerdict,
    *,
    exit_code: int = int(ExitCode.FAIL_CLOSED),
) -> lifecycle.Report:
    """Refusal report: nothing was stopped, no dialog, no proxy change."""
    report.status = "FAILED"
    report.exit_code = int(exit_code)
    report.data["on_demand"] = verdict.label
    # The sentinel is part of the *warning* too: warnings are printed to
    # stderr, which is what ``do shell script`` keeps in its error message when
    # the root phase exits non-zero.  That is how the user phase still sees
    # "refused" when the marker write failed and osascript flattened the code.
    report.warn(verdict.reason + "（" + REFUSAL_SENTINEL + "）")
    report.add(
        "已拒绝切换：没有停止 " + name + "、没有弹出密码框、没有修改代理 / "
        + REFUSAL_SENTINEL
    )
    return report


def _post_start_guard(
    ctx: lifecycle.Context, name: str, *, uid: int, user: str
) -> None:
    """Abort a start whose external VPN came back (called from ``lifecycle.start``).

    Runs after launchd reports the job running and before the controller
    readiness/health gate, so a Nextin that Always On VPN (On Demand) brought
    back rolls back immediately instead of after a slow healthcheck.  The
    conflict detector's VPN list is checked too: any connected VPN means two
    stacks.
    """
    connections = connected_vpns_as_user(ctx.executor, uid=uid, user=user)
    if any(item.name == name for item in connections):
        raise NotReady(
            f"{name} reconnected after mihomo start "
            "(Always On VPN / On Demand?); rolling back"
        )
    verdict = ctx.conflicts()
    if verdict.vpn_connected:
        labels = ", ".join(item.name or "VPN" for item in verdict.vpn_connected)
        raise NotReady(
            "external VPN connected after mihomo start: "
            + labels
            + " (Always On VPN / On Demand?); rolling back"
        )


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

    Every failure *after* the Always On VPN (On Demand) preflight - including a
    conflict-check exception - converges on :func:`_root_rollback`; nothing can
    return while mihomo is half-started.  The preflight itself returns a
    refusal report directly: it runs before anything is touched, so there is
    nothing to roll back and no system command may run.
    """
    report = lifecycle.Report(command="switch")
    name = vpn_name or resolve_vpn_name(ctx.environ)
    uid_value = _uid_of(ctx) if uid is None else int(uid)
    user_value = _user_of() if user is None else user
    default_code = int(ExitCode.NOT_READY)
    started: lifecycle.Report | None = None

    # 0. Always On VPN (On Demand) preflight (root side, user's session).  The
    #    user phase already refused, but the flag may have been enabled since,
    #    or the root phase may have been invoked directly.
    #
    #    This runs *before* the protected flow: TRUE (or an unreadable flag)
    #    means the VPN will come back right after the stop, so the switch is
    #    refused while nothing has been touched.  Entering the ``try`` below
    #    would raise into the generic ``_root_rollback`` - lifecycle.stop,
    #    proxy restore, ``launchctl disable`` and ``scutil --nc start`` - all
    #    of which are needless system changes for a refusal that changed
    #    nothing.  The refusal returns here instead.
    gate = on_demand_gate_as_user(
        ctx.executor, name, uid=uid_value, user=user_value
    )
    if gate.blocked:
        refusal = _refuse(
            report, name, gate, exit_code=REFUSED_EXIT_CODE
        )
        # The password-dialog wrapper rolls back when the marker is missing.
        # Nothing was changed, so the marker records "no rollback needed" and
        # keeps the refusal free of stop/disable/start side effects.  The
        # refusal *exit code* is the second half of that protocol: it skips
        # the rollback even if this write fails (a read-only or vanished
        # temporary directory must not turn a refusal into a rollback).
        try:
            _write_marker(marker)
        except Exception as exc:  # noqa: BLE001 - the marker never changes the verdict
            report.warn(
                "could not write the refusal marker ("
                + _reason(exc)
                + "); the wrapper skips the rollback because the exit code is "
                + str(REFUSED_EXIT_CODE)
            )
        return refusal
    report.data["on_demand"] = gate.label

    try:
        # 1. Nextin first: the user's NetworkExtension can only be operated
        #    from the user's session.  If this fails, mihomo is not touched.
        report.add("disconnecting " + name)
        stopped = stop_vpn_as_user(
            ctx.executor,
            name=name,
            uid=uid_value,
            user=user_value,
            sleeper=ctx.sleeper,
            clock=ctx.clock,
        )
        report.data["stop_wait"] = stopped.to_dict()
        if stopped.reconnected:
            raise NotReady(
                f"{name} reconnected {stopped.elapsed:g}s after stop while waiting "
                "for the teardown to finish (Always On VPN / On Demand?)"
            )
        if not stopped.ok:
            # A port still held here (Nextin's ProxyTunnel, or anything else on
            # 7890/9090) - or a listener PID that is still alive - is never
            # ignored: starting mihomo would race it.
            if stopped.state == "connected":
                raise FailClosed(
                    f"{name} is still connected after the stop command; "
                    "aborted before starting mihomo"
                )
            detail = "; ".join(stopped.leftover + stopped.pending) or (
                f"state is {stopped.state}"
            )
            raise FailClosed(
                f"{name} did not finish disconnecting within "
                f"{STOP_TEARDOWN_SECONDS:g}s ({detail}); not starting mihomo"
            )

        # 1b. Stable disconnect window: Always On VPN (On Demand) reconnects
        #     ~1.3 s after the stop, so "the stop finished" is not enough.  The
        #     state must read Disconnected on *every* poll of the window; a
        #     reconnect - or a teardown that is still in progress - rolls back
        #     *before* mihomo exists.
        window = stable_disconnect_seconds(ctx.environ)
        report.add(f"verifying {name} stays disconnected for {window:g}s")
        stable = wait_stable_disconnect(
            ctx.executor,
            name=name,
            uid=uid_value,
            user=user_value,
            duration=window,
            sleeper=ctx.sleeper,
            clock=ctx.clock,
        )
        report.data["stable_window"] = stable.to_dict()
        if not stable.ok:
            if stable.state == "connected":
                raise NotReady(
                    f"{name} reconnected within {window:g}s after stop "
                    "(Always On VPN / On Demand?)"
                )
            raise FailClosed(
                f"{name} was not continuously disconnected during the "
                f"{window:g}s window (state {stable.state} after "
                f"{stable.elapsed:g}s); not starting mihomo"
            )

        # 2. conflict check (inside the guarded region: a broken detector must
        #    still roll back instead of returning early).
        verdict = ctx.conflicts()
        if not verdict.ok:
            raise FailClosed(conflict_mod.describe(verdict))

        # 3. start.  The health guard re-checks the VPN after the job is up and
        #    before the controller health gate (see _post_start_guard).
        started = lifecycle.start(
            ctx,
            quiet=True,
            health_guard=lambda: _post_start_guard(
                ctx, name, uid=uid_value, user=user_value
            ),
        )
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
                clock=ctx.clock,
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
            location = probe_mod.exit_location(
                ctx.executor, deadline=probe_report.deadline, clock=ctx.clock
            )
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
        # Informational only: the flag explains *why* a rollback happened.
        _record_on_demand(
            report.data,
            lambda: read_on_demand_as_user(
                ctx.executor, name, uid=uid_value, user=user_value
            ),
        )
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
            sleeper=ctx.sleeper,
            clock=ctx.clock,
        )
    except Exception as exc:  # noqa: BLE001
        started = False
        report.warn("could not start " + name + ": " + _reason(exc))
    if started:
        report.add("已自动切回 " + name)
    else:
        report.warn("could not reconnect " + name + "; open it manually")
    return report


def launchd_loaded(ctx: lifecycle.Context) -> bool | None:
    """Whether the core job is loaded - read-only, ``None`` when unreadable.

    ``launchctl print system/<label>`` is not guaranteed to be readable by an
    ordinary user, so only "it printed state" and "Could not find service" are
    taken as answers.  Everything else is unknown, and unknown is treated like
    "loaded" by the caller: it may start at any moment.
    """
    launchd = Launchd(ctx.executor, plist=ctx.layout.plist, sleeper=ctx.sleeper)
    try:
        result = ctx.executor.run(launchd.print_argv())
    except Exception:  # noqa: BLE001 - unknown, never "not loaded"
        return None
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return parse_print(text, label=launchd.label, returncode=0).loaded
    if "could not find service" in text.lower():
        return False
    return None


def _recover_after_privileged_timeout(
    ctx: lifecycle.Context,
    report: lifecycle.Report,
    *,
    name: str,
) -> lifecycle.Report:
    """A reported privileged timeout: fail closed, write nothing.

    ``run_privileged`` passes ``timeout=None`` so the dialog is not killed any
    more; this path is defensive.  Should a timeout be reported anyway, it
    says nothing about the root child: the root phase may still be stopping
    Nextin, waiting out the stable window, or about to ``bootstrap`` mihomo.
    No read-only check can prove that it has *finished* -
    ``core_may_be_running`` and :func:`launchd_loaded` are snapshots, and an
    orphan that looks idle now may start mihomo a moment later - so nothing is
    decided from them:

    * no ``scutil --nc start/stop``, no ``launchctl``, no ``networksetup``:
      starting Nextin on a "looks down" snapshot is exactly the dual-stack
      race this project exists to prevent;
    * the report only *describes* what was seen (mihomo, launchd job, Nextin)
      and points at ``mpm-switch nextin``, which stops mihomo behind its own
      password dialog and then reconnects Nextin.
    """
    report.status = "FAILED"
    report.exit_code = int(ExitCode.FAIL_CLOSED)
    report.warn(
        "特权阶段超时，状态未知；请运行 mpm-switch nextin 恢复 Nextin"
        "（会再弹一次密码框） / the privileged phase reported a timeout: its "
        "state is unknown (the root phase may still be running); run "
        "`mpm-switch nextin` to restore "
        + name
        + " (it asks for the password again)"
    )
    # Read-only only: every value below is a snapshot that may be stale a
    # moment later, which is exactly why nothing is decided from it.
    may_run = core_may_be_running(ctx.executor)
    loaded = launchd_loaded(ctx)
    connected: bool | None
    try:
        connected = vpn_is_connected(ctx.executor, name)
    except Exception as exc:  # noqa: BLE001 - unreadable is "unknown", not "down"
        connected = None
        report.warn("cannot read the " + name + " state: " + _reason(exc))
    report.data["privileged_timeout"] = {
        "mihomo_may_run": may_run,
        "launchd_loaded": loaded,
        "nextin_connected": connected,
        "wrote_nothing": True,
    }
    report.add(
        "state after the timeout (read-only): mihomo "
        + ("may be running" if may_run else "was not seen running")
        + "; launchd job "
        + ("unknown" if loaded is None else ("loaded" if loaded else "not loaded"))
        + "; "
        + name
        + " "
        + (
            "connected"
            if connected
            else ("not connected" if connected is False else "unknown")
        )
    )
    report.warn(
        "mihomo may be running: the timeout proves nothing about the root child, so "
        "nothing was started or stopped here; run `mpm-switch nextin` to stop mihomo "
        "and reconnect " + name
    )
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

    # Always On VPN (On Demand) preflight, before the dialog and before
    # anything is touched.  The VPN must not reconnect behind mihomo's back;
    # this project never changes Always On VPN (On Demand) itself, so the
    # operator has to disable it first.
    gate = on_demand_gate(ctx.executor, name)
    if gate.blocked:
        return _refuse(report, name, gate)

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
    timed_out = False
    try:
        code = run_privileged(
            ctx.executor,
            ctx.layout.cli_entry,
            *arguments,
            runner=privileged,
            emit=output.append,
            shell_command=shell_command,
        )
    except PrivilegedTimeout as exc:  # a reported timeout is not a cancel
        code = TIMEOUT_EXIT_CODE
        timed_out = True
        error = _reason(exc)
    except Exception as exc:  # noqa: BLE001 - an osascript failure is a failed hop
        code = 1
        error = _reason(exc)
    for line in output:
        report.add(line)

    marker_present = os.path.isfile(marker_path)
    succeeded = code == 0 and marker_present
    # A custom runner can return the executor's timeout code without raising;
    # either way the root phase must be treated as *unknown*, and a marker left
    # by a root phase that did finish must never be read as the "refused"
    # verdict below (the marker means "there is nothing to undo", not "nothing
    # happened").
    timed_out = timed_out or code == TIMEOUT_EXIT_CODE
    # A refusal that touched nothing must not be "recovered" by starting
    # Nextin.  Three independent signals, because each one can be lost:
    #   * the dedicated exit code (what the wrapper itself sees),
    #   * the marker (a refusal writes it too, when the write succeeds),
    #   * the sentinel in the emitted output (``do shell script`` turns the
    #     exit code into a generic error but keeps stderr).
    refused = not succeeded and not timed_out and (
        code == REFUSED_EXIT_CODE
        or marker_present
        or any(REFUSAL_SENTINEL in line for line in output)
    )
    _remove_marker(marker_path)
    if succeeded:
        report.status = "OK"
        report.add("mihomo is in charge (probe passed in the privileged phase)")
        return report

    report.status = "FAILED"
    report.exit_code = int(ExitCode.NOT_READY)
    if timed_out:
        return _recover_after_privileged_timeout(ctx, report, name=name)
    if refused:
        # Nothing was stopped, no proxy was changed: there is nothing to
        # recover and starting Nextin here would be a needless side effect.
        report.exit_code = int(ExitCode.FAIL_CLOSED)
        report.warn(
            "root 阶段在改动任何东西之前拒绝了切换 / "
            "the privileged phase refused before touching anything"
        )
        report.warn(ON_DEMAND_REFUSAL.format(name=name))
        return report
    if error:
        report.warn("privileged phase raised: " + error)
    elif code == 0:
        report.warn(
            "privileged phase returned 0 without the success marker; treating it as failed"
        )
    else:
        report.warn("privileged switch failed; Nextin was not touched by the user phase")
    return _recover_after_failed_hop(ctx, report, name=name)


# ---- switch: nextin -------------------------------------------------------


def switch_to_nextin(
    ctx: lifecycle.Context,
    *,
    privileged: Callable[[str], int] | None = None,
    probe_runner: Callable[[], probe_mod.ProbeReport] | None = None,
    uid: int | None = None,
    user: str | None = None,
    vpn_name: str | None = None,
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
        except PrivilegedTimeout as exc:
            # Same rule as the switch direction: a reported timeout says
            # nothing about the root child, so the stop is unknown - never
            # confirmed.
            code = TIMEOUT_EXIT_CODE
            report.warn(
                "特权阶段超时，状态未知 / the privileged stop reported a timeout: "
                "its state is unknown (" + _reason(exc) + ")"
            )
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
            sleeper=ctx.sleeper,
            clock=ctx.clock,
        )
    except Exception as exc:  # noqa: BLE001
        started = False
        report.warn("could not start " + name + ": " + _reason(exc))
    if not started:
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        report.warn(f"could not connect {name}; open it manually")
        _record_on_demand(report.data, lambda: read_on_demand(ctx.executor, name))
        return report

    report.add(name + " is connected")
    # Recorded only: switching back uses plain ``scutil --nc start`` and never
    # touches Always On VPN (On Demand) - no plist, no shortcut, no UI scripting.
    _record_on_demand(report.data, lambda: read_on_demand(ctx.executor, name))
    try:
        probe_report = (
            probe_runner()
            if probe_runner is not None
            else probe_mod.run_probe(
                ctx.executor,
                window=20.0,
                interval=probe_mod.ROUND_INTERVAL,
                sleeper=ctx.sleeper,
                clock=ctx.clock,
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
