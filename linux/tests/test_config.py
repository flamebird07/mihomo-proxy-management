"""Section 11 test category 1+2: config rendering, secret-input validation, overrides.

Everything here is offline and host-free: a throwaway root prefix, canary
subscriptions on ``example.invalid``, and no real binary is ever executed.
"""

from __future__ import annotations

import os
import stat
import unittest

import support
from mpm import config as config_mod
from mpm import secrets_io
from mpm.atomicio import check_secret_input, write_atomic
from mpm.errors import FailClosed
from mpm.preflight import evaluate

FOREIGN = support.FOREIGN
CN = support.CN


def render_for(
    sandbox,
    *,
    cn_url: str = "",
    health: str = "",
    controller: str = "s3cret-controller",
    cn_state: str = "OK",
) -> str:
    """Render directly from inputs (no filesystem writes)."""
    inputs = secrets_io.SecretInputs(
        foreign_url=sandbox.canary_foreign,
        cn_url=cn_url,
        controller_secret=controller,
        cn_healthcheck_url=health,
        interval="45",
    )
    preflight = evaluate(sandbox.facts)
    return config_mod.render(inputs, preflight, config_mod.Overrides(), cn_health=cn_state)


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_tun_mode_renders_transparent_setup(self) -> None:
        text = render_for(self.sb)
        config_mod.audit(text, cn_configured=False)
        self.assertIn("  enable: true", text)
        self.assertIn("  auto-route: true", text)
        self.assertIn("  dns-hijack:\n    - any:53", text)
        self.assertIn("enhanced-mode: fake-ip", text)

    def test_degraded_mode_disables_hijack_and_autoroute(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        text = render_for(sb)
        config_mod.audit(text, cn_configured=False)
        self.assertIn("  enable: false", text)
        self.assertIn("  auto-route: false", text)
        self.assertIn("  dns-hijack: []", text)
        self.assertNotIn("    - any:53", text)

    def test_loopback_only_bindings(self) -> None:
        text = render_for(self.sb)
        self.assertIn("external-controller: 127.0.0.1:9090", text)
        self.assertIn("allow-lan: false", text)
        self.assertIn("bind-address: 127.0.0.1", text)
        self.assertNotIn("0.0.0.0:9090", text)

    def test_no_firewall_directives_emitted(self) -> None:
        for tun in (True, False):
            sb = support.Sandbox(tun=tun)
            self.addCleanup(sb.cleanup)
            text = render_for(sb)
            for token in ("iptables", "nftables", "ip rule", "ip route", "sysctl"):
                self.assertNotIn(token, text.lower())

    def test_special_characters_in_url_cannot_break_yaml(self) -> None:
        awkward = (
            "https://provider.example.invalid/sub"
            "?token=abc$def&clash=1#frag\\\"quoted'\xc3\xa9\xf0\x9f\x87\xba"
        )
        sb = support.Sandbox(foreign_url=awkward)
        self.addCleanup(sb.cleanup)
        text = render_for(sb)
        config_mod.audit(text, cn_configured=False)
        self.assertIn("?token=abc$def&clash=1", text)
        # the value is double quoted with escaped quotes, never bare
        self.assertIn('\\"quoted', text)

    def test_cn_isolated_from_foreign(self) -> None:
        text = render_for(self.sb, cn_url=self.sb.canary_cn, health=self.sb.canary_health)
        config_mod.audit(text, cn_configured=True)
        groups = config_mod._groups(text)
        for name in config_mod.FOREIGN_ONLY_GROUPS:
            self.assertEqual(groups[name]["use"], [FOREIGN], f"{name} must use foreign only")
        for name in config_mod.CN_ONLY_GROUPS:
            self.assertEqual(groups[name]["use"], [CN], f"{name} must use CN only")
        self.assertEqual(groups["CN-EXIT"]["proxies"], ["AUTO-CN"])
        self.assertNotIn("DIRECT", groups["CN-EXIT"]["proxies"])

    def test_cn_rules_absent_when_not_configured(self) -> None:
        text = render_for(self.sb)
        self.assertNotIn("CN-EXIT", text)
        # no CN provider => no CN rules at all: a DIRECT row would both claim a
        # CN feature that does not exist and be a silent fallback
        self.assertNotIn("GEOSITE,cn", text)
        self.assertNotIn("GEOIP,cn", text)
        rules = [line for line in text.splitlines() if line.startswith("  - ")]
        self.assertEqual(rules[-1], "  - MATCH,PROXY")

    def test_cn_dead_provider_renders_an_explicit_refuse(self) -> None:
        text = render_for(
            self.sb, cn_url=self.sb.canary_cn, health=self.sb.canary_health,
            cn_state="DEGRADED",
        )
        config_mod.audit(text, cn_configured=True, cn_live=False)
        self.assertIn("  - GEOSITE,cn,REJECT", text)
        self.assertIn("  - GEOIP,cn,REJECT,no-resolve", text)
        # the CN groups still exist, but no *rule* may hand traffic to a dead
        # node, and there is no DIRECT/foreign fallback anywhere
        rules = config_mod._rendered_rules(text)
        self.assertFalse([r for r in rules if r.endswith(",CN-EXIT")])
        self.assertEqual(
            [r for r in rules if config_mod._rule_parts(r) and config_mod._rule_parts(r)[1] == "cn"],
            ["GEOSITE,cn,REJECT", "GEOIP,cn,REJECT,no-resolve"],
        )
        self.assertEqual(config_mod._groups(text)["CN-EXIT"]["use"], [CN])
        with self.assertRaises(FailClosed):
            config_mod.audit(text, cn_configured=True, cn_live=True)

    def test_cn_bypass_overrides_are_refused_while_cn_is_live(self) -> None:
        inputs = secrets_io.SecretInputs(
            foreign_url=self.sb.canary_foreign,
            cn_url=self.sb.canary_cn,
            controller_secret="s3cret-controller",
            cn_healthcheck_url=self.sb.canary_health,
            interval="45",
        )
        preflight = evaluate(self.sb.facts)
        # CN -> DIRECT, CN -> foreign, and the earlier-matching equivalents
        # (a CN-shaped domain suffix, a REJECT that contradicts health, an
        # everything-but-CN DIRECT row) must all fail closed.
        for rule in (
            "GEOSITE,cn,DIRECT",
            "DOMAIN-SUFFIX,cn,DIRECT",
            "GEOIP,cn,AUTO-FOREIGN,no-resolve",
            "GEOSITE,cn,GLOBAL",
            "GEOSITE,cn,REJECT",
        ):
            with self.subTest(rule=rule):
                with self.assertRaises(FailClosed):
                    config_mod.render(
                        inputs, preflight,
                        config_mod.Overrides(extra_rules=[rule]), cn_health="OK",
                    )
        # a harmless override is still rendered, and unknown shapes are refused
        text = config_mod.render(
            inputs,
            preflight,
            config_mod.Overrides(extra_rules=["DOMAIN-SUFFIX,internal.example,DIRECT"]),
            cn_health="OK",
        )
        config_mod.audit(text, cn_configured=True)
        self.assertIn("  - DOMAIN-SUFFIX,internal.example,DIRECT", text)
        for rule in ("BADTYPE,x,PROXY", "GEOSITE,cn,NOT-A-GROUP", "MATCH,PROXY"):
            with self.subTest(rule=rule):
                with self.assertRaises(FailClosed):
                    config_mod.render(
                        inputs, preflight,
                        config_mod.Overrides(extra_rules=[rule]), cn_health="OK",
                    )

    def test_cn_refuse_rejects_a_bypassing_override(self) -> None:
        inputs = secrets_io.SecretInputs(
            foreign_url=self.sb.canary_foreign,
            cn_url=self.sb.canary_cn,
            controller_secret="s3cret-controller",
            cn_healthcheck_url=self.sb.canary_health,
            interval="45",
        )
        preflight = evaluate(self.sb.facts)
        for rule, reason in (
            ("DOMAIN-SUFFIX,.cn,DIRECT", "direct"),
            ("GEOSITE,cn,DIRECT", "direct"),
            ("GEOSITE,cn,PROXY", "foreign"),
            ("GEOIP,cn,CN-EXIT,no-resolve", "dead"),
            ("GEOSITE,geolocation-!cn,DIRECT", "geolocation-!cn"),
            ("MATCH,PROXY", "match"),
        ):
            overrides = config_mod.Overrides(extra_rules=[rule])
            with self.subTest(rule=rule):
                with self.assertRaises(FailClosed) as caught:
                    config_mod.render(
                        inputs, preflight, overrides, cn_health="DEGRADED"
                    )
                self.assertIn(reason.lower(), str(caught.exception).lower())

    def test_degraded_mode_renders_no_fake_ip_at_all(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        text = render_for(sb)
        config_mod.audit(text, cn_configured=False)
        for token in (
            "enhanced-mode: fake-ip",
            "fake-ip-range",
            "fake-ip-filter",
            "store-fake-ip: true",
        ):
            self.assertNotIn(token, text)
        self.assertIn("  enhanced-mode: redir-host", text)
        self.assertIn("  store-fake-ip: false", text)

    def test_audit_rejects_fake_ip_and_hijack_in_degraded_mode(self) -> None:
        sb = support.Sandbox(tun=False)
        self.addCleanup(sb.cleanup)
        text = render_for(sb)
        mutations = {
            "enhanced-mode": text.replace(
                "  enhanced-mode: redir-host", "  enhanced-mode: fake-ip"
            ),
            "fake-ip-range": text.replace(
                "  enhanced-mode: redir-host",
                "  enhanced-mode: fake-ip\n  fake-ip-range: 198.18.0.1/16",
            ),
            "fake-ip-filter": text.replace(
                "  enhanced-mode: redir-host",
                "  enhanced-mode: redir-host\n  fake-ip-filter:\n    - '*.lan'",
            ),
            "store-fake-ip": text.replace(
                "  store-fake-ip: false", "  store-fake-ip: true"
            ),
            "hijack-entry": text.replace("  dns-hijack: []", "  dns-hijack:\n    - any:53"),
            "hijack-inline": text.replace("  dns-hijack: []", "  dns-hijack: [any:53]"),
            "hijack-tcp": text.replace(
                "  dns-hijack: []", "  dns-hijack:\n    - tcp://any:53"
            ),
        }
        for label, mutated in mutations.items():
            with self.subTest(label), self.assertRaises(FailClosed) as caught:
                config_mod.audit(mutated, cn_configured=False)
            self.assertIn("degraded mode", str(caught.exception))

    def test_tun_mode_still_requires_fake_ip_and_hijack(self) -> None:
        text = render_for(self.sb)
        degraded_clone = text.replace("  enable: true\n  stack: system", "  enable: false\n  stack: system")
        with self.assertRaises(FailClosed):
            config_mod.audit(text.replace("tun:\n  enable: true", "tun:\n  enable: false"), cn_configured=False)
        self.assertTrue(degraded_clone)

    def test_cn_rule_tampering_is_rejected_whichever_way(self) -> None:
        text = render_for(self.sb, cn_url=self.sb.canary_cn, health=self.sb.canary_health)
        for mutated, cn_live in (
            (text.replace("  - GEOSITE,cn,CN-EXIT", "  - GEOSITE,cn,DIRECT"), True),
            (text.replace("  - GEOIP,cn,CN-EXIT,no-resolve", "  - GEOIP,cn,PROXY,no-resolve"), True),
            (text.replace("  - GEOSITE,cn,CN-EXIT", "  - GEOSITE,cn,REJECT"), True),
            (text.replace("  - GEOSITE,cn,CN-EXIT", "  - GEOSITE,cn,DIRECT"), False),
        ):
            with self.subTest(policy=mutated.split("GEOSITE,cn,")[1].split("\n")[0]):
                with self.assertRaises(FailClosed):
                    config_mod.audit(mutated, cn_configured=True, cn_live=cn_live)

    def test_cn_rules_are_rejected_when_cn_is_not_configured(self) -> None:
        text = render_for(self.sb)
        injected = text.replace("  - MATCH,PROXY", "  - GEOSITE,cn,DIRECT\n  - MATCH,PROXY")
        with self.assertRaises(FailClosed) as caught:
            config_mod.audit(injected, cn_configured=False)
        self.assertIn("no CN rule", str(caught.exception))

    def test_tampered_config_is_rejected(self) -> None:
        text = render_for(self.sb)
        mutations = {
            "allow-lan": text.replace("allow-lan: false", "allow-lan: true"),
            "controller-bind": text.replace(
                "external-controller: 127.0.0.1:9090", "external-controller: 0.0.0.0:9090"
            ),
            "empty-secret": text.replace('secret: "s3cret-controller"', 'secret: ""'),
            "cn-fallback": render_for(
                self.sb, cn_url=self.sb.canary_cn, health=self.sb.canary_health
            ).replace("      - AUTO-CN\n", "      - AUTO-CN\n      - DIRECT\n"),
            "foreign-cn-leak": render_for(
                self.sb, cn_url=self.sb.canary_cn, health=self.sb.canary_health
            ).replace("  - name: PROXY\n    type: select\n    use:\n      - " + FOREIGN, "  - name: PROXY\n    type: select\n    use:\n      - " + CN),
            "unresolved-placeholder": text.replace("log-level: info", "log-level: __LOG__"),
        }
        for label, mutated in mutations.items():
            with self.subTest(label):
                with self.assertRaises(FailClosed):
                    config_mod.audit(mutated, cn_configured=label in ("cn-fallback", "foreign-cn-leak"))

    def test_render_is_deterministic(self) -> None:
        first = render_for(self.sb, cn_url=self.sb.canary_cn, health=self.sb.canary_health)
        second = render_for(self.sb, cn_url=self.sb.canary_cn, health=self.sb.canary_health)
        self.assertEqual(first, second)

    def test_provider_paths_use_relative_dirs(self) -> None:
        text = render_for(self.sb)
        self.assertIn(f"path: ./providers/{FOREIGN}.yaml", text)


class SecretInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def resolve(self, **kwargs):
        # never inherit the host environment in tests
        kwargs.setdefault("environ", {})
        return secrets_io.resolve(self.sb.layout, **kwargs)

    def test_0600_file_accepted(self) -> None:
        self.sb.write_secret_file(mode=0o600)
        inputs = self.resolve(system=False)
        self.assertEqual(inputs.foreign_url, self.sb.canary_foreign)
        self.assertEqual(inputs.origins["MPM_SUBSCRIPTION_URL"], "file")

    def test_0644_file_rejected(self) -> None:
        path = self.sb.write_secret_file(mode=0o644)
        with self.assertRaises(FailClosed) as caught:
            self.resolve(system=False)
        self.assertIn("permissions too wide", str(caught.exception))
        self.assertTrue(os.path.isfile(path))

    def test_0600_group_read_rejected(self) -> None:
        self.sb.write_secret_file(mode=0o640)
        with self.assertRaises(FailClosed):
            self.resolve(system=False)

    def test_0600_other_read_rejected(self) -> None:
        self.sb.write_secret_file(mode=0o601)
        with self.assertRaises(FailClosed):
            self.resolve(system=False)

    def test_symlink_rejected(self) -> None:
        real = self.sb.layout.subscription_env + ".real"
        write_atomic(real, f"MPM_SUBSCRIPTION_URL={self.sb.canary_foreign}\n", mode=0o600)
        os.symlink(real, self.sb.layout.subscription_env)
        with self.assertRaises(FailClosed) as caught:
            self.resolve(system=False)
        self.assertIn("symlink", str(caught.exception))

    def test_directory_rejected(self) -> None:
        os.makedirs(self.sb.layout.subscription_env, exist_ok=True)
        with self.assertRaises(FailClosed):
            self.resolve(system=False)

    def test_world_readable_secret_input_check_directly(self) -> None:
        path = self.sb.write_secret_file(mode=0o600)
        os.chmod(path, 0o666)
        with self.assertRaises(FailClosed):
            check_secret_input(path, require_root_owned=False)

    def test_missing_foreign_url_fails_closed(self) -> None:
        write_atomic(self.sb.layout.subscription_env, "MPM_CONTROLLER_SECRET=x\n", mode=0o600)
        with self.assertRaises(FailClosed) as caught:
            self.resolve(environ={}, system=False)
        self.assertIn("MPM_SUBSCRIPTION_URL is required", str(caught.exception))

    def test_empty_secret_in_file_is_generated_and_stored(self) -> None:
        write_atomic(
            self.sb.layout.subscription_env,
            f"MPM_SUBSCRIPTION_URL={self.sb.canary_foreign}\nMPM_CONTROLLER_SECRET=\n",
            mode=0o600,
        )
        inputs = self.resolve(environ={}, system=False)
        # env-var path is empty, stored/generated secret fills it in
        self.assertTrue(inputs.controller_secret)
        text = render_for(self.sb, controller=inputs.controller_secret)
        config_mod.audit(text, cn_configured=False)

    def test_non_http_scheme_rejected(self) -> None:
        write_atomic(
            self.sb.layout.subscription_env,
            "MPM_SUBSCRIPTION_URL=file:///etc/passwd\n",
            mode=0o600,
        )
        with self.assertRaises(FailClosed) as caught:
            self.resolve(environ={}, system=False)
        self.assertIn("http(s) URL", str(caught.exception))
        self.assertNotIn("/etc/passwd", str(caught.exception))

    def test_extra_line_in_secret_file_does_not_reach_config(self) -> None:
        write_atomic(
            self.sb.layout.subscription_env,
            "MPM_SUBSCRIPTION_URL=" + self.sb.canary_foreign + "\nallow-lan: true\n",
            mode=0o600,
        )
        inputs = self.resolve(environ={}, system=False)
        self.assertEqual(inputs.foreign_url, self.sb.canary_foreign)
        text = render_for(self.sb)
        self.assertNotIn("allow-lan: true", text)

    def test_env_and_file_conflict_fails_closed(self) -> None:
        self.sb.write_secret_file(mode=0o600)
        other = "https://other.example.invalid/sub?token=zzz"
        with self.assertRaises(FailClosed) as caught:
            self.resolve(environ={"MPM_SUBSCRIPTION_URL": other}, system=False)
        self.assertIn("both", str(caught.exception))
        # fingerprints only - never the values
        self.assertNotIn(other, str(caught.exception))
        self.assertNotIn(self.sb.canary_foreign, str(caught.exception))

    def test_env_only_source_is_allowed(self) -> None:
        self.sb.use_env_secrets()
        inputs = self.resolve(environ=dict(self.sb.environ), system=False)
        self.assertEqual(inputs.origins["MPM_SUBSCRIPTION_URL"], "env")
        self.assertEqual(inputs.foreign_url, self.sb.canary_foreign)

    def test_controller_secret_reused_across_runs(self) -> None:
        self.sb.use_env_secrets(include_cn=False)
        first = self.resolve(environ={"MPM_SUBSCRIPTION_URL": self.sb.canary_foreign}, system=False)
        self.assertTrue(first.generated_secret)
        second = self.resolve(environ={"MPM_SUBSCRIPTION_URL": self.sb.canary_foreign}, system=False)
        self.assertEqual(first.controller_secret, second.controller_secret)
        self.assertFalse(second.generated_secret)
        self.assertEqual(support.file_mode(self.sb.layout.controller_secret_file), 0o600)

    def test_cn_without_healthcheck_fails_closed(self) -> None:
        environ = {
            "MPM_SUBSCRIPTION_URL": self.sb.canary_foreign,
            "MPM_CN_SUBSCRIPTION_URL": self.sb.canary_cn,
        }
        with self.assertRaises(FailClosed) as caught:
            self.resolve(environ=environ, system=False)
        self.assertIn("MPM_CN_HEALTHCHECK_URL is required", str(caught.exception))

    def test_safe_dict_contains_no_secret_material(self) -> None:
        self.sb.write_secret_file(with_cn=True)
        inputs = self.resolve(system=False)
        blob = repr(inputs.as_safe_dict())
        for canary in self.sb.canaries():
            if canary:
                self.assertNotIn(canary, blob)
        self.assertIn("https://foreign.example.invalid#fp", blob)

    def test_interval_must_be_digits(self) -> None:
        environ = {
            "MPM_SUBSCRIPTION_URL": self.sb.canary_foreign,
            "MPM_SUBSCRIPTION_INTERVAL": "60;rm",
        }
        with self.assertRaises(FailClosed):
            self.resolve(environ=environ, system=False)

    def test_bom_and_crlf_secret_file_is_parsed(self) -> None:
        payload = "\ufeffMPM_SUBSCRIPTION_URL=" + self.sb.canary_foreign + "\r\n"
        write_atomic(self.sb.layout.subscription_env, payload, mode=0o600)
        inputs = self.resolve(environ={}, system=False)
        self.assertEqual(inputs.foreign_url, self.sb.canary_foreign)


class OverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        os.makedirs(self.sb.layout.overrides_dir, exist_ok=True)

    def tearDown(self) -> None:
        self.sb.cleanup()

    def write(self, name: str, text: str, *, mode: int = 0o640) -> str:
        path = os.path.join(self.sb.layout.overrides_dir, name)
        write_atomic(path, text, mode=mode)
        return path

    def test_allowed_override_is_applied(self) -> None:
        self.write("10-base.conf", "log_level = debug\ninterval_minutes = 15\n")
        overrides = config_mod.load_overrides([os.path.join(self.sb.layout.overrides_dir, "10-base.conf")])
        text = config_mod.render(
            secrets_io.SecretInputs(
                foreign_url=self.sb.canary_foreign, controller_secret="abc"
            ),
            evaluate(self.sb.facts),
            overrides,
        )
        self.assertIn("log-level: debug", text)
        self.assertIn(f"    interval: {15}", text)
        config_mod.audit(text, cn_configured=False)

    def test_denied_keys_cannot_be_overridden(self) -> None:
        for key in ("allow-lan", "external-controller", "secret", "mixed-port", "tun", "url"):
            with self.subTest(key=key):
                path = self.write("99-bad.conf", f"{key} = whatever\n")
                with self.assertRaises(FailClosed) as caught:
                    config_mod.load_overrides([path])
                self.assertIn("refused", str(caught.exception))
                os.unlink(path)

    def test_unknown_key_rejected(self) -> None:
        path = self.write("99-unknown.conf", "external_ui = true\n")
        with self.assertRaises(FailClosed):
            config_mod.load_overrides([path])

    def test_credentialed_url_in_override_rejected(self) -> None:
        path = self.write(
            "99-secret.conf",
            "rule += DOMAIN-SUFFIX,x.invalid\n# https://host.invalid/sub?token=supersecretvalue\n",
        )
        with self.assertRaises(FailClosed) as caught:
            config_mod.load_overrides([path])
        self.assertIn("non-secret", str(caught.exception))
        self.assertNotIn("supersecretvalue", str(caught.exception))

    def test_symlinked_override_rejected_by_lifecycle(self) -> None:
        real = self.write("real.conf", "log_level = info\n")
        link = os.path.join(self.sb.layout.overrides_dir, "00-link.conf")
        os.symlink(real, link)

        from mpm import lifecycle

        self.sb.write_secret_file()
        ctx = self.sb.ctx()
        with self.assertRaises(FailClosed) as caught:
            lifecycle.configure(ctx, quiet=True)
        self.assertIn("symlinked override", str(caught.exception))

    def test_extra_rule_is_rendered(self) -> None:
        path = self.write("20-rules.conf", "rule += DOMAIN-SUFFIX,internal.example,PROXY\n")
        overrides = config_mod.load_overrides([path])
        text = config_mod.render(
            secrets_io.SecretInputs(foreign_url=self.sb.canary_foreign, controller_secret="abc"),
            evaluate(self.sb.facts),
            overrides,
        )
        self.assertIn("  - DOMAIN-SUFFIX,internal.example,PROXY", text)
        config_mod.audit(text, cn_configured=False)

    def test_bad_boolean_and_cidr_rejected(self) -> None:
        for content in ("ipv6 = maybe\n", "fake_ip_range = not-a-cidr\n", "interval_minutes = -5\n"):
            path = self.write("30-bad.conf", content)
            with self.subTest(content=content), self.assertRaises(FailClosed):
                config_mod.load_overrides([path])
            os.unlink(path)


class ModeDecisionTests(unittest.TestCase):
    def test_tun_preferred_and_mixed_only_when_forced(self) -> None:
        good = evaluate(support.make_facts(tun=True))
        self.assertEqual(good.mode, "tun")
        self.assertEqual(good.degraded_reasons, [])

        for reason, kwargs in (
            ("missing tun device", {"tun": False}),
        ):
            with self.subTest(reason):
                result = evaluate(support.make_facts(**kwargs))
                self.assertEqual(result.mode, "mixed")
                self.assertTrue(result.degraded_reasons, "degradation must record a reason")

    def test_asset_suffix_selection(self) -> None:
        self.assertEqual(evaluate(support.make_facts(avx2=True)).asset_suffix, "amd64-v3")
        self.assertEqual(
            evaluate(support.make_facts(avx2=False)).asset_suffix, "amd64-compatible"
        )
        self.assertEqual(
            evaluate(support.make_facts(machine="aarch64")).asset_suffix, "arm64"
        )

    def test_unsupported_matrix(self) -> None:
        empty_os_release = support.make_facts()
        empty_os_release.id_like = {}
        blank_os_release = support.make_facts()
        blank_os_release.id_like = {"ID": "", "VERSION_ID": "", "VERSION_CODENAME": ""}
        cases = {
            "unsupported arch": support.make_facts(machine="riscv64"),
            "no systemd": support.make_facts(systemd=False),
            "not root": support.make_facts(root=False),
            "systemd-less container": support.make_facts(container=True, systemd=False),
            "untested distro": support.make_facts(distro="fedora", version="40"),
            "missing /etc/os-release": empty_os_release,
            "unreadable /etc/os-release": blank_os_release,
        }
        for label, facts in cases.items():
            with self.subTest(label):
                result = evaluate(facts)
                self.assertFalse(result.supported)
                self.assertTrue(result.reasons)
        for label in ("missing /etc/os-release", "unreadable /etc/os-release"):
            with self.subTest(f"{label} reason"):
                facts = (
                    empty_os_release
                    if label.startswith("missing")
                    else blank_os_release
                )
                reasons = " | ".join(evaluate(facts).reasons)
                self.assertIn("os-release", reasons)
                self.assertIn("cannot verify", reasons)

    def test_supported_distros_are_accepted(self) -> None:
        for distro, version in (("ubuntu", "22.04"), ("ubuntu", "24.04"), ("debian", "12")):
            with self.subTest(distro=distro, version=version):
                result = evaluate(support.make_facts(distro=distro, version=version))
                self.assertTrue(result.supported, result.reasons)
                self.assertEqual(result.mode, "tun")

    def test_loose_perms_constant(self) -> None:
        self.assertEqual(stat.S_IMODE(0o100600), 0o600)


if __name__ == "__main__":
    unittest.main()
