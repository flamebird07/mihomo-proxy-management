"""``networksetup`` snapshot / apply / restore transaction."""

from __future__ import annotations

import json

import pytest

from mpm_common.executor import FakeExecutor
from mpm_macos import network_proxy as np

from macos_test_support import FakeNetworkSetup, assert_posix_mode

pytestmark = pytest.mark.macos


def _proxy():
    executor = FakeExecutor()
    fake = FakeNetworkSetup()
    executor.set("networksetup", fake)
    return np.NetworkProxy(executor), fake, executor


def test_parse_proxy_enabled_values():
    setting = np.parse_proxy(
        "Enabled: Yes\nServer: 10.0.0.5\nPort: 8080\nAuthenticated Proxy Enabled: 0\n"
    )
    assert setting.enabled is True
    assert setting.server == "10.0.0.5"
    assert setting.port == 8080
    assert setting.authenticated is False


def test_parse_proxy_detects_authentication():
    setting = np.parse_proxy(
        "Enabled: Yes\nServer: 10.0.0.5\nPort: 8080\nAuthenticated Proxy Enabled: 1\n"
    )
    assert setting.authenticated is True


def test_parse_proxy_empty_port_is_zero():
    setting = np.parse_proxy("Enabled: No\nServer:\nPort:\nAuthenticated Proxy Enabled: 0\n")
    assert setting.enabled is False
    assert setting.port == 0


def test_parse_bypass_domains():
    assert np.parse_bypass("There aren't any bypass domains set on Wi-Fi.") == []
    assert np.parse_bypass("*.corp.example\nlocalhost\n") == ["*.corp.example", "localhost"]


def test_parse_services_skips_header_and_marks_disabled():
    entries = np.parse_services(
        "An asterisk (*) denotes that a network service is disabled.\n"
        "Wi-Fi\n"
        "*USB 10/100/1000 LAN\n"
    )
    assert [(entry.name, entry.disabled) for entry in entries] == [
        ("Wi-Fi", False),
        ("USB 10/100/1000 LAN", True),
    ]


def test_snapshot_records_every_service_and_value():
    proxy, fake, _executor = _proxy()
    fake.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    fake.seed("Wi-Fi", "socks", enabled=True, server="10.0.0.6", port=1080)
    fake.seed_bypass("Wi-Fi", ["*.corp.example"])
    snapshot = proxy.snapshot()
    assert [service.name for service in snapshot.services] == [
        "Wi-Fi",
        "USB 10/100/1000 LAN",
    ]
    assert snapshot.services[0].web.enabled is True
    assert snapshot.services[0].web.server == "10.0.0.5"
    assert snapshot.services[0].socks.port == 1080
    assert snapshot.services[0].bypass == ["*.corp.example"]
    assert snapshot.services[1].web.enabled is False


def test_apply_points_every_enabled_service_at_the_local_proxy():
    proxy, fake, executor = _proxy()
    snapshot = proxy.snapshot()
    modified = proxy.apply(snapshot)
    assert modified == ["Wi-Fi", "USB 10/100/1000 LAN"]
    for name in modified:
        for kind in ("web", "secure", "socks"):
            assert fake.state[name][kind]["enabled"] is True
            assert fake.state[name][kind]["server"] == "127.0.0.1"
            assert fake.state[name][kind]["port"] == 7890

    call = executor.find("networksetup", "-setwebproxy", "USB 10/100/1000 LAN")
    assert call is not None
    assert call.argv == [
        "networksetup",
        "-setwebproxy",
        "USB 10/100/1000 LAN",
        "127.0.0.1",
        "7890",
    ]


def test_apply_never_modifies_a_disabled_service():
    executor = FakeExecutor()
    fake = FakeNetworkSetup(disabled=("USB 10/100/1000 LAN",))
    executor.set("networksetup", fake)
    proxy = np.NetworkProxy(executor)
    snapshot = proxy.snapshot()
    proxy.apply(snapshot)
    assert executor.find("networksetup", "-setwebproxy", "USB 10/100/1000 LAN") is None
    assert fake.state["USB 10/100/1000 LAN"]["web"]["enabled"] is False


def test_restore_returns_the_saved_values():
    proxy, fake, _executor = _proxy()
    fake.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    fake.seed_bypass("Wi-Fi", ["*.corp.example"])
    snapshot = proxy.snapshot()

    proxy.apply(snapshot)
    assert fake.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"

    proxy.restore(snapshot)
    assert fake.state["Wi-Fi"]["web"]["enabled"] is True
    assert fake.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert fake.state["Wi-Fi"]["web"]["port"] == 8080
    assert fake.state["Wi-Fi"]["bypass"] == ["*.corp.example"]
    assert fake.state["Wi-Fi"]["secure"]["enabled"] is False


def test_authenticated_proxy_fails_closed_before_any_change():
    proxy, fake, executor = _proxy()
    fake.authenticated.add(("Wi-Fi", "web"))
    snapshot = proxy.snapshot()
    with pytest.raises(np.NetworkProxyError) as excinfo:
        proxy.apply(snapshot)
    assert "authentication" in str(excinfo.value)
    assert executor.count("networksetup", "-setwebproxy") == 0
    assert executor.count("networksetup", "-setwebproxystate") == 0


def test_readback_mismatch_rolls_back():
    proxy, fake, _executor = _proxy()
    fake.fail_set_for.add("USB 10/100/1000 LAN")
    snapshot = proxy.snapshot()
    with pytest.raises(np.NetworkProxyError):
        proxy.apply(snapshot)
    # Wi-Fi was set, then rolled back to its original (disabled) state
    assert fake.state["Wi-Fi"]["web"]["enabled"] is False


def test_snapshot_file_is_0600_and_never_overwritten(tmp_path):
    proxy, _fake, _executor = _proxy()
    snapshot = proxy.snapshot()
    path = tmp_path / "network-proxy-backup.json"
    np.save_snapshot(str(path), snapshot)
    assert_posix_mode(path, 0o600)
    with pytest.raises(np.NetworkProxyError):
        np.save_snapshot(str(path), snapshot)
    loaded = np.load_snapshot(str(path))
    assert [service.name for service in loaded.services] == ["Wi-Fi", "USB 10/100/1000 LAN"]
    assert loaded.stage == np.STAGE_PREPARED
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == np.SNAPSHOT_SCHEMA


def test_restore_verification_failure_is_reported():
    proxy, fake, executor = _proxy()
    fake.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    snapshot = proxy.snapshot()
    # the host drifted away from the snapshot (e.g. a previous apply)
    fake.state["Wi-Fi"]["web"] = {"enabled": True, "server": "127.0.0.1", "port": 7890}

    def sabotage(argv):
        # reads still work, writes are silently dropped
        if argv[1].startswith("-get") or argv[1] == "-listallnetworkservices":
            return fake(argv)
        return (0, "", "")

    executor.set("networksetup", sabotage)
    with pytest.raises(np.NetworkProxyError):
        proxy.restore(snapshot)
