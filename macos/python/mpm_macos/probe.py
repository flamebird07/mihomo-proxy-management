"""Post-switch reachability probe (root side of ``mpm-switch``).

After mihomo is started, the switch must prove the machine still has working
Internet before it is allowed to stay in that state.  The probe runs against a
**monotonic absolute deadline** of 60 seconds (one round every 5 seconds) and
uses the real HTTP status code from ``curl``:

* ``https://www.baidu.com`` - must be 200/3xx (domestic reachability);
* ``https://www.google.com/generate_204`` - must be exactly 204;
* ``https://chatgpt.com/cdn-cgi/trace`` - must be 200;
* ``https://cursor.com`` - any status other than ``000`` counts as reachable.

Success = the domestic target works **and** at least one foreign target works.
A domestic failure, or all three foreign targets failing, is a rollback.

``curl`` also reports ``%{remote_ip}``.  A fake-ip answer (198.18.0.0/15) only
counts when ``route -n get <remote_ip>`` resolves to a utun that appeared
during this start (the before/after interface difference).  Checking the
*default* route would be wrong: macOS TUN mode can take over through more
specific routes while ``route get default`` still shows the physical link.

The deadline is absolute: every ``curl`` gets ``-m min(8, remaining)`` as a
**float** (``--max-time`` accepts fractions of a second) and a request is not
started at all when less than :data:`MIN_CURL_SECONDS` remains, so a single
request can never overrun the window.

Every command goes through an injected executor, so the tests never touch the
network.
"""

from __future__ import annotations

import ipaddress
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from mpm_common.executor import Executor
from mpm_common.sanitize import HOST

from .conflict import parse_default_interface, parse_utun_addresses

WINDOW_SECONDS = 60.0
ROUND_INTERVAL = 5.0
#: Upper bound for one ``curl -m``; the actual value is capped by the window.
CURL_TIMEOUT = 8.0
#: A request is not started when less than this much of the window remains.
MIN_CURL_SECONDS = 0.5
#: Upper bound for the informational ``exit_location`` lookup.
EXIT_LOCATION_TIMEOUT = 3.0

PROJECT_ADDRESS_BLOCK = "198.19.0.0/16"
FAKE_IP_SUPERNET = "198.18.0.0/15"

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


@dataclass(frozen=True)
class ProbeTarget:
    name: str
    url: str
    domestic: bool = False


DOMESTIC_TARGETS = (
    ProbeTarget("baidu", "https://www.baidu.com", domestic=True),
)
FOREIGN_TARGETS = (
    ProbeTarget("google", "https://www.google.com/generate_204"),
    ProbeTarget("chatgpt", "https://chatgpt.com/cdn-cgi/trace"),
    ProbeTarget("cursor", "https://cursor.com"),
)
TARGETS = DOMESTIC_TARGETS + FOREIGN_TARGETS


@dataclass
class ProbeItem:
    name: str
    url: str
    status: int
    remote_ip: str
    ok: bool
    reason: str = ""
    domestic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "status": self.status,
            "remote_ip": self.remote_ip,
            "ok": self.ok,
            "reason": HOST.text(self.reason),
            "domestic": self.domestic,
        }


@dataclass
class ProbeReport:
    ok: bool
    items: list[ProbeItem] = field(default_factory=list)
    rounds: int = 0
    reason: str = ""
    exit_location: str = ""
    #: Absolute monotonic deadline the probe ran against; ``exit_location``
    #: must never wait past it.
    deadline: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rounds": self.rounds,
            "reason": HOST.text(self.reason),
            "exit_location": self.exit_location,
            "items": [item.to_dict() for item in self.items],
        }

    def lines(self) -> list[str]:
        lines = [
            f"{item.name}: HTTP {item.status or '000'}"
            + (f" via {item.remote_ip}" if item.remote_ip else "")
            + (" OK" if item.ok else f" FAIL ({item.reason or 'unreachable'})")
            for item in self.items
        ]
        if self.exit_location:
            lines.append(f"exit: {self.exit_location}")
        return lines


