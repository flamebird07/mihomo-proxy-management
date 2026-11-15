"""External VPN / port / route conflict detection (host-free)."""

from __future__ import annotations

import pytest

from mpm_common.executor import FakeExecutor
from mpm_macos import conflict

pytestmark = pytest.mark.macos

SCUTIL_CONNECTED = """Available network connection services in the current set (*=enabled):
* (Connected)    "Nextin" [VPN:com.Tommy.Nextin]
"""

SCUTIL_DISCONNECTED = """Available network connection services in the current set (*=enabled):
* (Disconnected)    "Nextin" [VPN:com.Tommy.Nextin]
"""

IFCONFIG_NEXTIN = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1500
\tinet 198.18.0.1 --> 198.18.0.1 netmask 0xffff0000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.0.2.10 netmask 0xffffff00
"""

IFCONFIG_MIHOMO = """utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1500
\tinet 198.19.0.1 --> 198.19.0.1 netmask 0xffff0000
"""


def _executor(
    *,
    scutil=SCUTIL_DISCONNECTED,
    ports=None,
    route="interface: en0\n",
    ifconfig="",
    job_pid=100,
    ps=None,
):
    executor = FakeExecutor()
    executor.set("scutil --nc list", (0, scutil, ""))
    executor.set("route -n get default", (0, route, ""))
    executor.set("ifconfig", (0, ifconfig, ""))
    if job_pid is None:
        executor.set("launchctl print", (113, "", "Could not find service"))
    else:
        executor.set("launchctl print", (0, f"state = running\n\tpid = {job_pid}\n", ""))
    paths = dict(ps or {})
    executor.set(
        "ps -o comm=",
        lambda argv: (0, paths.get(int(argv[-1]), ""), ""),
    )
    for port, payload in (ports or {}).items():
        executor.set(f"lsof -nP -iTCP:{port}", (0, payload, ""))
    return executor


# ---- parsers --------------------------------------------------------------


def test_parse_scutil_nc_list_only_connected():
    connections = conflict.parse_scutil_nc_list(SCUTIL_CONNECTED + SCUTIL_DISCONNECTED)
    assert len(connections) == 1
    assert connections[0].name == "Nextin"
    assert connections[0].bundle == "com.Tommy.Nextin"


def test_parse_lsof_listeners_pairs_pid_and_command():
    owners = conflict.parse_lsof_listeners("p991\ncproxytunnel\np992\ncmihomo\n", 7890)
    assert [(owner.pid, owner.command) for owner in owners] == [
        (991, "proxytunnel"),
        (992, "mihomo"),
    ]


def test_parse_utun_addresses():
    addresses = conflict.parse_utun_addresses(IFCONFIG_NEXTIN)
    assert addresses == [("utun3", "198.18.0.1")]


# ---- Always On VPN (On Demand) flag ---------------------------------------

SCUTIL_SHOW_ON_DEMAND = """SCNetworkReachability information:
  "Nextin" [VPN:com.Tommy.Nextin]
  OnDemandEnabled : TRUE
  OnDemandRules : [
    {Action: Connect}
  ]
