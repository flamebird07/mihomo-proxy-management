"""Point the active network service's DNS at the TUN gateway.

macOS ``mDNSResponder`` binds its resolver sockets to the network service, so a
DHCP/手动 DNS server is queried *outside* the TUN.  In fake-ip TUN mode that
returns poisoned answers (``chatgpt.com`` -> a Facebook address) and blocked
sites fail even though mihomo's own routing is fine.  Sending DNS to the TUN
gateway (``198.19.0.1``) forces the query through the utun, where mihomo's
``dns-hijack: any:53`` answers it with a fake-ip.

This is a system change, so it is a small transaction: snapshot the previous
servers at mode ``0600`` before touching anything, apply and read back, and only
delete the snapshot after a verified restore.  ``stop``/switch-to-Nextin calls
:func:`restore`.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from mpm_common.atomicio import read_text_strict, write_atomic
from mpm_common.errors import FailClosed
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST

TUN_DNS = "198.19.0.1"
SNAPSHOT_SCHEMA = 1

_SERVICE_ORDER = re.compile(r"^\((?:\*)?\d+\)\s+(.+)$")
_DEVICE = re.compile(r"Device:\s*([^)\s]+)")


class TunDnsError(FailClosed):
    """The DNS transaction was refused or could not be completed."""


def _run(executor: Executor, argv: list[str]) -> Any:
    result = executor.run(argv)
    if not result.ok:
        raise TunDnsError(
            "networksetup failed: " + HOST.text((result.stderr or result.stdout or "").strip())[:200]
        )
    return result


def default_interface(executor: Executor) -> str:
    result = _run(executor, ["route", "-n", "get", "default"])
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-2] == "interface:":
            return parts[-1]
    return ""


def service_for_interface(executor: Executor, interface: str) -> str:
    """Map ``en1`` back to the ``networksetup`` service name (e.g. ``Wi-Fi``)."""
    if not interface:
        return ""
    result = _run(executor, ["networksetup", "-listnetworkserviceorder"])
    current = ""
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        match = _SERVICE_ORDER.match(line)
        if match:
            current = match.group(1).strip()
            continue
        device = _DEVICE.search(line)
        if device and current and device.group(1) == interface:
            return current
    return ""


def read_servers(executor: Executor, service: str) -> list[str]:
    result = _run(executor, ["networksetup", "-getdnsservers", service])
    if "aren't any DNS Servers" in (result.stdout or ""):
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _write_servers(executor: Executor, service: str, servers: list[str]) -> None:
    argv = ["networksetup", "-setdnsservers", service, *(servers or ["Empty"])]
    _run(executor, argv)
    actual = read_servers(executor, service)
    expected = list(servers)
    if actual != expected:
        raise TunDnsError(
            f"DNS read-back mismatch on '{service}': expected {expected}, got {actual}"
        )


def apply(executor: Executor, snapshot_path: str) -> str:
    """Snapshot the current servers, then point them at the TUN gateway.

    Returns the service name that was changed.  An existing snapshot is kept
    (it is the only record of the original servers) and the DNS is re-applied,
    which makes a repeated ``start`` idempotent.
    """
    if os.path.isfile(snapshot_path):
        payload = json.loads(read_text_strict(snapshot_path))
        service = str(payload.get("service") or "")
        if not service:
            raise TunDnsError("the DNS snapshot has no service; remove it and retry")
        _write_servers(executor, service, [TUN_DNS])
        return service

    interface = default_interface(executor)
    service = service_for_interface(executor, interface)
    if not service:
        raise TunDnsError(
            "could not map the default-route interface to a networksetup service"
        )
    servers = read_servers(executor, service)
    write_atomic(
        snapshot_path,
        json.dumps(
            {"schema": SNAPSHOT_SCHEMA, "service": service, "servers": servers},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        mode=0o600,
    )
    _write_servers(executor, service, [TUN_DNS])
    return service


def restore(executor: Executor, snapshot_path: str) -> list[str]:
    """Put the snapshotted servers back; delete the snapshot only on success."""
    if not os.path.isfile(snapshot_path):
        return []
    payload = json.loads(read_text_strict(snapshot_path))
    service = str(payload.get("service") or "")
    servers = [str(item) for item in (payload.get("servers") or [])]
    if not service:
        raise TunDnsError("the DNS snapshot has no service; fix it by hand")
    _write_servers(executor, service, servers)
    os.unlink(snapshot_path)
    return [service]
