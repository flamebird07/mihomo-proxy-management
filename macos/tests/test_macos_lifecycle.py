"""Lifecycle state machine, entirely host-free (temp root + fakes)."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest
import yaml

from mpm_common.controller_health import ControllerError
from mpm_common.errors import FailClosed, NotReady
from mpm_common.executor import Result
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
    # make_context already wired a harness; reuse it so the assertions below
    # read the state install actually produced.
    harness = LaunchdHarness.attached(ctx.executor)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
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
    assert os.path.isfile(layout.switch_entry)
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


def test_install_no_start_leaves_service_disabled(tmp_path):
    """KeepAlive implies RunAtLoad, so only ``disable`` keeps boot clean."""
    ctx, harness, _network = _installed(tmp_path)
    assert harness.disabled is True
    assert harness.loaded is False
    assert any(
        call.argv[:2] == ["launchctl", "disable"] for call in ctx.executor.calls
    )


def test_start_enables_bootstraps_then_disables(tmp_path):
    ctx, harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "OK"
    assert harness.running is True
    assert harness.disabled is True, "the loaded job keeps running but must not auto-start"
    commands = [call.argv[:2] for call in ctx.executor.calls]
    enable_at = commands.index(["launchctl", "enable"])
    bootstrap_at = commands.index(["launchctl", "bootstrap"])
    disable_at = len(commands) - 1 - commands[::-1].index(["launchctl", "disable"])
    assert enable_at < bootstrap_at < disable_at


def test_stop_bootout_and_disable(tmp_path):
    ctx, harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)
    lifecycle.start(ctx, quiet=True)
    ctx.executor.calls.clear()

    report = lifecycle.stop(ctx, quiet=True)

    assert report.status == "OK"
    assert harness.loaded is False
    assert harness.disabled is True
    commands = [call.argv[:2] for call in ctx.executor.calls]
    assert commands.index(["launchctl", "bootout"]) < commands.index(["launchctl", "disable"])


def _fail_on(ctx, *prefix):
    """Make one launchctl subcommand fail, keeping the rest of the fake working."""
    original = ctx.executor.run

    def run(argv, **kwargs):
        if tuple(argv[: len(prefix)]) == tuple(prefix):
            return Result(tuple(argv), 1, "", "refused")
        return original(argv, **kwargs)

    ctx.executor.run = run


def test_stop_core_disable_runs_even_if_bootout_fails(tmp_path):
    """``disable`` must run in ``finally``: never leave a boot-enabled job."""
    ctx, harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)
    lifecycle.start(ctx, quiet=True)
    ctx.executor.calls.clear()
    _fail_on(ctx, "launchctl", "bootout")

    with pytest.raises(NotReady):
        lifecycle._stop_core(ctx)

    assert ctx.executor.count("launchctl", "disable") >= 1
    assert harness.disabled is True
    assert harness.running is True, "a still-running core is not a safe rollback"


def test_post_start_disable_failure_rolls_back(tmp_path):
    """A failed persistent disable is a failed start, not a warning."""
    ctx, harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)
    _fail_on(ctx, "launchctl", "disable")

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert harness.loaded is False, "the core must be booted out again"
    assert ctx.executor.count("launchctl", "bootout") >= 1
    state = lifecycle.load_state(ctx.layout)
    assert state.tun_ok is False


def test_post_start_disable_readback_failure_rolls_back(tmp_path):
    """An unreadable override is not a confirmation of "disabled"."""
    ctx, harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)
    _fail_on(ctx, "launchctl", "print-disabled")

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert harness.loaded is False


# ---- install: geo data / ownership / interpreter --------------------------


def test_install_puts_geodata_in_the_mihomo_home_directory(tmp_path):
    """``mihomo -t`` and the runner both use ``-d support``; ``state/`` is wrong."""
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)
    _add_geodata(ctx)

    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    for name in ("GeoIP.dat", "GeoSite.dat"):
        installed = os.path.join(ctx.layout.mihomo_home, name)
        assert os.path.isfile(installed), name
        assert open(installed, "rb").read() == b"geo-" + name.encode()
        assert not os.path.exists(os.path.join(ctx.layout.state_dir, name))
    if os.name != "nt":
        assert_posix_mode(os.path.join(ctx.layout.mihomo_home, "GeoIP.dat"), 0o644)

    call = ctx.executor.find(ctx.layout.binary, "-t")
    assert call is not None, "install must validate the rendered config"
    assert call.argv[call.argv.index("-d") + 1] == ctx.layout.mihomo_home


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


def _is_subpath(child: str, parent: str) -> bool:
    child_n = os.path.normcase(os.path.normpath(child))
    parent_n = os.path.normcase(os.path.normpath(parent))
    return child_n != parent_n and child_n.startswith(parent_n + os.sep)


def test_provider_path_plist_and_geodata_share_the_mihomo_home(tmp_path):
    """Invariant: one ``-d`` directory for the plist, ``mihomo -t`` and the provider.

    mihomo rejects a provider path that is not below its home (or in
    ``SAFE_PATHS``), so the rendered provider path, the geo databases and the
    plist's ``--state-dir``/``WorkingDirectory`` must all resolve to
    ``layout.support`` - never to ``layout.state_dir``.
    """
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)
    _add_geodata(ctx)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    plist_document = plistlib.loads(open(ctx.layout.plist, "rb").read())
    arguments = plist_document["ProgramArguments"]
    plist_home = arguments[arguments.index("--state-dir") + 1]
    assert plist_home == plist_document["WorkingDirectory"]
    assert plist_home == ctx.layout.to_target_path(ctx.layout.mihomo_home)
    assert plist_home != ctx.layout.to_target_path(ctx.layout.state_dir)

    call = ctx.executor.find(ctx.layout.binary, "-t")
    assert call is not None, "install must validate the rendered config"
    validate_home = call.argv[call.argv.index("-d") + 1]
    assert validate_home == ctx.layout.mihomo_home
    assert ctx.layout.to_target_path(validate_home) == plist_home

    with open(ctx.layout.config, "r", encoding="utf-8") as handle:
        config_document = yaml.safe_load(handle.read())
    provider_path = config_document["proxy-providers"]["subscription"]["path"]
    assert _is_subpath(provider_path, validate_home), provider_path
    assert os.path.dirname(os.path.normpath(provider_path)) == os.path.normpath(
        os.path.join(validate_home, "providers")
    )

    # geo data is installed where ``-d`` points, so mihomo -t never downloads it
    for name in ("GeoIP.dat", "GeoSite.dat"):
        assert os.path.isfile(os.path.join(validate_home, name)), name


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


def test_start_already_running_disable_readback_failure_fails_safe(tmp_path):
    """An unconfirmed override on an already-running core is a failed start.

    Like the fresh paths, the already-running branch must read the disable
    override back; a failure runs the safety stop transaction (bootout +
    disable) instead of reporting a false UNCHANGED or raising past the CLI.
    """
    ctx, harness, _network = _installed(tmp_path)
    ctx.tun_observer = _tun_sequence(NO_TUN, WITH_TUN)
    lifecycle.start(ctx, quiet=True)
    assert harness.running is True
    _fail_on(ctx, "launchctl", "print-disabled")

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert harness.loaded is False, "the running core is stopped by the safety transaction"
    assert ctx.executor.count("launchctl", "bootout") >= 1
    assert any("disable" in warning or "override" in warning for warning in report.warnings)


# ---- start: failure is FAILED, never a mixed takeover ---------------------


def test_start_health_failure_stops_and_restores_no_mixed_takeover(tmp_path):
    """A TUN failure must stop the core and leave the system proxy alone."""
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    network.seed_bypass("Wi-Fi", ["*.corp.example"])
    ctx.tun_observer = _tun_sequence(NO_TUN, NO_TUN)

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert report.exit_code not in (0, None)
    assert harness.loaded is False
    assert harness.disabled is True
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert network.state["Wi-Fi"]["web"]["port"] == 8080
    assert ctx.executor.count("networksetup", "-setwebproxy", "Wi-Fi", "127.0.0.1") == 0
    assert not snapshot_exists(ctx.layout.network_backup)
    text = open(ctx.layout.config, encoding="utf-8").read()
    assert "enhanced-mode: fake-ip" in text, "the TUN config must not be replaced by mixed"
    assert "redir-host" not in text
    state = lifecycle.load_state(ctx.layout)
    assert state.mode == "tun"
    assert state.degraded is False


def test_start_wait_running_failure_stops_without_mixed(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    harness.start_failures = 1

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert harness.loaded is False
    assert harness.disabled is True
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert not snapshot_exists(ctx.layout.network_backup)


def test_start_readiness_failure_stops_without_mixed(tmp_path):
    class _ReadinessFails(FakeController):
        def version(self, *, timeout: float | None = None):
            raise ControllerError("controller not ready")

    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    ctx.controller_factory = lambda _secret: _ReadinessFails()

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert harness.loaded is False
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"


def test_start_unhealthy_core_stops_without_mixed(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    ctx.controller_factory = lambda _secret: FakeController(node_count=3, alive=0)

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert harness.loaded is False
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"


def test_start_requires_autoforeign_and_gpt_group_liveness(tmp_path):
    ctx, harness, _network = _installed(tmp_path)
    ctx.controller_factory = lambda _secret: FakeController(
        group_alive={"AUTO-FOREIGN": True, "GPT": False}
    )

    report = lifecycle.start(ctx, quiet=True)

    assert report.status == "FAILED"
    assert any("GPT" in warning for warning in report.warnings)
    assert harness.loaded is False


def test_start_conflict_refuses_before_any_launchd_action(tmp_path):
    from mpm_macos.conflict import ConflictReport

    ctx, harness, _network = _installed(tmp_path)
    ctx.conflict_detector = lambda: ConflictReport(
        ok=False, reasons=["外部 VPN 已连接：Nextin"]
    )
    # install already registered the boot-switch job, so compare against the
    # pre-start count: the refusal must not bootstrap anything *new*.
    bootstraps_before = ctx.executor.count("launchctl", "bootstrap")

    with pytest.raises(FailClosed) as excinfo:
        lifecycle.start(ctx, quiet=True)

    assert "Nextin" in str(excinfo.value)
    assert harness.loaded is False
    assert ctx.executor.count("launchctl", "bootstrap") == bootstraps_before


# ---- start: explicit mixed mode only --------------------------------------


def test_explicit_mixed_start_sets_the_proxy(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    network.seed_bypass("Wi-Fi", ["*.corp.example"])

    report = lifecycle.start(ctx, quiet=True, mode="mixed")

    assert report.status == "DEGRADED"
    assert report.data["mode"] == "mixed"
    assert harness.running is True
    assert harness.disabled is True
    assert snapshot_exists(ctx.layout.network_backup)
    assert network.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"
    assert network.state["Wi-Fi"]["web"]["port"] == 7890
    assert network.state["Wi-Fi"]["bypass"] == ["*.corp.example"]
    state = lifecycle.load_state(ctx.layout)
    assert state.mode == "mixed"
    assert state.degraded is True
    assert "Wi-Fi" in state.services
    assert "redir-host" in open(ctx.layout.config, encoding="utf-8").read()


def test_explicit_mixed_start_failure_never_sets_the_system_proxy(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    ctx.controller_factory = lambda _secret: FakeController(node_count=3, alive=0)

    report = lifecycle.start(ctx, quiet=True, mode="mixed")

    assert report.status == "FAILED"
    assert harness.loaded is False
    assert harness.disabled is True
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert network.state["Wi-Fi"]["web"]["enabled"] is True
    assert ctx.executor.count("networksetup", "-setwebproxy", "Wi-Fi", "127.0.0.1") == 0
    assert not snapshot_exists(ctx.layout.network_backup)
    # the failed mixed attempt must not leave a mixed config for the next start
    assert "enhanced-mode: fake-ip" in open(ctx.layout.config, encoding="utf-8").read()
    assert lifecycle.load_state(ctx.layout).mode == "tun"


# ---- stop / crash recovery ------------------------------------------------


def test_stop_restores_the_proxy_and_removes_the_snapshot(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    network.seed_bypass("Wi-Fi", ["*.corp.example"])
    lifecycle.start(ctx, quiet=True, mode="mixed")
    assert network.state["Wi-Fi"]["web"]["server"] == "127.0.0.1"

    report = lifecycle.stop(ctx, quiet=True)

    assert report.status == "OK"
    assert harness.loaded is False
    assert harness.disabled is True
    assert network.state["Wi-Fi"]["web"]["server"] == "10.0.0.5"
    assert network.state["Wi-Fi"]["web"]["port"] == 8080
    assert network.state["Wi-Fi"]["web"]["enabled"] is True
    assert network.state["Wi-Fi"]["bypass"] == ["*.corp.example"]
    assert not snapshot_exists(ctx.layout.network_backup)
    assert ctx.executor.count("launchctl", "bootout") >= 1
    assert ctx.executor.count("launchctl", "disable") >= 1


def test_stop_is_idempotent_when_never_started(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    report = lifecycle.stop(ctx, quiet=True)
    assert report.status == "UNCHANGED"
    assert _harness.disabled is True


def test_start_restores_an_unfinished_transaction_first(tmp_path):
    ctx, harness, network = _installed(tmp_path)
    network.seed("Wi-Fi", "web", enabled=True, server="10.0.0.5", port=8080)
    lifecycle.start(ctx, quiet=True, mode="mixed")
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


def test_status_reports_degraded_for_explicit_mixed(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    lifecycle.start(ctx, quiet=True, mode="mixed")

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


# ---- Feishu-pool mode (no subscription URL) --------------------------------


def _pool_context(tmp_path, **kwargs):
    """A context whose secret file has no subscription URL (pool mode)."""
    return make_context(
        tmp_path,
        environ={"MPM_SUBSCRIPTION_URL": "", "MPM_SECRET": ""},
        **kwargs,
    )


def _write_pool(ctx, names=("美国-01", "日本-01", "新加坡-01")) -> None:
    """A node pool provider file inside the sandbox (host-free by construction)."""
    path = ctx.layout.pool_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(
            {"proxies": [{"name": name, "type": "ss"} for name in names]},
            handle,
            allow_unicode=True,
            sort_keys=False,
        )


def _write_secrets(ctx, text="SUBSCRIPTION_URL=\nSECRET=canary-controller\n") -> None:
    """A 0600-file payload with the controller secret (always required)."""
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    with open(ctx.layout.secret_file, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_load_secrets_allows_an_empty_subscription_url(tmp_path):
    ctx = _pool_context(tmp_path)
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    with open(ctx.layout.secret_file, "w", encoding="utf-8") as handle:
        handle.write("SECRET=canary-controller\n")
    secrets = ctx.secrets()
    assert secrets.subscription_url == ""
    assert secrets.controller_secret == "canary-controller"


def test_load_secrets_still_fails_closed_without_a_controller_secret(tmp_path):
    ctx = _pool_context(tmp_path)
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    with open(ctx.layout.secret_file, "w", encoding="utf-8") as handle:
        handle.write("SUBSCRIPTION_URL=\n")
    with pytest.raises(FailClosed):
        ctx.secrets()


def test_empty_url_round_trips_through_save_secrets(tmp_path):
    ctx = _pool_context(tmp_path)
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    with open(ctx.layout.secret_file, "w", encoding="utf-8") as handle:
        handle.write(
            "SECRET=canary-controller\n"
            "FEISHU_APP_ID=app-id\n"
            "FEISHU_APP_SECRET=app-secret\n"
        )
    secrets = ctx.secrets()
    lifecycle.save_secrets(ctx.layout, secrets)
    reread = ctx.secrets()
    assert reread.subscription_url == ""
    assert reread.controller_secret == "canary-controller"
    assert reread.feishu_app_id == "app-id"
    assert reread.feishu_app_secret == "app-secret"


def test_pool_install_renders_the_file_provider(tmp_path):
    ctx = _pool_context(tmp_path)
    prepare_install(ctx)
    _write_secrets(ctx)
    _write_pool(ctx)

    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert report.data["provider_nodes"] == 3
    with open(ctx.layout.config, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle.read())
    provider = document["proxy-providers"]["subscription"]
    assert provider["type"] == "file"
    assert provider["path"] == config.POOL_PROVIDER_PATH
    assert "url" not in provider
    assert "interval" not in provider
    # the policy verdict was computed from the pool node names (美国/日本 hit
    # CURSOR; the missing CODEBUDDY-HK region surfaces as a warning)
    assert any("CODEBUDDY-HK" in warning for warning in report.warnings)


def test_pool_install_fails_closed_when_the_pool_file_is_missing(tmp_path):
    ctx = _pool_context(tmp_path)
    prepare_install(ctx)
    _write_secrets(ctx)

    with pytest.raises(FailClosed) as excinfo:
        lifecycle.install(ctx, quiet=True, skip_start=True)

    assert "Feishu node pool" in str(excinfo.value)


def test_pool_install_fails_closed_when_the_pool_has_no_cursor_region(tmp_path):
    ctx = _pool_context(tmp_path)
    prepare_install(ctx)
    _write_secrets(ctx)
    _write_pool(ctx, names=("香港-01", "新加坡-01"))

    with pytest.raises(FailClosed):
        lifecycle.install(ctx, quiet=True, skip_start=True)


def test_root_pool_install_chowns_the_pool_file(tmp_path):
    owner = _Ownership(0)
    # the controller secret comes from the environment: a real root install
    # writes the 0600 secret file itself (an unowned file would trip the
    # system-profile secret check before install even starts)
    ctx = make_context(
        tmp_path,
        environ={"MPM_SUBSCRIPTION_URL": "", "MPM_SECRET": "canary-root-install"},
        system_profile=True,
        geteuid=owner.geteuid,
        chown=owner.chown,
    )
    prepare_install(ctx)
    _write_pool(ctx)

    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    assert os.path.normpath(ctx.layout.pool_file) in owner.paths()


def test_install_copies_the_feishu_pool_script(tmp_path):
    ctx, _harness, _network = _installed(tmp_path)
    installed = os.path.join(ctx.layout.scripts_dir, "feishu_pool.py")
    assert os.path.isfile(installed)


def test_groups_reads_the_pool_file_in_pool_mode(tmp_path):
    ctx = _pool_context(tmp_path)
    prepare_install(ctx)
    _write_secrets(ctx)
    _write_pool(ctx)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    report = lifecycle.groups(ctx)

    assert report.status == "OK"
    assert report.data["provider_nodes"] == 3
    rows = {row["name"]: row for row in report.data["groups"]}
    assert rows["CURSOR-US"]["matched_nodes"] == 1
    assert rows["CURSOR-JP"]["matched_nodes"] == 1


class _FakePoolModule:
    """Stands in for the installed ``feishu_pool.py`` (records argv)."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.argvs: list[list[str]] = []

    def main(self, argv):
        self.argvs.append(list(argv))
        return self.code