"""


def test_parse_on_demand_enabled_true():
    assert conflict.parse_on_demand_enabled(SCUTIL_SHOW_ON_DEMAND) is True


def test_parse_on_demand_enabled_false():
    assert conflict.parse_on_demand_enabled("OnDemandEnabled : FALSE\n") is False


def test_parse_on_demand_is_case_and_space_tolerant():
    assert conflict.parse_on_demand_enabled("ondemandenabled:true") is True
    assert conflict.parse_on_demand_enabled("  OnDemandEnabled    :    FALSE  ") is False


def test_parse_on_demand_missing_returns_none():
    assert conflict.parse_on_demand_enabled("") is None
    assert conflict.parse_on_demand_enabled('"Nextin" [VPN:com.Tommy.Nextin]\n') is None


def test_parse_on_demand_key_is_anchored_at_the_line_start():
    """A longer key must not be mistaken for ``OnDemandEnabled``."""
    assert conflict.parse_on_demand_enabled("SomeOnDemandEnabled : TRUE\n") is None
    assert conflict.parse_on_demand_enabled("X-OnDemandEnabled: FALSE\n") is None
    # leading blanks are what scutil actually prints, and still count
    assert conflict.parse_on_demand_enabled("\t  OnDemandEnabled : TRUE\n") is True


def test_parse_on_demand_contradictory_lines_return_none():
    """Ambiguity is never "disabled": the caller has to fail closed."""
    text = "OnDemandEnabled : TRUE\nOnDemandEnabled : FALSE\n"
    assert conflict.parse_on_demand_enabled(text) is None


def test_missing_service_output_is_recognised():
    assert conflict.is_missing_service_output("No service\n") is True
    assert conflict.is_missing_service_output("no service") is True
    assert conflict.is_missing_service_output(SCUTIL_SHOW_ON_DEMAND) is False


# ---- service states (a stop is not finished while it is Disconnecting) ----


def test_disconnecting_is_not_reported_as_connected():
    """``(Disconnecting)`` is a teardown, not a connection."""
    text = (
        "Available network connection services in the current set (*=enabled):\n"
        '* (Disconnecting)    "Nextin" [VPN:com.Tommy.Nextin]\n'
    )
    assert conflict.parse_scutil_nc_list(text) == []


def test_parse_nc_status_keeps_the_transient_states():
    """``Disconnecting``/``Stopping`` are not ``Disconnected``."""
    assert conflict.parse_nc_status("Connected\n") == "connected"
    assert conflict.parse_nc_status("Connecting\n") == "connecting"
    assert (
        conflict.parse_nc_status("Disconnecting\nExtended Status <dictionary> {\n}\n")
        == "disconnecting"
    )
    assert conflict.parse_nc_status("Stopping\n") == "stopping"
    assert conflict.parse_nc_status("Disconnected\n") == "disconnected"
    assert conflict.parse_nc_status("No service\n") == "unknown"
    assert conflict.parse_nc_status("") == "unknown"


def test_parse_scutil_nc_states_reads_every_service_line():
    text = (
        "Available network connection services in the current set (*=enabled):\n"
        '* (Disconnecting)    "Nextin" [VPN:com.Tommy.Nextin]\n'
        '* (Connected)    "Corp VPN" [VPN:com.example.corp]\n'
        "* (Disconnected)   1234-... PPP --> ...\n"
    )
    states = conflict.parse_scutil_nc_states(text)
    assert states["Nextin"] == "disconnecting"
    assert states["Corp VPN"] == "connected"
    assert set(states.values()) == {"disconnecting", "connected", "disconnected"}


# ---- detection ------------------------------------------------------------


def test_nextin_connected_is_conflict():
    report = conflict.detect(_executor(scutil=SCUTIL_CONNECTED))
    assert report.ok is False
    assert [item.name for item in report.vpn_connected] == ["Nextin"]
    assert any("Nextin" in reason for reason in report.reasons)


def test_port_7890_owned_by_proxytunnel_is_conflict():
    report = conflict.detect(_executor(ports={7890: "p991\ncproxytunnel\n"}))
    assert report.ok is False
    assert any("7890" in reason for reason in report.reasons)
    assert any("proxytunnel" in reason for reason in report.reasons)


def test_own_mihomo_on_7890_is_not_conflict():
    """Our launchd job's PID owns the ports: that is us, not a conflict."""
    report = conflict.detect(
        _executor(
            ports={7890: "p100\ncmihomo\n", 9090: "p100\ncmihomo\n"},
            route="interface: utun4\n",
            ifconfig=IFCONFIG_MIHOMO,
            job_pid=100,
        )
    )
    assert report.ok is True, report.reasons
    assert report.foreign_default_utun == ""
    assert report.project_pids == [100]


def test_own_mihomo_identified_by_executable_path():
    """Without a launchd PID the executable path still identifies us."""
    report = conflict.detect(
        _executor(
            ports={7890: "p100\ncmihomo\n", 9090: "p100\ncmihomo\n"},
            job_pid=None,
            ps={100: conflict.PROJECT_EXECUTABLE},
        )
    )
    assert report.ok is True, report.reasons


def test_foreign_process_named_mihomo_is_not_ours():
    """A hand-started ``mihomo`` is foreign: identity is PID/path, not a name."""
    report = conflict.detect(
        _executor(
            ports={7890: "p777\ncmihomo\n", 9090: "p777\ncmihomo\n"},
            job_pid=100,
            ps={777: "/opt/homebrew/bin/mihomo"},
        )
    )
    assert report.ok is False
    assert any("7890" in reason for reason in report.reasons)


def test_foreign_default_utun_is_conflict():
    report = conflict.detect(
        _executor(route="interface: utun3\n", ifconfig=IFCONFIG_NEXTIN)
    )
    assert report.ok is False
    assert report.foreign_default_utun == "utun3"
    assert any("utun3" in reason for reason in report.reasons)


def test_foreign_198_18_address_without_mihomo_is_conflict():
    report = conflict.detect(_executor(ifconfig=IFCONFIG_NEXTIN))
    assert report.ok is False
    assert any("198.18.0.1" in reason for reason in report.reasons)


def test_detect_error_is_fail_safe():
    class Broken(FakeExecutor):
        def run(self, argv, **kwargs):
            raise RuntimeError("probe exploded")

    report = conflict.detect(Broken())
    assert report.ok is False
    assert "probe exploded" in report.error
    assert any("冲突检测失败" in reason for reason in report.reasons)