def _status_for(target: ProbeTarget, status: int) -> bool:
    if target.domestic:
        return 200 <= status < 400
    if target.name == "google":
        return status == 204
    if target.name == "chatgpt":
        return status == 200
    return status != 0


def _in(address: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def curl_status(executor: Executor, url: str, *, timeout: float = CURL_TIMEOUT) -> tuple[int, str]:
    """Return ``(http_status, remote_ip)``; transport failure is status 0.

    ``--max-time`` receives the timeout as a decimal string: fractional
    seconds are honoured, rounding up would break the absolute deadline.
    """
    result = executor.run(
        [
            "curl",
            "-sS",
            "--max-time",
            f"{float(timeout):g}",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code} %{remote_ip}",
            url,
        ]
    )
    payload = (result.stdout or "").strip()
    parts = payload.split()
    try:
        status = int(parts[0]) if parts else 0
    except ValueError:
        status = 0
    remote_ip = parts[1] if len(parts) > 1 else ""
    return status, remote_ip


def route_interface(executor: Executor, address: str) -> str:
    """Interface that actually carries ``address`` (``route -n get <ip>``)."""
    if not address:
        return ""
    return parse_default_interface(
        executor.run(["route", "-n", "get", address]).stdout
    )


def fake_ip_owned_by_mihomo(
    executor: Executor,
    remote_ip: str,
    mihomo_utuns: Sequence[str] = (),
) -> bool:
    """Whether a fake-ip answer is routed through *this* start's mihomo TUN.

    ``mihomo_utuns`` is the set of interfaces that appeared between the
    pre-start and post-start observations.  When it is empty (standalone
    ``probe``), fall back to "the interface carries a 198.19/16 address".
    """
    interface = route_interface(executor, remote_ip)
    if not interface:
        return False
    if mihomo_utuns:
        return interface in set(mihomo_utuns)
    addresses = parse_utun_addresses(executor.run(["ifconfig"]).stdout)
    return any(
        name == interface and _in(address, PROJECT_ADDRESS_BLOCK)
        for name, address in addresses
    )


def _timeout_for(
    clock: Callable[[], float], deadline: float | None, cap: float = CURL_TIMEOUT
) -> float:
    """Per-request ``curl -m``: ``min(cap, remaining)``, never rounded up.

    ``curl --max-time`` accepts fractions of a second, so the exact float
    remainder is passed through; the caller stops instead of running when it
    is below :data:`MIN_CURL_SECONDS`.
    """
    if deadline is None:
        return float(cap)
    remaining = deadline - clock()
    if remaining <= 0:
        return 0.0
    return min(float(cap), remaining)


def probe_once(
    executor: Executor,
    *,
    mihomo_utuns: Sequence[str] = (),
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
    fake_ip_must_be_mihomo: bool = True,
) -> list[ProbeItem]:
    items: list[ProbeItem] = []
    for target in TARGETS:
        timeout = _timeout_for(clock, deadline, CURL_TIMEOUT)
        if timeout < MIN_CURL_SECONDS:
            # Not enough window left: starting the request could only overrun
            # the deadline, so record it as "not probed" instead.
            items.append(
                ProbeItem(
                    name=target.name,
                    url=target.url,
                    status=0,
                    remote_ip="",
                    ok=False,
                    reason="probe deadline reached",
                    domestic=target.domestic,
                )
            )
            continue
        status, remote_ip = curl_status(executor, target.url, timeout=timeout)
        ok = _status_for(target, status)
        reason = "" if ok else "unreachable"
        if (
            ok
            and fake_ip_must_be_mihomo
            and remote_ip
            and _in(remote_ip, FAKE_IP_SUPERNET)
        ):
            if not fake_ip_owned_by_mihomo(executor, remote_ip, mihomo_utuns):
                # A stale Nextin fake-ip route answered; mihomo's TUN does not
                # carry this address, so this is a false positive.
                ok = False
                reason = f"fake-ip answer {remote_ip} not routed via mihomo TUN"
        items.append(
            ProbeItem(
                name=target.name,
                url=target.url,
                status=status,
                remote_ip=remote_ip,
                ok=ok,
                reason=reason,
                domestic=target.domestic,
            )
        )
    return items


