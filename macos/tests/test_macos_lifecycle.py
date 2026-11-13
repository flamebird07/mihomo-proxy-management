"""Lifecycle state machine, entirely host-free (temp root + fakes)."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from mpm_common.controller_health import ControllerError
from mpm_common.errors import FailClosed, NotReady
from mpm_macos import config, lifecycle
from mpm_macos.network_proxy import snapshot_exists

from macos_test_support import (
    FakeController,
    FakeNetworkSetup,
    LaunchdHarness,
    TunObservation,
    assert_posix_mode,
    make_context,
    prepare_install,
)

pytestmark = pytest.mark.macos


def _installed(tmp_path, **ctx_kwargs):
    ctx = make_context(tmp_path, **ctx_kwargs)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
    harness = LaunchdHarness(ctx.executor)
    network = FakeNetworkSetup()
    ctx.executor.set("networksetup", network)
    return ctx, harness, network


def _add_geodata(ctx, names=("GeoIP.dat", "GeoSite.dat")) -> None:
    """Put geo databases next to the source binary, as MPM_MIHOMO_BINARY expects."""
    source = Path(ctx.environ["MPM_MIHOMO_BINARY"])
    for name in names:
        (source.parent / name).write_bytes(b"geo-" + name.encode())


class _Ownership:
    """Records ``chown`` calls with an injectable effective uid."""

    def __init__(self, euid: int) -> None:
        self.euid = euid
        self.calls: list[tuple[str, int, int]] = []

    def geteuid(self) -> int:
        return self.euid

    def chown(self, path: str, uid: int, gid: int) -> None:
        self.calls.append((path, uid, gid))

    def paths(self) -> set[str]:
        return {os.path.normpath(path) for path, _uid, _gid in self.calls}


def _tun_sequence(*observations):
    items = list(observations)
    iterator = iter(items)
    return lambda: next(iterator)


NO_TUN = TunObservation(("en0", "utun1"), "en0")
WITH_TUN = TunObservation(("en0", "utun1", "utun7"), "utun7")


# ---- install --------------------------------------------------------------


def test_install_writes_config_plist_and_provider(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    layout = ctx.layout
    assert os.path.isfile(layout.config)
    assert_posix_mode(layout.config, 0o600)
    assert os.path.isfile(layout.plist)
    assert os.path.isfile(layout.provider_file)
    assert os.path.isfile(layout.cli_entry)
    assert os.path.isfile(layout.binary)
    state = lifecycle.load_state(layout)
    assert state.mode == "tun"
    assert state.degraded is False


def test_install_is_idempotent(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    first = open(ctx.layout.config, "rb").read()
    first_plist = open(ctx.layout.plist, "rb").read()
    second = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert second.status == "OK"
    assert open(ctx.layout.config, "rb").read() == first
    assert open(ctx.layout.plist, "rb").read() == first_plist


def test_install_fails_closed_when_cursor_has_no_region_node(tmp_path):
    ctx = make_context(tmp_path)

    def converter(_url, destination):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write("proxies:\n  - name: 香港-01\n    type: ss\n")
        return ["香港-01"]

    prepare_install(ctx, converter=converter)
    with pytest.raises(FailClosed):
        lifecycle.install(ctx, quiet=True, skip_start=True)


# ---- install: geo data / ownership / interpreter --------------------------


def test_install_puts_geodata_in_the_mihomo_state_directory(tmp_path):
    """``mihomo -t`` and the runner both use ``-d state``; support root is wrong."""
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)
    _add_geodata(ctx)

    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    for name in ("GeoIP.dat", "GeoSite.dat"):
        installed = os.path.join(ctx.layout.state_dir, name)
        assert os.path.isfile(installed), name
        assert open(installed, "rb").read() == b"geo-" + name.encode()
        assert not os.path.exists(os.path.join(ctx.layout.support, name))
    if os.name != "nt":
        assert_posix_mode(os.path.join(ctx.layout.state_dir, "GeoIP.dat"), 0o644)

    call = ctx.executor.find(ctx.layout.binary, "-t")
    assert call is not None, "install must validate the rendered config"
    assert call.argv[call.argv.index("-d") + 1] == ctx.layout.state_dir


def test_install_fails_closed_before_mihomo_when_geodata_is_missing(tmp_path):
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)  # no GeoIP.dat / GeoSite.dat next to the binary

    with pytest.raises(config.RenderError) as excinfo:
        lifecycle.install(ctx, quiet=True, skip_start=True)

    message = str(excinfo.value)
    assert "GeoIP.dat" in message and "GeoSite.dat" in message
    assert "MPM_MIHOMO_BINARY" in message
    assert ctx.executor.count(ctx.layout.binary, "-t") == 0
    assert not os.path.exists(ctx.layout.config)


def test_plist_and_validation_share_the_mihomo_state_directory(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    document = plistlib.loads(open(ctx.layout.plist, "rb").read())
    arguments = document["ProgramArguments"]
    state = arguments[arguments.index("--state-dir") + 1]
    assert state == ctx.layout.to_target_path(ctx.layout.state_dir)
    assert document["WorkingDirectory"] == state


def test_root_system_install_chowns_state_and_secrets_to_root_wheel(tmp_path):
    owner = _Ownership(0)
    ctx = make_context(
        tmp_path,
        system_profile=True,
        geteuid=owner.geteuid,
        chown=owner.chown,
    )
    prepare_install(ctx)

    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    owned = owner.paths()
    for path in (
        ctx.layout.support,
        ctx.layout.providers,
        ctx.layout.state_dir,
        ctx.layout.logs,
        ctx.layout.secret_file,
        ctx.layout.deployment_state,
        ctx.layout.provider_file,
    ):
        assert os.path.normpath(path) in owned, path
    assert all((uid, gid) == (0, 0) for _path, uid, gid in owner.calls)


def test_non_root_install_never_chowns(tmp_path):
    owner = _Ownership(501)
    ctx = make_context(
        tmp_path,
        system_profile=True,
        geteuid=owner.geteuid,
        chown=owner.chown,
    )
    prepare_install(ctx)

    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"
    assert owner.calls == []


def test_installed_cli_defaults_to_the_installing_interpreter(tmp_path):
    interpreter = "/opt/homebrew/bin/python3.13"
    ctx = make_context(tmp_path, python=interpreter)
    prepare_install(ctx)

    lifecycle.install(ctx, quiet=True, skip_start=True)

    text = open(ctx.layout.cli_entry, encoding="utf-8").read()
    assert f"MPM_PYTHON:={interpreter}" in text
    assert "/usr/bin/python3" not in text, "stock python3 is 3.9 and has no PyYAML"


# ---- start: TUN -----------------------------------------------------------


def test_start_tun_success_never_touches_networksetup(tmp_path):
    ctx, harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)
    report = lifecycle.start(ctx, quiet=True)
    assert report.status == "OK"
    assert harness.running is True
    assert ctx.executor.count("networksetup") == 0
    state = lifecycle.load_state(ctx.layout)
    assert state.mode == "tun"
    assert state.tun_ok is True


def test_start_is_idempotent_when_already_running(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)
    lifecycle.start(ctx, quiet=True)
    before = (
        ctx.executor.count("launchctl", "bootstrap")
        + ctx.executor.count("launchctl", "kickstart")
        + ctx.executor.count("launchctl", "bootout")
    )
    second = lifecycle.start(ctx, quiet=True)
    after = (
        ctx.executor.count("launchctl", "bootstrap")
        + ctx.executor.count("launchctl", "kickstart")
        + ctx.executor.count("launchctl", "bootout")
    )
    assert second.status == "UNCHANGED"
    assert after == before, "an already-running core must not be started twice"


# ---- start: fallback ------------------------------------------------------


def test_tun_failure_falls_back_and_sets_the_system_proxy(tmp_path):
    ctx, _harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    network.seed_bypass("Wi-Fi", ["*.corp.example"])
    ctx.tun_observer = _tun_sequence(NO_TUN, NO_TUN)

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "DEGRADED"
    config_text = open(ctx.layout.config, encoding="utf-8").read()
    assert "redir-host" in config_text
    assert "fake-ip-range" not in config_text
    assert snapshot_exists(ctx.layout.network_backup)
    assert network.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"
    assert network.state["Wi-Fi"]["web"]["port"] == 7890
    assert network.state["Wi-Fi"]["web"]["enabled"] is True
    state = lifecycle.load_state(ctx.layout)
    assert state.mode == "mixed"
    assert state.degraded is True
    assert "Wi-Fi" in state.services


def test_mixed_start_failure_never_sets_the_system_proxy(tmp_path):
    ctx, _harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)

    class _FirstOkThenDown(FakeController):
        def __init__(self) -> None:
            super().__init__()
            self.version_calls = 0

        def version(self):
            self.version_calls += 1
            if self.version_calls > 1:
                raise ControllerError("controller went away")
            return super().version()

    controller = _FirstOkThenDown()
    ctx.controller_factory = lambda _secret: controller
    ctx.tun_observer = _tun_sequence(NO_TUN, NO_TUN)

    with pytest.raises(NotReady):
        lifecycle.start(ctx, quiet=True)

    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert network.state["Wi-Fi"]["web"]["enabled"] is True
    assert ctx.executor.count("networksetup", "-setwebproxy", "Wi-Fi", "127.0.0.1") == 0
    assert not snapshot_exists(ctx.layout.network_backup)


def test_wait_running_failure_falls_back_to_mixed(tmp_path):
    """The job loads but never runs: same transaction as a utun failure."""
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    harness.start_failures = 1

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "DEGRADED"
    assert report.data["mode"] == "mixed"
    assert network.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"
    assert network.state["Wi-Fi"]["web"]["port"] == 7890
    assert snapshot_exists(ctx.layout.network_backup)
    state = lifecycle.load_state(ctx.layout)
    assert state.mode == "mixed"
    assert state.degraded is True


def test_readiness_failure_falls_back_to_mixed(tmp_path):
    """An unreachable controller during the TUN attempt is a TUN failure."""

    class _ReadinessFailsFirst(FakeController):
        def __init__(self) -> None:
            super().__init__()
            self.remaining_failures = 25

        def version(self):
            if self.remaining_failures > 0:
                self.remaining_failures -= 1
                raise ControllerError("controller not ready")
            return super().version()

    ctx, _harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    controller = _ReadinessFailsFirst()
    ctx.controller_factory = lambda _secret: controller

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "DEGRADED"
    assert report.data["mode"] == "mixed"
    assert network.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"
    assert lifecycle.load_state(ctx.layout).mode == "mixed"


def test_unhealthy_tun_core_falls_back_to_mixed(tmp_path):
    """provider healthcheck failing means TUN never counts as established."""

    class _HealthcheckFailsOnce(FakeController):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def healthcheck(self, name: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise ControllerError("provider healthcheck failed")
            super().healthcheck(name)

    ctx, _harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    controller = _HealthcheckFailsOnce()
    ctx.controller_factory = lambda _secret: controller

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "DEGRADED"
    assert controller.attempts == 2, "the mixed core must be health-checked too"
    assert report.data["health"]["state"] == "OK"
    assert network.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"
    assert lifecycle.load_state(ctx.layout).mode == "mixed"


def test_unhealthy_mixed_core_never_sets_the_system_proxy(tmp_path):
    """mixed is fail-closed: no live node -> roll back, proxy untouched."""
    ctx, _harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    ctx.controller_factory = lambda _secret: FakeController(node_count=3, alive=0)
    ctx.tun_observer = _tun_sequence(NO_TUN, NO_TUN)

    with pytest.raises(NotReady):
        lifecycle.start(ctx, quiet=True)

    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert network.state["Wi-Fi"]["web"]["enabled"] is True
    assert ctx.executor.count("networksetup", "-setwebproxy", "Wi-Fi", "127.0.0.1") == 0
    assert not snapshot_exists(ctx.layout.network_backup)


# ---- stop / crash recovery ------------------------------------------------


def test_stop_restores_the_proxy_and_removes_the_snapshot(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    network.seed_bypass("Wi-Fi", ["*.corp.example"])
    ctx.tun_observer = _tun_sequence(NO_TUN, NO_TUN)
    lifecycle.start(ctx, quiet=True)
    assert network.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"

    report = lifecycle.stop(ctx, quiet=True)

    assert report.status == "OK"
    assert harness.loaded is False
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert network.state["Wi-Fi"]["web"]["port"] == 8080
    assert network.state["Wi-Fi"]["web"]["enabled"] is True
    assert network.state["Wi-Fi"]["bypass"] == ["*.corp.example"]
    assert not snapshot_exists(ctx.layout.network_backup)
    assert ctx.executor.count("launchctl", "bootout") >= 1


def test_stop_is_idempotent_when_never_started(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    report = lifecycle.stop(ctx, quiet=True)
    assert report.status == "UNCHANGED"


def test_start_restores_an_unfinished_transaction_first(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    ctx.tun_observer = _tun_sequence(NO_TUN, NO_TUN)
    lifecycle.start(ctx, quiet=True)
    assert snapshot_exists(ctx.layout.network_backup)

    # simulate a crash: the job is gone but the proxy and snapshot remain
    harness.loaded = False
    harness.running = False
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "OK"
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert not snapshot_exists(ctx.layout.network_backup)
    state = lifecycle.load_state(ctx.layout)
    assert state.mode == "tun"


# ---- status / preflight ---------------------------------------------------


def test_status_reports_degraded_after_fallback(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, NO_TUN)
    lifecycle.start(ctx, quiet=True)

    report = lifecycle.status(ctx)

    assert report.status == "DEGRADED"
    assert report.data["mode"] == "mixed"
    assert report.data["health"]["state"] == "DEGRADED"
    assert report.data["network_proxy"]["matches_saved_state"] is True


def test_preflight_report_is_ok_for_darwin_arm64(tmp_path):
    ctx = make_context(tmp_path)
    report = lifecycle.run_preflight(ctx)
    assert report.status == "OK"
    assert report.data["supported"] is True
    assert report.data["mode"] == "tun"


def test_preflight_report_is_unsupported_for_linux(tmp_path):
    from macos_test_support import darwin_facts

    ctx = make_context(tmp_path, facts=darwin_facts(system="linux"))
    report = lifecycle.run_preflight(ctx)
    assert report.status == "FAILED"
    assert report.exit_code == 4


# ---- uninstall ------------------------------------------------------------


def test_uninstall_keeps_state_by_default(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    report = lifecycle.uninstall(ctx, quiet=True)
    assert report.status == "OK"
    assert not os.path.exists(ctx.layout.plist)
    assert not os.path.exists(ctx.layout.cli_entry)
    assert os.path.isdir(ctx.layout.support)
    assert os.path.isfile(ctx.layout.config)


def test_uninstall_purge_removes_state(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    lifecycle.uninstall(ctx, purge=True, quiet=True)
    assert not os.path.exists(ctx.layout.support)
    assert not os.path.exists(ctx.layout.run_dir)