def _update_sources_context(tmp_path):
    ctx = _pool_context(tmp_path)
    prepare_install(ctx)
    _write_secrets(ctx)
    return ctx


def test_update_sources_pool_branch_builds_the_pool(tmp_path):
    ctx = _update_sources_context(tmp_path)
    module = _FakePoolModule(0)
    destination = os.path.join(ctx.layout.providers, "candidate.yaml")
    os.makedirs(ctx.layout.providers, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write("proxies: []\n")

    report = lifecycle.update_sources(ctx, load_module=lambda _path: module)

    assert report.status == "OK"
    assert report.data["mode"] == "feishu-pool"
    assert report.data["exit_code"] == 0
    assert any("candidate pool written" in message for message in report.messages)
    assert len(module.argvs) == 1
    argv = module.argvs[0]
    assert argv[argv.index("--env-file") + 1] == ctx.layout.secret_file
    assert argv[argv.index("-o") + 1] == destination


def test_update_sources_pool_branch_passes_min_nodes(tmp_path):
    ctx = _update_sources_context(tmp_path)
    module = _FakePoolModule(0)
    destination = os.path.join(ctx.layout.providers, "candidate.yaml")
    os.makedirs(ctx.layout.providers, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write("proxies: []\n")

    lifecycle.update_sources(ctx, min_nodes=5, load_module=lambda _path: module)

    argv = module.argvs[0]
    assert argv[argv.index("--min-nodes") + 1] == "5"


def test_update_sources_pool_below_minimum_maps_to_exit_4(tmp_path):
    ctx = _update_sources_context(tmp_path)
    module = _FakePoolModule(4)

    report = lifecycle.update_sources(ctx, load_module=lambda _path: module)

    assert report.status == "FAILED"
    assert report.exit_code == 4
    assert report.data["mode"] == "feishu-pool"
    assert any("Feishu pool below the minimum" in message for message in report.messages)


def test_update_sources_pool_failure_exit_raises(tmp_path):
    ctx = _update_sources_context(tmp_path)
    module = _FakePoolModule(3)

    with pytest.raises(FailClosed) as excinfo:
        lifecycle.update_sources(ctx, load_module=lambda _path: module)

    assert "Feishu pool build failed (exit 3)" in str(excinfo.value)


def test_update_sources_refuses_the_pool_file_as_destination(tmp_path):
    ctx = _update_sources_context(tmp_path)

    with pytest.raises(FailClosed):
        lifecycle.update_sources(ctx, output=ctx.layout.pool_file)


def test_update_sources_subscription_branch_reports_its_mode(tmp_path):
    ctx = make_context(tmp_path)  # default environment: a subscription URL
    prepare_install(ctx)
    module = _FakePoolModule(0)
    destination = os.path.join(ctx.layout.providers, "candidate.yaml")
    os.makedirs(ctx.layout.providers, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write("proxies: []\n")

    report = lifecycle.update_sources(ctx, load_module=lambda _path: module)

    assert report.status == "OK"
    assert report.data["mode"] == "subscription"
    assert module.argvs[0][module.argvs[0].index("--sources-env-file") + 1] == ctx.layout.secret_file