def _succeeded(items: list[ProbeItem]) -> bool:
    domestic = [item for item in items if item.domestic]
    foreign = [item for item in items if not item.domestic]
    return all(item.ok for item in domestic) and any(item.ok for item in foreign)


def run_probe(
    executor: Executor,
    *,
    window: float = WINDOW_SECONDS,
    interval: float = ROUND_INTERVAL,
    sleeper: Callable[[float], None] = time.sleep,
    mihomo_utuns: Sequence[str] = (),
    force_fail: bool | None = None,
    max_rounds: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    fake_ip_must_be_mihomo: bool = True,
) -> ProbeReport:
    """Probe until a monotonic deadline of ``window`` seconds; return the verdict.

    The deadline is wall clock (``time.monotonic``), not a round count: every
    ``curl`` gets ``-m min(8, remaining)`` as an exact float and the loop stops
    as soon as the window is gone, so four slow targets can never stretch the
    probe to minutes.
    """
    if force_fail is None:
        force_fail = os.environ.get("MPM_PROBE_FORCE_FAIL") == "1"
    if force_fail:
        return ProbeReport(ok=False, rounds=0, reason="drill: forced probe failure")

    deadline = clock() + max(0.0, float(window))
    last: list[ProbeItem] = []
    rounds = 0
    while True:
        if deadline - clock() <= 0:
            break
        rounds += 1
        last = probe_once(
            executor,
            mihomo_utuns=mihomo_utuns,
            clock=clock,
            deadline=deadline,
            fake_ip_must_be_mihomo=fake_ip_must_be_mihomo,
        )
        if _succeeded(last):
            return ProbeReport(ok=True, items=last, rounds=rounds, deadline=deadline)
        if max_rounds is not None and rounds >= max_rounds:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(float(interval), remaining))

    domestic_failed = [item.name for item in last if item.domestic and not item.ok]
    foreign = [item for item in last if not item.domestic]
    if domestic_failed:
        reason = "domestic unreachable: " + ", ".join(domestic_failed)
    elif foreign and not any(item.ok for item in foreign):
        reason = "all foreign probes failed"
    else:
        reason = "probe did not succeed within the window"
    return ProbeReport(ok=False, items=last, rounds=rounds, reason=reason, deadline=deadline)


def exit_location(
    executor: Executor,
    *,
    timeout: float = EXIT_LOCATION_TIMEOUT,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """``loc=`` from Cloudflare's trace (empty when unavailable).

    Informational only, but it must not stretch the switch either: the lookup
    gets at most ``timeout`` seconds and never more than the remaining
    deadline; below the minimum budget it is skipped entirely.
    """
    budget = float(timeout)
    if deadline is not None:
        remaining = deadline - clock()
        if remaining < MIN_CURL_SECONDS:
            return ""
        budget = min(budget, remaining)
    result = executor.run(
        ["curl", "-sS", "--max-time", f"{budget:g}", TRACE_URL]
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith("loc="):
            return line.split("=", 1)[1].strip()
    return ""


__all__ = [
    "CURL_TIMEOUT",
    "DOMESTIC_TARGETS",
    "EXIT_LOCATION_TIMEOUT",
    "FAKE_IP_SUPERNET",
    "FOREIGN_TARGETS",
    "MIN_CURL_SECONDS",
    "PROJECT_ADDRESS_BLOCK",
    "ProbeItem",
    "ProbeReport",
    "ProbeTarget",
    "ROUND_INTERVAL",
    "TARGETS",
    "TRACE_URL",
    "WINDOW_SECONDS",
    "curl_status",
    "exit_location",
    "fake_ip_owned_by_mihomo",
    "probe_once",
    "route_interface",
    "run_probe",
]
