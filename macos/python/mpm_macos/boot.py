"""Boot entry: the ``com.mihomo-proxy-management.boot-switch`` LaunchDaemon.

The core job stays disabled at boot (``KeepAlive{SuccessfulExit: false}``
implies ``RunAtLoad``, so only the persistent ``launchctl disable`` override
keeps mihomo off until somebody asks for it).  This job is the single boot
entry and it does **not** start mihomo directly: it waits for the machine to
be usable and then runs the very same root flow as ``mpm-switch mihomo``.

Order of operations (every step before 3 is read-only):

0. **hold stamp** (``state/boot-switch.hold``, 0600): when it exists the
   entry writes the 0600 ack file (``state/boot-switch.hold-ack``) - proof
   for ``install`` that this RunAtLoad run has seen the hold - and exits
   immediately with zero side effects.  ``install`` creates the stamp before
   ``launchctl bootstrap`` - ``RunAtLoad`` starts this entry right after the
   bootstrap, and without the hold that run would perform the whole network
   switch while the operator watches.  Install deletes the stamp and the ack
   only after the job is loaded, the ack is present and the entry is no
   longer running; the next run (a real boot or a manual kickstart) then
   performs the switch again.
1. **wait for the console login user** (default 180 s).  ``/dev/console`` is
   owned by ``root``/``_mbsetupuser`` while nobody is logged in; a switch
   started then would run against a session that does not exist yet.  Timeout
   means *do nothing at all*: no mihomo start, no Nextin start - a login-window
   boot must not be turned into a network change.
2. **wait for a default route** (default 180 s).  Starting a TUN stack before
   the physical interface is up produces a mihomo that cannot reach its
   subscription.  Timeout is again zero side effects.
3. **already in charge?**  mihomo running and Nextin not connected means there
   is nothing to do; re-running the flow would bounce a working core and cut
   the network for no reason.
4. **the switch itself**: :func:`mpm_macos.switch.switch_to_mihomo_root` - the
   Always On VPN (On Demand) preflight (``TRUE`` or unreadable refuses before
   touching anything), Nextin teardown with the port/PID wait, the stable
   disconnect window, the conflict check, ``lifecycle.start`` (which disables
   the core again after a successful start), the post-start Nextin re-check,
   the deadline-bounded health gate and the 60 s probe.  Any failure rolls back
   to Nextin with ``scutil --nc start`` only; Always On VPN (On Demand) is
   never modified.

Nothing here opens a password dialog: a LaunchDaemon already runs as root, and
``switch_to_mihomo_root`` is the root phase.  ``--drill-fail`` /
``MPM_PROBE_FORCE_FAIL=1`` force the probe to fail so the rollback can be
verified without a reboot.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, TextIO

from mpm_common.atomicio import write_atomic
from mpm_common.errors import ExitCode
from mpm_common.executor import Executor
from mpm_common.sanitize import sanitize

from . import conflict as conflict_mod
from . import context as context_mod
from . import lifecycle, probe as probe_mod, switch as switch_mod
from . import tun_dns as tun_dns_mod

CONSOLE_DEADLINE_ENV = "MPM_BOOT_CONSOLE_DEADLINE_SECONDS"
NETWORK_DEADLINE_ENV = "MPM_BOOT_NETWORK_DEADLINE_SECONDS"
DEADLINE_DEFAULT = 180.0
DEADLINE_RANGE = (10.0, 900.0)
POLL_INTERVAL = 1.0

#: ``/dev/console`` owner: ``<user> <uid>`` (``stat -f '%Su %u'``).
CONSOLE_DEVICE = "/dev/console"
CONSOLE_STAT_FORMAT = "%Su %u"

#: Accounts that own ``/dev/console`` while nobody is logged in.
NO_CONSOLE_USERS = frozenset({"root", "_mbsetupuser", "loginwindow"})

#: Content of the 0600 ack file the entry writes on the hold exit.  No
#: secrets, no node names: just proof that this RunAtLoad run took the hold.
BOOT_HOLD_ACK_TEXT = "boot entry saw the install hold stamp; zero side effects\n"


@dataclass(frozen=True)
class ConsoleUser:
    """The logged-in console user the switch must run as."""

    name: str
    uid: int

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "uid": self.uid}


def parse_console_owner(text: str) -> ConsoleUser | None:
    """Console owner from ``stat -f '%Su %u' /dev/console`` output.

    ``root``/``_mbsetupuser``/uid 0 mean the login window: there is no user
    session yet, and a switch run in that state would target nobody.
    """
    parts = (text or "").split()
    if len(parts) < 2:
        return None
    name = parts[0]
    try:
        uid = int(parts[1])
    except ValueError:
        return None
    if uid <= 0 or name in NO_CONSOLE_USERS:
        return None
    return ConsoleUser(name=name, uid=uid)


def console_owner(executor: Executor) -> ConsoleUser | None:
    """Read the console owner (read-only; unreadable means "not ready")."""
    try:
        result = executor.run(["stat", "-f", CONSOLE_STAT_FORMAT, CONSOLE_DEVICE])
    except Exception:  # noqa: BLE001 - unreadable is "not ready", never "ready"
        return None
    if not result.ok:
        return None
    return parse_console_owner(result.stdout or "")


def default_interface(executor: Executor) -> str:
    """Default-route interface, or ``""`` when there is no usable default route."""
    try:
        result = executor.run(["route", "-n", "get", "default"])
    except Exception:  # noqa: BLE001 - unreadable is "not ready"
        return ""
    if not result.ok:
        return ""
    return conflict_mod.parse_default_interface(result.stdout or "")


def route_ready(executor: Executor) -> bool:
    """Whether a default route exists (the machine can reach the network)."""
    return bool(default_interface(executor))


def deadline_seconds(
    environ: dict[str, str] | None = None, name: str = CONSOLE_DEADLINE_ENV
) -> float:
    """Configured wait budget, clamped to a usable range."""
    values = environ if environ is not None else dict(os.environ)
    raw = (values.get(name) or "").strip()
    if not raw:
        return DEADLINE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return DEADLINE_DEFAULT
    return clamp_deadline(value)


def clamp_deadline(value: float | None) -> float | None:
    """Clamp a wait budget to :data:`DEADLINE_RANGE`.

    The ``--console-deadline``/``--network-deadline`` flags go through the
    same clamp as the environment variables: a ``--deadline 0`` or a
    ``--deadline 10^9`` from a wrapper script must not disable or hijack the
    zero-side-effect waits.
    """
    if value is None:
        return None
    low, high = DEADLINE_RANGE
    return min(high, max(low, float(value)))


def wait_until(
    probe: Callable[[], bool],
    *,
    deadline: float,
    sleeper: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
    interval: float = POLL_INTERVAL,
) -> tuple[bool, float]:
    """Poll ``probe`` until it is true or the deadline passes.

    An exception from ``probe`` is "not ready yet" (an unreadable answer never
    counts as ready).  ``sleeper``/``clock`` are injected, so no test sleeps.
    Returns ``(ready, elapsed)``.
    """
    started = clock()
    limit = started + max(0.0, float(deadline))
    while True:
        try:
            if probe():
                return True, clock() - started
        except Exception:  # noqa: BLE001 - unknown is not ready
            pass
        remaining = limit - clock()
        if remaining <= 0:
            return False, clock() - started
        sleeper(min(float(interval), remaining))


def already_in_charge(
    ctx: lifecycle.Context, *, name: str, uid: int, user: str
) -> bool:
    """mihomo running and the VPN not connected: nothing to switch.

    Both halves must be *confirmed*: an unreadable launchd state or VPN state
    returns ``False`` and the normal (idempotent, verified) flow runs instead.
    """
    try:
        if not ctx.launchd().state().running:
            return False
    except Exception:  # noqa: BLE001 - unknown is not "in charge"
        return False
    try:
        return not switch_mod.vpn_is_connected_as_user(
            ctx.executor, name, uid=uid, user=user
        )
    except Exception:  # noqa: BLE001 - unknown is not "disconnected"
        return False


def write_hold_ack(layout) -> None:
    """Write the 0600 ack for this RunAtLoad run (atomic, mode 0600).

    ``install`` waits for this file after ``bootstrap``: "loaded" alone does
    not say whether the entry process already ran, so the hold stamp must
    stay until the entry has proven - via the ack - that it exited with zero
    side effects.
    """
    write_atomic(layout.boot_hold_ack, BOOT_HOLD_ACK_TEXT, mode=0o600)


def boot_switch(
    ctx: lifecycle.Context,
    *,
    console_reader: Callable[[Executor], ConsoleUser | None] | None = None,
    route_reader: Callable[[Executor], bool] | None = None,
    probe_runner: Callable[[], probe_mod.ProbeReport] | None = None,
    force_fail: bool | None = None,
    console_deadline: float | None = None,
    network_deadline: float | None = None,
    vpn_name: str | None = None,
) -> lifecycle.Report:
    """Run the boot entry: wait, then the same root flow as ``mpm-switch mihomo``."""
    report = lifecycle.Report(command="switch")
    # Step 0: the install hold stamp.  Must run before any probe, wait or
    # launchd/scutil command - a bootstrap issued by ``install --no-start``
    # lands here, and this run must change nothing.
    if os.path.isfile(ctx.layout.boot_hold):
        report.data["boot_hold"] = True
        try:
            write_hold_ack(ctx.layout)
            report.data["boot_hold_ack"] = True
        except OSError as exc:
            # Zero side effects still hold.  install will not see the ack,
            # times out and fails closed (bootout + cleanup) instead of
            # deleting the stamp under a run that never reached it.
            report.data["boot_hold_ack"] = False
            report.warn("could not write the boot hold ack: " + sanitize(str(exc)))
        report.status = "OK"
        report.add(
            "install hold stamp present; the boot entry did nothing "
            "(install registered the job without running the switch)"
        )
        return report
    # A previous session may have shut down (or crashed) while TUN mode owned
    # the system DNS.  mihomo is not running now, so that DNS (the TUN gateway)
    # answers nothing: pull the snapshot back before any path that can refuse
    # the switch, otherwise the zero-side-effect exits below would leave the
    # machine without resolvers.  Never touch it when launchd state is unknown.
    try:
        core_running = ctx.launchd().state().running
    except Exception:  # noqa: BLE001 - unknown is not "stopped"
        core_running = True
    if not core_running:
        try:
            services = tun_dns_mod.restore(ctx.executor, ctx.layout.tun_dns_backup)
            if services:
                report.add("stale TUN DNS restored on: " + ", ".join(services))
        except Exception as exc:  # noqa: BLE001 - refusal paths must still run
            report.warn("could not restore stale TUN DNS: " + sanitize(str(exc))[:200])

    name = vpn_name or switch_mod.resolve_vpn_name(ctx.environ)
    read_console = console_reader or console_owner
    read_route = route_reader or route_ready

    console_wait = (
        console_deadline
        if console_deadline is not None
        else deadline_seconds(ctx.environ, CONSOLE_DEADLINE_ENV)
    )
    found: list[ConsoleUser] = []

    def console_ready() -> bool:
        user = read_console(ctx.executor)
        if user is None:
            return False
        found.append(user)
        return True

    ready, elapsed = wait_until(
        console_ready,
        deadline=console_wait,
        sleeper=ctx.sleeper,
        clock=ctx.clock,
    )
    report.data["console_wait"] = {
        "ok": ready,
        "elapsed": round(elapsed, 3),
        "deadline": console_wait,
    }
    if not ready or not found:
        # Zero side effects on purpose: the login window is not a session that
        # can own a VPN or a TUN device.  Starting either one here is the
        # 2026-11-14 dual-stack shape with nobody at the keyboard.
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        report.warn(
            "console user not ready after "
            f"{elapsed:g}s (deadline {console_wait:g}s); nothing was started "
            "(no mihomo, no " + name + ")"
        )
        return report

    console = found[-1]
    report.data["console_user"] = console.to_dict()

    network_wait = (
        network_deadline
        if network_deadline is not None
        else deadline_seconds(ctx.environ, NETWORK_DEADLINE_ENV)
    )
    ready, elapsed = wait_until(
        lambda: read_route(ctx.executor),
        deadline=network_wait,
        sleeper=ctx.sleeper,
        clock=ctx.clock,
    )
    report.data["network_wait"] = {
        "ok": ready,
        "elapsed": round(elapsed, 3),
        "deadline": network_wait,
    }
    if not ready:
        report.status = "FAILED"
        report.exit_code = int(ExitCode.NOT_READY)
        report.warn(
            "default route not ready after "
            f"{elapsed:g}s (deadline {network_wait:g}s); nothing was started "
            "(no mihomo, no " + name + ")"
        )
        return report

    if already_in_charge(ctx, name=name, uid=console.uid, user=console.name):
        report.status = "OK"
        report.data["already_in_charge"] = True
        report.add(
            "mihomo is already in charge and " + name + " is not connected; "
            "nothing to do (the core was not restarted)"
        )
        return report
    report.data["already_in_charge"] = False

    switched = switch_mod.switch_to_mihomo_root(
        ctx,
        uid=console.uid,
        user=console.name,
        vpn_name=name,
        probe_runner=probe_runner,
        force_fail=force_fail,
    )
    report.status = switched.status
    report.exit_code = switched.exit_code
    report.messages.extend(switched.messages)
    report.warnings.extend(switched.warnings)
    report.data.update(switched.data)
    return report


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
    parser = argparse.ArgumentParser(prog="mpm-boot")
    parser.add_argument("--root", help="alternate filesystem root (staging/tests only)")
    parser.add_argument(
        "--drill-fail",
        action="store_true",
        help="force the post-switch probe to fail (rollback drill)",
    )
    parser.add_argument("--vpn-name", help="VPN service name (default: Nextin / MPM_VPN_NAME)")
    parser.add_argument(
        "--console-deadline",
        type=float,
        help="seconds to wait for a console login user (default 180, clamped 10-900)",
    )
    parser.add_argument(
        "--network-deadline",
        type=float,
        help="seconds to wait for a default route (default 180, clamped 10-900)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    ctx = context_factory() if context_factory is not None else context_mod.build_context(args.root)
    if stdout is not None:
        ctx.out = stdout
    if stderr is not None:
        ctx.err = stderr

    return _print_report(
        ctx,
        boot_switch(
            ctx,
            force_fail=True if args.drill_fail else None,
            console_deadline=clamp_deadline(args.console_deadline),
            network_deadline=clamp_deadline(args.network_deadline),
            vpn_name=args.vpn_name,
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
