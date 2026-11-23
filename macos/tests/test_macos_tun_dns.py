"""Host-free tests for the TUN fake-ip DNS transaction."""

from __future__ import annotations

import json
import os

from mpm_common.executor import FakeExecutor

from mpm_macos import tun_dns

ORDER = (
    "(1) Ethernet\n(Hardware Port: Ethernet, Device: en0)\n\n"
    "(2) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en1)\n"
)


def _executor(current: list[str]) -> FakeExecutor:
    executor = FakeExecutor()

    def set_servers(argv):
        if argv[-1] == "Empty":
            current[:] = []
        else:
            current[:] = argv[3:]
        return (0, "", "")

    def get_servers(argv):
        if not current:
            return (0, "There aren't any DNS Servers set on Wi-Fi.\n", "")
        return (0, "\n".join(current) + "\n", "")

    executor.set("route -n get default", (0, "   interface: en1\n", ""))
    executor.set("networksetup -listnetworkserviceorder", (0, ORDER, ""))
    executor.set("networksetup -getdnsservers", get_servers)
    executor.set("networksetup -setdnsservers", set_servers)
    return executor


def test_apply_snapshots_then_restores(tmp_path):
    current = ["114.114.114.114", "8.8.8.8"]
    executor = _executor(current)
    snapshot = str(tmp_path / "tun-dns-backup.json")

    service = tun_dns.apply(executor, snapshot)

    assert service == "Wi-Fi"
    assert current == [tun_dns.TUN_DNS]
    payload = json.loads(open(snapshot, encoding="utf-8").read())
    assert payload["service"] == "Wi-Fi"
    assert payload["servers"] == ["114.114.114.114", "8.8.8.8"]
    assert oct(os.stat(snapshot).st_mode & 0o777) == "0o600"

    assert tun_dns.restore(executor, snapshot) == ["Wi-Fi"]
    assert current == ["114.114.114.114", "8.8.8.8"]
    assert not os.path.exists(snapshot)


def test_apply_keeps_an_existing_snapshot(tmp_path):
    current = ["1.1.1.1"]
    executor = _executor(current)
    snapshot = str(tmp_path / "tun-dns-backup.json")
    tun_dns.apply(executor, snapshot)

    current[:] = ["9.9.9.9"]
    tun_dns.apply(executor, snapshot)

    payload = json.loads(open(snapshot, encoding="utf-8").read())
    assert payload["servers"] == ["1.1.1.1"], "the first snapshot is the only original"
    assert current == [tun_dns.TUN_DNS]


def test_restore_without_a_snapshot_is_a_noop(tmp_path):
    executor = _executor([])
    assert tun_dns.restore(executor, str(tmp_path / "missing.json")) == []
    assert executor.calls == []
