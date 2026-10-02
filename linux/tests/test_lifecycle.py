"""Section 11 test categories 3, 4, 5, 7 and 9: lifecycle, idempotency, DEGRADED,
CN fallback prevention, uninstall/purge, port guard.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import support
from mpm import cli
from mpm import config as config_mod
from mpm import lifecycle, state as state_mod
from mpm import unit as unit_mod
from mpm.errors import ExitCode, FailClosed, MpmError, NotReady, Unsupported
from mpm.paths import UNIT_NAME
from mpm import preflight as preflight_mod

FOREIGN = support.FOREIGN
CN = support.CN


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_install_produces_expected_layout(self) -> None:
        self.sb.installed()
        layout = self.sb.layout
        self.assertTrue(os.path.isfile(layout.unit))
        self.assertTrue(os.path.islink(layout.libexec_current))
        self.assertTrue(os.path.isfile(layout.binary))
        self.assertEqual(support.file_mode(layout.binary), 0o755)
        self.assertEqual(support.file_mode(layout.config), 0o600)
        self.assertEqual(support.file_mode(layout.controller_secret_file), 0o600)
        self.assertEqual(support.file_mode(layout.cli_entry), 0o755)
        self.assertEqual(support.file_mode(layout.state_dir), 0o700)
        self.assertEqual(support.file_mode(layout.providers), 0o700)
        self.assertEqual(support.file_mode(layout.staging_dir), 0o700)

    def test_install_starts_unit_and_reports_verified_asset(self) -> None:
        self.sb.installed()
        report = self.sb.install_report
        self.assertEqual(report.status, "OK")
        binary = report.data["binary"]
        self.assertEqual(binary["asset"], "mihomo-linux-amd64-v3-v0.0.0-mock.gz")
        self.assertEqual(len(binary["sha256"]), 64)
        self.assertEqual(self.sb.executor.count("systemctl start"), 1)
        self.assertTrue(self.sb.systemd_state.active)

    def test_install_does_not_enable_by_default(self) -> None:
        self.sb.installed(enable=False)
        self.assertFalse(self.sb.systemd_state.enabled)
        self.assertIn("enable state left untouched", " ".join(self.sb.install_report.messages))

    def test_install_enable_flag_is_opt_in(self) -> None:
        self.sb.installed(enable=True)
        self.assertTrue(self.sb.systemd_state.enabled)
        self.assertEqual(self.sb.executor.count("systemctl enable"), 1)

    def test_install_twice_is_idempotent(self) -> None:
        first = self.sb.installed()
        unit_before = support.read_text(self.sb.layout.unit)
        config_before = support.read_text(self.sb.layout.config)
        self.sb.executor.reset()

        second = lifecycle.install(self.sb.ctx(), quiet=True)
        self.assertEqual(second.status, "OK")
        # no second instance: start is a no-op while the unit is active
        self.assertEqual(self.sb.executor.count("systemctl start"), 0)
        self.assertEqual(support.read_text(self.sb.layout.unit), unit_before)
        self.assertEqual(support.read_text(self.sb.layout.config), config_before)
        self.assertIn("unit unchanged", second.messages)
        self.assertTrue(first)

    def test_install_never_writes_outside_the_sandbox_root(self) -> None:
        self.sb.installed()
        for path in ("/etc/" + "mihomo-proxy-management", "/usr/local/bin/mihomo-proxy-management"):
            self.assertFalse(os.path.exists(path), f"host path touched: {path}")

    def test_install_without_secret_fails_closed(self) -> None:
        ctx = self.sb.ctx()
        with self.assertRaises(FailClosed) as caught:
            lifecycle.install(ctx)
        self.assertIn("MPM_SUBSCRIPTION_URL is required", str(caught.exception))
        self.assertFalse(self.sb.systemd_state.active)
        self.assertFalse(os.path.exists(self.sb.layout.unit))

    def test_install_rejects_world_readable_secret(self) -> None:
        self.sb.write_secret_file(mode=0o644)
        with self.assertRaises(FailClosed):
            lifecycle.install(self.sb.ctx())
        self.assertFalse(os.path.isfile(self.sb.layout.config))

    def test_install_validates_config_with_the_pinned_binary(self) -> None:
        self.sb.installed()
        tested = [
            list(call.argv)
            for call in self.sb.executor.calls
            if call.argv[0] == self.sb.layout.binary
        ]
        self.assertEqual(len(tested), 1, tested)
        argv = tested[0]
        self.assertEqual(
            argv, [self.sb.layout.binary, "-t", "-f", argv[3], "-d", self.sb.layout.var_lib],
            "the pinned binary must be invoked directly, testing the candidate",
        )
        # the validated file is the candidate in the staging area, never the
        # live config (which does not exist yet on a first install)
        candidate = argv[3]
        self.assertTrue(candidate.startswith(self.sb.layout.staging_dir + os.sep), candidate)
        self.assertNotEqual(candidate, self.sb.layout.config)
        self.assertFalse(os.path.exists(candidate), "staging area must be cleaned up")

    def test_first_install_validates_the_candidate_not_a_live_config(self) -> None:
        """``mihomo -t`` must be pointed at the file that is about to go live."""
        layout = self.sb.layout
        self.sb.write_secret_file()
        seen: dict[str, object] = {}

        def probe(argv: list[str]) -> tuple[int, str, str]:
            candidate = argv[argv.index("-f") + 1]
            seen["live_exists"] = os.path.isfile(layout.config)
            seen["candidate"] = candidate
            with open(candidate, "r", encoding="utf-8") as handle:
                seen["text"] = handle.read()
            seen["mode"] = support.file_mode(candidate)
            return (0, "", "")

        self.sb.executor.set(layout.binary, probe)
        self.assertFalse(os.path.isfile(layout.config), "precondition: nothing installed yet")
        lifecycle.install(self.sb.ctx(), quiet=True)
        self.assertEqual(self.sb.executor.count(layout.binary), 1)
        self.assertFalse(seen.get("live_exists", True), "a first install has no live config to test")
        self.assertNotEqual(seen["candidate"], layout.config)
        self.assertEqual(seen["mode"], 0o600)
        # what mihomo validated is byte-for-byte what got installed
        self.assertEqual(seen["text"], support.read_text(layout.config))

    def test_later_configure_validates_the_new_candidate(self) -> None:
        self.sb.installed()
        stale = support.read_text(self.sb.layout.config)
        self.sb.executor.reset()
        seen: dict[str, str] = {}

        def probe(argv: list[str]) -> tuple[int, str, str]:
            candidate = argv[argv.index("-f") + 1]
            with open(candidate, "r", encoding="utf-8") as handle:
                seen["text"] = handle.read()
            return (0, "", "")

        self.sb.executor.set(self.sb.layout.binary, probe)
        self.sb.forget_canaries()
        self.sb.canary_foreign = support.canary_url("foreign3")
        self.sb.write_secret_file()
        report = lifecycle.configure(self.sb.ctx())
        self.assertEqual(report.status, "CHANGED")
        self.assertNotEqual(seen["text"], stale, "the stale live config must not be the test input")
        self.assertIn(self.sb.canary_foreign, seen["text"])
        self.assertEqual(seen["text"], support.read_text(self.sb.layout.config))

    def test_candidate_rejection_keeps_the_previous_config(self) -> None:
        self.sb.installed()
        good = support.read_text(self.sb.layout.config)
        self.sb.executor.reset()
        self.sb.set_binary_test(returncode=1, stderr="mock yaml parse error")
        self.sb.forget_canaries()
        self.sb.canary_foreign = support.canary_url("foreign4")
        self.sb.write_secret_file()
        with self.assertRaises(FailClosed) as caught:
            lifecycle.configure(self.sb.ctx())
        self.assertIn("mihomo -t", str(caught.exception))
        self.assertEqual(support.read_text(self.sb.layout.config), good)
        self.assertNotIn("mock yaml parse error", str(caught.exception))

    def test_install_persists_plan_and_degraded_state(self) -> None:
        self.sb.installed()
        plan = state_mod.load_plan(self.sb.layout)
        self.assertEqual(plan.mode, "tun")
        self.assertEqual(plan.foreign_provider, FOREIGN)
        self.assertFalse(plan.cn_configured)
        degraded = state_mod.load_degraded(self.sb.layout)
        self.assertEqual(degraded.status(), state_mod.HEALTH_OK)
        # no CN subscription configured -> CN feature reports DISABLED (not a
        # silent OK, and never a silent foreign/DIRECT fallback claim)
        self.assertEqual(degraded.cn, "DISABLED")
        self.assertTrue(degraded.cn_details)

    def test_state_files_never_contain_secret_material(self) -> None:
        self.sb.installed()
        blob = ""
        for name in ("plan.json", "degraded.json", "backups/index.json"):
            path = os.path.join(self.sb.layout.state_dir, name)
            if os.path.isfile(path):
                blob += support.read_text(path)
        for canary in self.sb.canaries():
            self.assertNotIn(canary, blob, f"leaked into state: {canary}")


class DegradedModeTests(unittest.TestCase):
    def test_tun_failure_is_loud_and_explicit(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        self.assertEqual(sb.install_report.status, "DEGRADED")
        out = sb.outputs()
        self.assertIn("DEGRADED", out)
        self.assertIn("mixed-port", out)
        # explicit banner, not a single quiet line
        self.assertIn("!" * 72, out)
        self.assertTrue(sb.install_report.data["degraded"])
        self.assertEqual(state_mod.load_degraded(sb.layout).status(), "DEGRADED")
        self.assertEqual(state_mod.load_degraded(sb.layout).mode, "mixed")

    def test_degraded_config_has_no_hijack_and_no_autoroute(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        text = support.read_text(sb.layout.config)
        self.assertIn("  enable: false", text)
        self.assertIn("  dns-hijack: []", text)
        self.assertIn("  auto-route: false", text)

    def test_degraded_config_turns_fake_ip_off(self) -> None:
        """D5: mixed-port mode owns no traffic path, so fake-ip must be off."""
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        text = support.read_text(sb.layout.config)
        for token in (
            "enhanced-mode: fake-ip",
            "fake-ip-range",
            "fake-ip-filter",
            "store-fake-ip: true",
            "- any:53",
        ):
            self.assertNotIn(token, text)
        self.assertIn("  enhanced-mode: redir-host", text)
        self.assertIn("  store-fake-ip: false", text)
        # the installed config still passes the project's own audit
        config_mod.audit(text, cn_configured=False)
        # and the recorded state says why the host is degraded
        self.assertTrue(state_mod.load_degraded(sb.layout).reasons)

    def test_degraded_unit_grants_no_extra_capabilities(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        unit = support.read_text(sb.layout.unit)
        self.assertNotIn("CAP_SYS_ADMIN", unit)
        self.assertNotIn("AmbientCapabilities=", unit)

    def test_status_reports_degraded_reason(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        report = lifecycle.status(sb.ctx())
        self.assertEqual(report.status, "DEGRADED")
        self.assertTrue(report.data["degraded_reasons"])
        self.assertEqual(report.data["mode"], "mixed")

    def test_system_dns_and_routing_never_touched(self) -> None:
        # nothing in the project shell-executes a routing/DNS tool
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        argv_join = " ".join(" ".join(call.argv) for call in sb.executor.calls)
        for forbidden in ("ip route", "ip rule", "iptables", "nft", "resolvectl", "systemd-resolve"):
            self.assertNotIn(forbidden, argv_join)


class StartStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.ctx = self.sb.installed()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_start_twice_does_not_start_a_second_instance(self) -> None:
        self.sb.executor.reset()
        report = lifecycle.start(self.sb.ctx())
        self.assertEqual(report.status, "UNCHANGED")
        self.assertEqual(self.sb.executor.count("systemctl start"), 0)
        self.assertIn("no second instance", " ".join(report.messages))

    def test_stop_twice_is_safe(self) -> None:
        lifecycle.stop(self.ctx)
        self.assertFalse(self.sb.systemd_state.active)
        self.sb.executor.reset()
        second = lifecycle.stop(self.sb.ctx())
        self.assertEqual(second.status, "UNCHANGED")
        self.assertEqual(self.sb.executor.count("systemctl stop"), 0)

    def test_stop_only_uses_systemctl_on_our_unit(self) -> None:
        lifecycle.stop(self.ctx)
        calls = [" ".join(call.argv) for call in self.sb.executor.calls]
        stops = [c for c in calls if "stop" in c]
        self.assertTrue(stops)
        for call in stops:
            self.assertTrue(call.startswith("systemctl stop " + UNIT_NAME), call)
        joined = " ".join(calls)
        for forbidden in ("pkill", "killall", "pgrep", "kill -"):
            self.assertNotIn(forbidden, joined)

    def test_restart_stops_before_starting(self) -> None:
        self.sb.executor.reset()
        report = lifecycle.restart(self.ctx)
        self.assertIn(report.status, ("OK", "DEGRADED"))
        sequence = [" ".join(call.argv[:2]) for call in self.sb.executor.calls]
        self.assertLess(sequence.index("systemctl stop"), sequence.index("systemctl start"))

    def test_restart_reports_readiness(self) -> None:
        report = lifecycle.restart(self.ctx)
        self.assertEqual(report.data.get("version"), "mock-1.0.0")

    def test_start_refuses_foreign_port_owner_and_never_kills_it(self) -> None:
        lifecycle.stop(self.ctx)
        self.sb.executor.reset()
        self.sb.make_port_owner(7890, unit="somebody-else.service")
        with self.assertRaises(FailClosed) as caught:
            lifecycle.start(self.sb.ctx())
        self.assertIn("refusing to start", str(caught.exception))
        self.assertIn("will not terminate", str(caught.exception))
        self.assertEqual(self.sb.executor.count("systemctl start"), 0)
        joined = " ".join(" ".join(call.argv) for call in self.sb.executor.calls)
        for forbidden in ("kill", "pkill", "systemctl stop somebody-else"):
            self.assertNotIn(forbidden, joined)

    def test_start_refuses_unknown_process_owner(self) -> None:
        lifecycle.stop(self.ctx)
        self.sb.make_port_owner(9090, unit="")
        with self.assertRaises(FailClosed) as caught:
            lifecycle.start(self.sb.ctx())
        self.assertIn("foreign-process", str(caught.exception))

    def test_start_without_unit_fails_closed(self) -> None:
        sb = support.Sandbox()
        self.addCleanup(sb.cleanup)
        sb.write_secret_file()
        with self.assertRaises(FailClosed) as caught:
            lifecycle.start(sb.ctx())
        self.assertIn("unit is not installed", str(caught.exception))

    def test_readiness_failure_is_not_ready(self) -> None:
        self.sb.transport.responses = {"GET /version": (200, b"{}")}
        lifecycle.stop(self.ctx)
        self.sb.executor.reset()
        with self.assertRaises(NotReady):
            lifecycle.start(self.sb.ctx())

    def test_controller_unreachable_is_not_ready(self) -> None:
        self.sb.transport.responses = {"GET /version": (503, b"{}")}
        lifecycle.stop(self.ctx)
        with self.assertRaises(MpmError):
            lifecycle.start(self.sb.ctx())


class CnFallbackTests(unittest.TestCase):
    def test_cn_configured_is_isolated_and_healthy(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        report = lifecycle.run_test(sb.ctx())
        self.assertEqual(report.status, "OK")
        names = {check["name"] for check in report.data["checks"]}
        self.assertIn("provider.cn.exit", names)

    def test_cn_zero_nodes_degrades_without_fallback(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        sb.providers[CN] = support.provider_entry(CN, 0)
        report = lifecycle.status(sb.ctx())
        self.assertEqual(report.status, "DEGRADED")
        self.assertEqual(report.data["cn"], "DEGRADED")
        degraded = state_mod.load_degraded(sb.layout)
        self.assertEqual(degraded.cn, "DEGRADED")
        self.assertTrue(degraded.cn_details)
        # and it is persisted so later runs still shout about it
        start_report = lifecycle.start(sb.ctx())
        self.assertEqual(start_report.status, "DEGRADED")
        out = sb.outputs()
        self.assertIn("DEGRADED", out)
        config_text = support.read_text(sb.layout.config)
        self.assertNotIn("- DIRECT\n    ", config_text)
        groups = __import__("mpm.config", fromlist=["_groups"])._groups(config_text)
        self.assertNotIn("DIRECT", groups["CN-EXIT"]["proxies"])

    def test_cn_dead_nodes_report_is_sanitised(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        sb.providers[CN] = support.provider_entry(CN, 0)
        report = lifecycle.status(sb.ctx())
        blob = json.dumps(report.to_dict())
        for canary in sb.canaries():
            self.assertNotIn(canary, blob)

    def test_run_test_reports_all_dead_cn_nodes_as_failed(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        sb.providers[CN] = support.provider_entry(CN, 2, alive=False)
        report = lifecycle.run_test(sb.ctx())
        self.assertEqual(report.status, "FAILED", "a dead CN provider is a FAIL, never a soft pass")
        states = {check["name"]: check["result"] for check in report.data["checks"]}
        self.assertEqual(states.get("provider.cn.exit"), "FAIL")
        self.assertEqual(state_mod.load_degraded(sb.layout).cn, "DEGRADED")
        self.assertIn("  - GEOSITE,cn,REJECT", support.read_text(sb.layout.config))

    def test_persisted_degraded_never_masks_a_failed_check(self) -> None:
        """D12: DEGRADED explains state; it must not overwrite FAILED."""
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        self.assertEqual(state_mod.load_degraded(sb.layout).status(), "DEGRADED")
        # a real failure on top of the recorded degradation
        os.chmod(sb.layout.config, 0o644)
        report = lifecycle.run_test(sb.ctx())
        states = {check["name"]: check["result"] for check in report.data["checks"]}
        self.assertEqual(states.get("config.mode_0600"), "FAIL")
        self.assertEqual(report.status, "FAILED")
        # the CLI maps FAILED onto a non-zero exit code, never onto DEGRADED's 0
        os.chmod(sb.layout.config, 0o600)
        self.assertNotEqual(cli.emit(sb.ctx(), report, as_json=True), 0)
        out = sb.outputs()
        self.assertIn("DEGRADED", out, "the degradation is still reported, not swallowed")

    def test_run_test_still_degrades_without_a_failure(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        report = lifecycle.run_test(sb.ctx())
        self.assertEqual(report.status, "DEGRADED")
        self.assertTrue(all(c["result"] == "PASS" for c in report.data["checks"]))

    def test_missing_cn_never_silently_creates_cn_groups(self) -> None:
        sb = support.Sandbox(cn=False)
        self.addCleanup(sb.cleanup)
        sb.installed()
        text = support.read_text(sb.layout.config)
        self.assertNotIn("CN-EXIT", text)
        # no CN provider => not a single CN rule: no DIRECT row, and no silent
        # foreign fallback pretending to be CN routing either
        self.assertNotIn("GEOSITE,cn", text)
        self.assertNotIn("GEOIP,cn", text)
        rules = [line for line in text.splitlines() if line.startswith("  - ")]
        self.assertEqual(rules[-1], "  - MATCH,PROXY")
        report = lifecycle.status(sb.ctx())
        self.assertEqual(report.data["cn"], "DISABLED")

    def test_dead_cn_provider_switches_routing_to_an_explicit_refuse(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        live = support.read_text(sb.layout.config)
        self.assertIn("  - GEOSITE,cn,CN-EXIT", live)
        self.assertIn("  - GEOIP,cn,CN-EXIT,no-resolve", live)

        sb.providers[CN] = support.provider_entry(CN, 2, alive=False)
        report = lifecycle.status(sb.ctx())
        self.assertEqual(report.status, "DEGRADED")
        self.assertEqual(report.data["cn"], "DEGRADED")
        text = support.read_text(sb.layout.config)
        self.assertIn("  - GEOSITE,cn,REJECT", text)
        self.assertIn("  - GEOIP,cn,REJECT,no-resolve", text)
        self.assertFalse([r for r in config_mod._rendered_rules(text) if r.endswith(",CN-EXIT")])
        # CN-EXIT itself gains no fallback entry
        groups = config_mod._groups(text)
        self.assertEqual(groups["CN-EXIT"]["proxies"], ["AUTO-CN"])
        self.assertNotIn("DIRECT", groups["CN-EXIT"]["proxies"])
        # and the refusal is explained in the state that survives the process
        degraded = state_mod.load_degraded(sb.layout)
        self.assertEqual(degraded.cn, "DEGRADED")
        self.assertIn("does not fall back to DIRECT or a foreign node", " ".join(degraded.cn_details))
        # the refuse rendering passes the audit for a dead provider
        config_mod.audit(text, cn_configured=True, cn_live=False)

    def test_empty_cn_provider_also_refuses_instead_of_falling_back(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        sb.providers[CN] = support.provider_entry(CN, 0)
        lifecycle.status(sb.ctx())
        text = support.read_text(sb.layout.config)
        self.assertIn("  - GEOSITE,cn,REJECT", text)
        self.assertFalse([r for r in config_mod._rendered_rules(text) if "cn," in r and not r.endswith("REJECT") and not r.endswith("REJECT,no-resolve")])

    def test_unreadable_liveness_is_degraded_not_optimistic(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        entry = dict(sb.providers[CN])
        entry["all"] = [{"name": node} for node in entry["vehicle"]]
        sb.providers[CN] = entry
        # /proxies must not carry the liveness either
        sb.transport.proxies_payload = {"proxies": {}}
        report = lifecycle.status(sb.ctx())
        self.assertEqual(report.data["cn"], "DEGRADED")
        degraded = state_mod.load_degraded(sb.layout)
        self.assertIn("liveness is unknown", " ".join(degraded.cn_details))
        self.assertIn("  - GEOSITE,cn,REJECT", support.read_text(sb.layout.config))

    def test_a_live_node_among_dead_ones_keeps_cn_exit(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        entry = support.provider_entry(CN, 3, alive=False)
        entry["all"][1]["alive"] = True
        sb.providers[CN] = entry
        report = lifecycle.status(sb.ctx())
        self.assertEqual(report.data["cn"], "OK")
        self.assertIn("  - GEOSITE,cn,CN-EXIT", support.read_text(sb.layout.config))


class ConfigureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.sb.write_secret_file()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_configure_twice_is_a_noop(self) -> None:
        first = lifecycle.configure(self.sb.ctx())
        self.assertEqual(first.status, "CHANGED")
        self.sb.executor.reset()
        second = lifecycle.configure(self.sb.ctx())
        self.assertEqual(second.status, "UNCHANGED")
        self.assertIn("config unchanged", " ".join(second.messages))
        self.assertEqual(self.sb.executor.count("mihomo"), 0)

    def test_config_written_atomically_at_0600(self) -> None:
        lifecycle.configure(self.sb.ctx())
        self.assertEqual(support.file_mode(self.sb.layout.config), 0o600)
        leftovers = [n for n in os.listdir(self.sb.layout.var_lib) if n.startswith(".mpm")]
        self.assertEqual(leftovers, [])

    def test_staging_directory_is_removed(self) -> None:
        lifecycle.configure(self.sb.ctx())
        entries = os.listdir(self.sb.layout.staging_dir) if os.path.isdir(self.sb.layout.staging_dir) else []
        self.assertEqual(entries, [])

    def test_backup_keeps_previous_config_and_index_has_no_contents(self) -> None:
        lifecycle.configure(self.sb.ctx())
        first = support.read_text(self.sb.layout.config)
        # change the input so the next render differs
        self.sb.forget_canaries()
        self.sb.canary_foreign = support.canary_url("foreign2")
        self.sb.write_secret_file()
        report = lifecycle.configure(self.sb.ctx())
        self.assertEqual(report.status, "CHANGED")
        index = state_mod.backup_index(self.sb.layout)
        entries = index["entries"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), {"name", "bytes", "sha256", "created_at"})
        blob = json.dumps(index)
        for canary in (first, self.sb.canary_foreign):
            self.assertNotIn(canary, blob)
        self.assertNotIn(self.sb.canary_secret, blob)

    def test_backup_window_is_bounded(self) -> None:
        for _ in range(5):
            lifecycle.configure(self.sb.ctx())
            self.sb.forget_canaries()
            self.sb.canary_foreign = support.canary_url("f")
            self.sb.write_secret_file()
        index = state_mod.backup_index(self.sb.layout)
        self.assertLessEqual(len(index["entries"]), 3)
        backups = [n for n in os.listdir(self.sb.layout.backups_dir) if n.startswith("config.")]
        self.assertLessEqual(len(backups), 3)

    def test_check_only_mode_writes_nothing(self) -> None:
        lifecycle.configure(self.sb.ctx())
        before = support.read_text(self.sb.layout.config)
        mtime = os.stat(self.sb.layout.config).st_mtime_ns
        report = lifecycle.configure(self.sb.ctx(), check_only=True)
        self.assertEqual(report.status, "UNCHANGED")
        self.assertEqual(support.read_text(self.sb.layout.config), before)
        self.assertEqual(os.stat(self.sb.layout.config).st_mtime_ns, mtime)

    def snapshot_state(self) -> dict[str, object]:
        """Everything ``configure`` may touch, keyed by path -> (mtime_ns, text)."""
        layout = self.sb.layout
        watched = [
            layout.config,
            layout.plan_file,
            layout.degraded_file,
            layout.backup_index,
        ]
        snap: dict[str, object] = {}
        for path in watched:
            snap[path] = None if not os.path.exists(path) else (
                os.stat(path).st_mtime_ns,
                support.read_text(path) if os.path.isfile(path) else None,
            )
        backups = layout.backups_dir
        snap[backups] = sorted(os.listdir(backups)) if os.path.isdir(backups) else None
        staging = layout.staging_dir
        snap[staging] = sorted(os.listdir(staging)) if os.path.isdir(staging) else None
        return snap

    def test_check_only_is_read_only_even_when_state_is_missing(self) -> None:
        """D10: the ExecStartPre gate must not create or repair any state."""
        lifecycle.configure(self.sb.ctx())
        self.sb.executor.reset()
        before = self.snapshot_state()
        report = lifecycle.configure(self.sb.ctx(), check_only=True)
        self.assertEqual(report.status, "UNCHANGED")
        self.assertEqual(self.snapshot_state(), before)
        # nothing at all is executed either: no mihomo -t, no systemctl
        self.assertEqual(self.sb.executor.calls, [])

    def test_check_only_writes_nothing_when_config_is_stale(self) -> None:
        lifecycle.configure(self.sb.ctx())
        with open(self.sb.layout.config, "a", encoding="utf-8") as handle:
            handle.write("\n# locally edited\n")
        edited = self.snapshot_state()
        self.sb.executor.reset()
        with self.assertRaises(FailClosed):
            lifecycle.configure(self.sb.ctx(), check_only=True)
        self.assertEqual(self.snapshot_state(), edited)
        self.assertEqual(self.sb.executor.calls, [])

    def test_check_only_writes_nothing_on_a_fresh_host(self) -> None:
        before = self.snapshot_state()
        self.sb.executor.reset()
        with self.assertRaises(FailClosed) as caught:
            lifecycle.configure(self.sb.ctx(), check_only=True)
        self.assertIn("has not been rendered yet", str(caught.exception))
        self.assertEqual(self.snapshot_state(), before)
        self.assertEqual(self.sb.executor.calls, [])

    def test_configure_does_not_reset_a_persisted_cn_degradation(self) -> None:
        sb = support.Sandbox(cn=True)
        self.addCleanup(sb.cleanup)
        sb.installed()
        sb.providers[CN] = support.provider_entry(CN, 2, alive=False)
        lifecycle.status(sb.ctx())
        self.assertEqual(state_mod.load_degraded(sb.layout).cn, "DEGRADED")
        # a plain re-render (idempotent configure) must not claim CN is healthy
        report = lifecycle.configure(sb.ctx())
        self.assertIn(report.status, ("CHANGED", "UNCHANGED"))
        degraded = state_mod.load_degraded(sb.layout)
        self.assertEqual(degraded.cn, "DEGRADED")
        self.assertTrue(degraded.cn_details)
        self.assertIn("  - GEOSITE,cn,REJECT", support.read_text(sb.layout.config))
        # and only a real health check clears it
        sb.providers[CN] = support.provider_entry(CN, 2, alive=True)
        lifecycle.status(sb.ctx())
        self.assertEqual(state_mod.load_degraded(sb.layout).cn, "OK")
        self.assertIn("  - GEOSITE,cn,CN-EXIT", support.read_text(sb.layout.config))

    def test_check_only_detects_manual_edit(self) -> None:
        lifecycle.configure(self.sb.ctx())
        with open(self.sb.layout.config, "a", encoding="utf-8") as handle:
            handle.write("\n# locally edited\n")
        with self.assertRaises(FailClosed) as caught:
            lifecycle.configure(self.sb.ctx(), check_only=True)
        self.assertIn("differs from the rendered config", str(caught.exception))

    def test_check_only_without_config_fails(self) -> None:
        with self.assertRaises(FailClosed):
            lifecycle.configure(self.sb.ctx(), check_only=True)

    def test_failed_mihomo_test_never_replaces_live_config(self) -> None:
        self.sb.installed()
        good = support.read_text(self.sb.layout.config)
        # now the pinned binary rejects the freshly rendered config
        self.sb.set_binary_test(returncode=1, stderr="mock yaml parse error")
        self.sb.forget_canaries()
        self.sb.canary_foreign = support.canary_url("other")
        self.sb.write_secret_file()
        with self.assertRaises(FailClosed) as caught:
            lifecycle.configure(self.sb.ctx())
        self.assertIn("mihomo -t", str(caught.exception))
        self.assertEqual(support.read_text(self.sb.layout.config), good)
        # mihomo's own stderr is never echoed back
        self.assertNotIn("mock yaml parse error", str(caught.exception))


class ExecStartPreTests(unittest.TestCase):
    """The unit's ExecStartPre gate: only project preflight/configure, and the
    ExecStart command line itself carries no secret material."""

    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.ctx = self.sb.installed()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_unit_exec_startpre_runs_project_configure(self) -> None:
        unit = support.read_text(self.sb.layout.unit)
        self.assertIn("configure --check-only", unit)

    def test_unit_exec_startpre_invokes_the_wrapper_directly(self) -> None:
        """D6: the gate runs the installed CLI wrapper, not python3 <wrapper>."""
        unit = support.read_text(self.sb.layout.unit)
        exec_pre = [
            line for line in unit.splitlines() if line.startswith("ExecStartPre=")
        ][0]
        program = exec_pre[len("ExecStartPre="):].split()[0]
        self.assertEqual(program, self.sb.layout.cli_entry)
        self.assertFalse(program.endswith(".py"))
        for interpreter in ("python", "python3", "/bin/sh", "/usr/bin/bash"):
            self.assertNotIn(interpreter, exec_pre)
        # the rendered unit passes the project's own unit audit
        unit_mod.audit(unit, tun=True)

    def test_unit_has_no_secret_or_url_text(self) -> None:
        unit = support.read_text(self.sb.layout.unit)
        for canary in self.sb.canaries():
            self.assertNotIn(canary, unit)
        for token in ("MPM_SUBSCRIPTION_URL", "MPM_CN_SUBSCRIPTION_URL", "Environment="):
            self.assertNotIn(token, unit)
        # ExecStart is exactly '<binary> -d <state dir>'
        exec_start = [
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        ][0]
        self.assertEqual(exec_start, f"ExecStart={self.sb.layout.binary} -d {self.sb.layout.var_lib}")
        self.assertNotIn("#", exec_start)


class UninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.ctx = self.sb.installed()
        os.makedirs(self.sb.layout.overrides_dir, exist_ok=True)
        support.write_atomic(
            os.path.join(self.sb.layout.overrides_dir, "40-user.conf"),
            "log_level = info\n# a clearly marked non-secret override\n",
            mode=0o640,
        )
        # provider state written by mihomo itself (secret-derived)
        support.write_atomic(
            os.path.join(self.sb.layout.providers, FOREIGN + ".yaml"),
            "proxies:\n  - name: leak-me\n",
            mode=0o600,
        )

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_dry_run_deletes_nothing(self) -> None:
        report = lifecycle.uninstall(self.sb.ctx(dry_run=True), assume_yes=True)
        self.assertTrue(report.data["dry_run"])
        self.assertTrue(os.path.isfile(self.sb.layout.config))
        self.assertTrue(os.path.isfile(self.sb.layout.unit))
        self.assertTrue(os.path.isfile(self.sb.layout.controller_secret_file))

    def test_normal_uninstall_removes_all_secret_material(self) -> None:
        report = lifecycle.uninstall(self.sb.ctx(), assume_yes=True)
        for path in (
            self.sb.layout.unit,
            self.sb.layout.subscription_env,
            self.sb.layout.controller_secret_file,
            self.sb.layout.config,
            self.sb.layout.providers,
            self.sb.layout.backups_dir,
            self.sb.layout.staging_dir,
            self.sb.layout.libexec,
            self.sb.layout.share,
            self.sb.layout.cli_entry,
        ):
            self.assertFalse(os.path.exists(path), f"still present: {path}")
        self.assertIn("kept /etc/mihomo-proxy-management/overrides.d", " ".join(report.messages))

    def test_normal_uninstall_keeps_marked_overrides(self) -> None:
        lifecycle.uninstall(self.sb.ctx(), assume_yes=True)
        self.assertTrue(os.path.isfile(os.path.join(self.sb.layout.overrides_dir, "40-user.conf")))

    def test_uninstall_twice_is_safe(self) -> None:
        lifecycle.uninstall(self.sb.ctx(), assume_yes=True)
        second = lifecycle.uninstall(self.sb.ctx(), assume_yes=True)
        self.assertEqual(second.status, "OK")
        self.assertEqual(second.data["removed_count"], 0)

    def test_uninstall_disables_unit_via_systemctl_only(self) -> None:
        lifecycle.uninstall(self.sb.ctx(), assume_yes=True)
        joined = " ".join(" ".join(call.argv) for call in self.sb.executor.calls)
        self.assertIn("systemctl disable --now " + UNIT_NAME, joined)
        for forbidden in ("pkill", "killall", "pgrep"):
            self.assertNotIn(forbidden, joined)

    def test_purge_requires_confirmation(self) -> None:
        with self.assertRaises(FailClosed) as caught:
            lifecycle.uninstall(self.sb.ctx(), purge=True, assume_yes=False)
        self.assertIn("--yes", str(caught.exception))
        self.assertTrue(os.path.isfile(self.sb.layout.unit))

    def test_purge_removes_overrides_too(self) -> None:
        report = lifecycle.uninstall(self.sb.ctx(), purge=True, assume_yes=True)
        self.assertTrue(report.data["removed_count"] > 0)
        self.assertFalse(os.path.isdir(self.sb.layout.overrides_dir))
        self.assertFalse(os.path.isdir(self.sb.layout.etc))
        self.assertFalse(os.path.isdir(self.sb.layout.var_lib))

    def test_purge_refuses_paths_outside_project_prefixes(self) -> None:
        layout = self.sb.layout
        self.assertFalse(layout.is_project_path(os.path.join(layout.root, "etc", "passwd")))
        self.assertFalse(layout.is_project_path(os.path.join(layout.root, "var", "log")))
        self.assertTrue(layout.is_project_path(layout.unit))
        self.assertTrue(layout.is_project_path(layout.var_lib))

    def test_uninstall_leaves_foreign_routing_state_untouched(self) -> None:
        report = lifecycle.uninstall(self.sb.ctx(), assume_yes=True)
        joined = " ".join(report.warnings).lower()
        self.assertIn("left untouched", joined)
        calls = " ".join(" ".join(call.argv) for call in self.sb.executor.calls)
        for forbidden in ("ip route del", "nft delete", "iptables -F"):
            self.assertNotIn(forbidden, calls)

    def test_uninstall_does_not_delete_users_or_journal(self) -> None:
        lifecycle.uninstall(self.sb.ctx(), purge=True, assume_yes=True)
        calls = " ".join(" ".join(call.argv) for call in self.sb.executor.calls)
        for forbidden in ("userdel", "groupdel", "journalctl --vacuum", "rm -rf /var/log"):
            self.assertNotIn(forbidden, calls)


class UpdateSubscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox(cn=True)
        self.ctx = self.sb.installed()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_refreshes_both_providers_with_auth(self) -> None:
        lifecycle.update_subscription(self.ctx)
        for call in self.sb.transport.calls:
            self.assertEqual(call["headers"].get("Authorization"), "Bearer " + self.sb.canary_secret)
        puts = [c["path"] for c in self.sb.transport.calls if c["method"] == "PUT"]
        self.assertEqual(sorted(puts), sorted([f"/providers/proxies/{FOREIGN}", f"/providers/proxies/{CN}"]))

    def test_refresh_failure_is_nonzero_and_no_restart(self) -> None:
        self.sb.transport.responses["PUT /providers/proxies/subscription-foreign"] = (500, b"{}")
        with self.assertRaises(FailClosed):
            lifecycle.update_subscription(self.sb.ctx())
        self.assertEqual(self.sb.executor.count("systemctl restart"), 0)
        self.assertEqual(self.sb.executor.count("systemctl stop"), 0)

    def test_cn_zero_nodes_after_refresh_degrades(self) -> None:
        self.sb.providers[CN] = support.provider_entry(CN, 0)
        with self.assertRaises(FailClosed):
            lifecycle.update_subscription(self.sb.ctx())
        degraded = state_mod.load_degraded(self.sb.layout)
        self.assertEqual(degraded.cn, "DEGRADED")

    def test_cn_all_dead_nodes_after_refresh_degrades_without_fallback(self) -> None:
        """Nodes exist but every one of them is dead - that is not health."""
        self.sb.providers[CN] = support.provider_entry(CN, 3, alive=False)
        with self.assertRaises(FailClosed) as caught:
            lifecycle.update_subscription(self.sb.ctx())
        self.assertIn("not pretending success", str(caught.exception))
        degraded = state_mod.load_degraded(self.sb.layout)
        self.assertEqual(degraded.cn, "DEGRADED")
        self.assertTrue(
            all("CN-EXIT will not select a dead node" in item for item in degraded.cn_details),
            degraded.cn_details,
        )
        self.assertIn("does not fall back to DIRECT or a foreign node", " ".join(degraded.cn_details))
        text = support.read_text(self.sb.layout.config)
        self.assertIn("  - GEOSITE,cn,REJECT", text)
        self.assertFalse([r for r in config_mod._rendered_rules(text) if r.endswith(",CN-EXIT")])
        self.assertNotIn("- DIRECT\n    ", text)

    def test_cn_liveness_unreadable_after_refresh_is_degraded(self) -> None:
        entry = dict(self.sb.providers[CN])
        entry["all"] = [{"name": node} for node in entry["vehicle"]]
        self.sb.providers[CN] = entry
        self.sb.transport.proxies_payload = {"proxies": {}}
        with self.assertRaises(FailClosed):
            lifecycle.update_subscription(self.sb.ctx())
        degraded = state_mod.load_degraded(self.sb.layout)
        self.assertEqual(degraded.cn, "DEGRADED")
        self.assertIn("  - GEOSITE,cn,REJECT", support.read_text(self.sb.layout.config))

    def test_unknown_provider_name_rejected(self) -> None:
        with self.assertRaises(FailClosed):
            lifecycle.update_subscription(self.sb.ctx(), provider="subscription-dre")

    def test_partial_refresh_failure_is_not_swallowed(self) -> None:
        self.sb.transport.responses["GET /providers/proxies/subscription-cn/healthcheck"] = (500, b"{}")
        with self.assertRaises(FailClosed):
            lifecycle.update_subscription(self.sb.ctx())


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.ctx = self.sb.installed()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_active_and_enabled_are_reported_separately(self) -> None:
        data = lifecycle.status(self.ctx).data
        self.assertEqual(data["unit"]["active_state"], "active")
        self.assertEqual(data["unit"]["enabled_state"], "disabled")
        self.assertNotIn('"main_pid":', json.dumps(data))
        self.assertTrue(data["unit"]["main_pid_present"])

    def test_partial_view_when_secret_unreadable(self) -> None:
        # root can read 0o000 files, so simulate the unprivileged case with a
        # path the project itself refuses to trust: a symlinked secret file.
        real = self.sb.layout.controller_secret_file + ".elsewhere"
        os.replace(self.sb.layout.controller_secret_file, real)
        os.symlink(real, self.sb.layout.controller_secret_file)
        report = lifecycle.status(self.sb.ctx())
        data = report.data
        self.assertTrue(data["privilege"]["partial_view"])
        self.assertIn("version", data["privilege"]["missing_fields"])
        self.assertIn("controller secret not readable", " ".join(report.warnings))
        # still useful: unit facts are present
        self.assertEqual(data["unit"]["active_state"], "active")

    def test_partial_view_when_secret_mode_is_too_wide(self) -> None:
        os.chmod(self.sb.layout.controller_secret_file, 0o644)
        report = lifecycle.status(self.sb.ctx())
        self.assertTrue(report.data["privilege"]["partial_view"])
        os.chmod(self.sb.layout.controller_secret_file, 0o600)

    def test_status_never_leaks_secrets(self) -> None:
        blob = json.dumps(lifecycle.status(self.ctx).to_dict())
        for canary in self.sb.canaries():
            self.assertNotIn(canary, blob)

    def test_status_reports_pinned_tag_and_ports(self) -> None:
        data = lifecycle.status(self.ctx).data
        self.assertEqual(data["ports"], {"mixed": 7890, "controller": 9090, "dns": 1053})
        self.assertTrue(data["binary"]["installed"])
        self.assertEqual(data["config"]["mode_ok"], True)


class PreflightCommandTests(unittest.TestCase):
    def test_preflight_reports_lock_summary(self) -> None:
        sb = support.Sandbox()
        self.addCleanup(sb.cleanup)
        report = lifecycle.run_preflight(sb.ctx())
        self.assertEqual(report.status, "OK")
        self.assertIn("assets", report.data["lock"])

    def test_preflight_degraded_status(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        report = lifecycle.run_preflight(sb.ctx())
        self.assertEqual(report.status, "DEGRADED")
        self.assertTrue(report.warnings)

    def test_preflight_unsupported_is_failed(self) -> None:
        sb = support.Sandbox(facts=support.make_facts(machine="riscv64"))
        self.addCleanup(sb.cleanup)
        report = lifecycle.run_preflight(sb.ctx())
        self.assertEqual(report.status, "FAILED")

    def test_preflight_without_os_release_is_unsupported(self) -> None:
        """D1: an unreadable /etc/os-release cannot be waved through."""
        facts = support.make_facts(root=True)
        facts.id_like = {}
        sb = support.Sandbox(facts=facts)
        self.addCleanup(sb.cleanup)
        report = lifecycle.run_preflight(sb.ctx())
        self.assertEqual(report.status, "FAILED")
        self.assertEqual(report.exit_code, ExitCode.UNSUPPORTED)
        self.assertIn("os-release", " ".join(report.warnings))
        # strict preflight raises rather than continuing
        with self.assertRaises(Unsupported):
            preflight_mod.preflight(facts)

    def test_collect_facts_from_a_root_without_os_release(self) -> None:
        """The host-fact reader itself must not invent a distro."""
        root = tempfile.mkdtemp(prefix="mpm-norelease-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        facts = preflight_mod.collect_facts(root=root, probe=False)
        self.assertEqual(facts.id_like, {})
        self.assertEqual(facts.distro(), "")
        self.assertEqual(facts.version(), "")
        # an empty os-release map is an unsupported host, never "skip the check"
        complete = support.make_facts(root=True)
        complete.id_like = facts.id_like
        result = preflight_mod.evaluate(complete)
        self.assertFalse(result.supported)
        self.assertIn("os-release", " ".join(result.reasons))


if __name__ == "__main__":
    unittest.main()
