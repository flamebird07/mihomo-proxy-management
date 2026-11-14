"""TUN / mixed rendering, the static audit and the policy check."""

from __future__ import annotations

import secrets

import pytest
import yaml

from mpm_common import policy
from mpm_common.errors import FailClosed
from mpm_common.executor import FakeExecutor
from mpm_macos import config

from macos_test_support import CONFIG_TEMPLATE, ROOT, assert_posix_mode

pytestmark = pytest.mark.macos

# Runtime canary: the repository must not contain credential-shaped literals.
SECRET = "canary-" + secrets.token_hex(8)
URL = "https://sub.example.invalid/subscription"
CONVERT_SUB = ROOT / "scripts" / "convert_sub.py"


def _template() -> str:
    return CONFIG_TEMPLATE.read_text(encoding="utf-8")


def _render(mode: str = "tun") -> str:
    return config.render(
        _template(),
        secret=SECRET,
        subscription_url=URL,
        interval_minutes=30,
        mode=mode,
    )


def test_tun_render_switches_the_technical_fields():
    document = yaml.safe_load(_render("tun"))
    assert document["mode"] == "rule"
    assert document["mixed-port"] == 7890
    assert document["external-controller"] == "127.0.0.1:9090"
    assert document["secret"] == SECRET
    assert document["tun"]["enable"] is True
    assert document["tun"]["auto-route"] is True
    assert document["tun"]["auto-detect-interface"] is True
    assert document["tun"]["stack"] == "gvisor"
    assert document["dns"]["enhanced-mode"] == "fake-ip"
    assert document["proxy-providers"]["subscription"]["interval"] == 3600
    assert document["proxy-providers"]["subscription"]["url"] == URL


def test_mixed_render_uses_redir_host_and_no_fake_ip():
    document = yaml.safe_load(_render("mixed"))
    assert document["tun"]["enable"] is False
    assert document["tun"]["auto-route"] is False
    assert document["tun"]["dns-hijack"] == []
    assert document["dns"]["enhanced-mode"] == "redir-host"
    assert "fake-ip-range" not in document["dns"]
    assert "fake-ip-filter" not in document["dns"]
    assert document["profile"]["store-fake-ip"] is False


def test_render_keeps_the_production_rule_order():
    document = yaml.safe_load(_render("tun"))
    rules = document["rules"]
    assert rules[0] == "DOMAIN,ark.cn-beijing.volces.com,DIRECT"
    assert rules.index("DOMAIN-SUFFIX,superboss.cc,DIRECT") < rules.index(
        "GEOSITE,tiktok,VIETNAM"
    )
    assert rules.index("DOMAIN-SUFFIX,jinritemai.com,DIRECT") < rules.index(
        "GEOSITE,tiktok,VIETNAM"
    )
    assert "DOMAIN-SUFFIX,codebuddy.ai,CODEBUDDY" in rules


def test_render_never_inlines_nodes_and_has_no_placeholders():
    text = _render("tun")
    document = yaml.safe_load(text)
    assert "proxies" not in document
    assert "__SUBSCRIPTION_URL__" not in text
    assert "__SECRET__" not in text
    assert "__SUBSCRIPTION_INTERVAL__" not in text


@pytest.mark.parametrize("mode", ["tun", "mixed"])
def test_render_disables_geo_auto_update(mode):
    """``mihomo -t`` must never fetch geo data from GitHub during install."""
    assert yaml.safe_load(_render(mode))["geo-auto-update"] is False


def test_audit_rejects_geo_auto_update():
    document = yaml.safe_load(_render("tun"))
    document["geo-auto-update"] = True
    broken = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    with pytest.raises(config.RenderError):
        config.audit(broken, mode="tun")


def test_ensure_geodata_fails_closed_before_mihomo_runs(tmp_path):
    with pytest.raises(config.RenderError) as excinfo:
        config.ensure_geodata(_render("tun"), str(tmp_path))
    message = str(excinfo.value)
    assert "GeoIP.dat" in message and "GeoSite.dat" in message
    assert "MPM_MIHOMO_BINARY" in message


def test_ensure_geodata_passes_when_both_files_are_present(tmp_path):
    text = _render("tun")
    (tmp_path / "GeoIP.dat").write_bytes(b"geo")
    (tmp_path / "GeoSite.dat").write_bytes(b"site")
    config.ensure_geodata(text, str(tmp_path))


def test_required_geodata_is_empty_without_geodata_mode():
    document = yaml.safe_load(_render("tun"))
    document["geodata-mode"] = False
    assert config.required_geodata(yaml.safe_dump(document, sort_keys=False)) == ()
    assert config.required_geodata(_render("tun")) == ("GeoIP.dat", "GeoSite.dat")


def test_render_requires_a_secret_and_a_url():
    with pytest.raises(config.RenderError):
        config.render(_template(), secret="", subscription_url=URL)
    with pytest.raises(config.RenderError):
        config.render(_template(), secret=SECRET, subscription_url="")


def test_audit_rejects_a_reordered_first_rule():
    document = yaml.safe_load(_render("tun"))
    rules = document["rules"]
    rules[0], rules[1] = rules[1], rules[0]
    broken = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    with pytest.raises(config.RenderError):
        config.audit(broken, mode="tun")


def test_audit_rejects_mixed_mode_with_fake_ip():
    document = yaml.safe_load(_render("mixed"))
    document["dns"]["enhanced-mode"] = "fake-ip"
    broken = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    with pytest.raises(config.RenderError):
        config.audit(broken, mode="mixed")


def test_audit_rejects_a_drifted_region_filter():
    document = yaml.safe_load(_render("tun"))
    for group in document["proxy-groups"]:
        if group["name"] == "CURSOR-US":
            group["filter"] = "(?i)usa"
    broken = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    with pytest.raises(config.RenderError):
        config.audit(broken, mode="tun")


def test_template_filters_match_the_policy_constants():
    document = yaml.safe_load(_template())
    groups = {group["name"]: group for group in document["proxy-groups"]}
    for name, pattern in policy.expected_filters().items():
        assert groups[name]["filter"] == pattern
        assert groups[name]["type"] == "fallback"
        assert groups[name]["use"] == ["subscription"]


def test_template_has_no_inline_nodes():
    document = yaml.safe_load(_template())
    assert "proxies" not in document
    valid = {group["name"] for group in document["proxy-groups"]} | {"DIRECT"}
    for group in document["proxy-groups"]:
        for member in group.get("proxies") or []:
            assert member in valid, f"group {group['name']} references unknown {member}"


def test_policy_fails_closed_when_cursor_has_no_region_node():
    with pytest.raises(FailClosed):
        policy.enforce(["香港-01", "新加坡-01"])


def test_policy_warns_but_allows_missing_codebuddy_regions():
    report = policy.enforce(["美国-01", "日本-01"])
    assert report.ok is True
    assert report.cursor == {"US": 1, "JP": 1}
    assert report.codebuddy["HK"] == 0
    assert any("CODEBUDDY-HK" in warning for warning in report.warnings)


def test_convert_provider_uses_convert_sub(tmp_path):
    module = config.load_convert_module(str(CONVERT_SUB))
    content = (
        "proxies:\n"
        "  - name: 美国-01\n"
        "    type: ss\n"
        "    server: 192.0.2.1\n"
        "    port: 8388\n"
        "    cipher: aes-128-gcm\n"
        "    password: test-only\n"
    )
    module.fetch_subscription = lambda url, timeout, ua: content
    destination = tmp_path / "subscription.yaml"
    names = config.convert_provider(
        "https://sub.example.invalid/sub", str(destination), module=module
    )
    assert names == ["美国-01"]
    assert_posix_mode(destination, 0o600)
    assert config.provider_node_names(destination.read_text(encoding="utf-8")) == ["美国-01"]


def test_convert_provider_rejects_an_empty_subscription(tmp_path):
    module = config.load_convert_module(str(CONVERT_SUB))
    module.fetch_subscription = lambda url, timeout, ua: "proxies: []\n"
    with pytest.raises(config.RenderError):
        config.convert_provider(
            "https://sub.example.invalid/sub", str(tmp_path / "out.yaml"), module=module
        )


def test_validate_fails_on_mihomo_rejection(tmp_path):
    executor = FakeExecutor()
    executor.set("/usr/local/bin/mihomo -t", (1, "", "bad config"))
    with pytest.raises(config.RenderError):
        config.validate("/usr/local/bin/mihomo", str(tmp_path / "c.yaml"), str(tmp_path), executor)


def test_validate_passes_on_success(tmp_path):
    executor = FakeExecutor()
    config.validate("/usr/local/bin/mihomo", str(tmp_path / "c.yaml"), str(tmp_path), executor)
    call = executor.find("/usr/local/bin/mihomo", "-t")
    assert call is not None
    assert call.argv[0] == "/usr/local/bin/mihomo"
    assert "-f" in call.argv and "-d" in call.argv


# ---- macOS-only policy regression tests (2026-11-14 outage) ---------------


def test_region_filters_do_not_match_russia_australia():
    """The old ``美|us|usa`` filter matched Russia/Australia and broke failover."""
    us = config.REGION_FILTERS["US"]
    not_us = ["Russia", "Australia", "Austria", "Belarus", "俄罗斯 RU", "澳大利亚 AU"]
    for name in not_us:
        assert not policy.matches(name, us), name
    for name in ("🇺🇸 US Mocha", "美国 01 AnyTLS", "USA-2"):
        assert policy.matches(name, us), name

    assert not policy.matches("Russia", config.REGION_FILTERS["SG"])
    assert not policy.matches("Japan", config.REGION_FILTERS["SG"])
    assert policy.matches("新加坡 SG-01", config.REGION_FILTERS["SG"])
    assert policy.matches("日本 JP-01", config.REGION_FILTERS["JP"])


def test_provider_interval_seconds_minimum_3600():
    assert config.provider_interval_seconds(1) == 3600
    assert config.provider_interval_seconds(30) == 3600
    assert config.provider_interval_seconds(60) == 3600
    assert config.provider_interval_seconds(120) == 7200
    # zero is a configuration mistake, not "use the floor"
    with pytest.raises(config.RenderError):
        config.provider_interval_seconds(0)
    with pytest.raises(config.RenderError):
        config.provider_interval_seconds(-5)

    document = yaml.safe_load(_render("tun"))
    provider = document["proxy-providers"]["subscription"]
    assert provider["interval"] == 3600
    assert provider["health-check"]["interval"] == 300
    assert provider["health-check"]["lazy"] is True
    assert provider["path"], "the provider path must stay (offline cache fallback)"

    provider["interval"] = 60
    broken = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    with pytest.raises(config.RenderError):
        config.audit(broken, mode="tun")


def test_gpt_group_order_us_sg_jp():
    document = yaml.safe_load(_render("tun"))
    groups = {group["name"]: group for group in document["proxy-groups"]}
    order = [group["name"] for group in document["proxy-groups"]]
    assert order.index("GPT-US") < order.index("GPT-SG") < order.index("GPT-JP") < order.index("GPT")
    assert groups["GPT"]["type"] == "fallback"
    assert groups["GPT"]["proxies"] == ["GPT-US", "GPT-SG", "GPT-JP"]
    assert groups["GPT"]["url"] == "https://chatgpt.com/cdn-cgi/trace"
    assert groups["GPT"]["interval"] == 300
    assert groups["GPT"]["max-failed-times"] == 3
    for name, region in (("GPT-US", "US"), ("GPT-SG", "SG"), ("GPT-JP", "JP")):
        group = groups[name]
        assert group["type"] == "fallback"
        assert group["use"] == ["subscription"]
        assert group["filter"] == config.REGION_FILTERS[region]
        assert group["url"] == "https://chatgpt.com/cdn-cgi/trace"
        assert group["expected-status"] == 200
        assert group["interval"] == 300
        assert group["timeout"] == 8000
        assert group["lazy"] is False
        assert group["max-failed-times"] == 3
    assert "GPT" in groups["GLOBAL"]["proxies"]

    rules = document["rules"]
    assert "GEOSITE,openai,GPT" in rules
    assert "DOMAIN-SUFFIX,chatgpt.com,GPT" in rules
    assert "DOMAIN-SUFFIX,openai.com,GPT" in rules
    assert "GEOSITE,openai,US-FAST" not in rules
    gpt_index = min(
        rules.index("GEOSITE,openai,GPT"),
        rules.index("DOMAIN-SUFFIX,chatgpt.com,GPT"),
        rules.index("DOMAIN-SUFFIX,openai.com,GPT"),
    )
    us_fast_or_proxy = [
        index
        for index, rule in enumerate(rules)
        if rule.endswith(",US-FAST") or rule.endswith(",PROXY")
    ]
    assert gpt_index < min(us_fast_or_proxy)


def test_us_fast_is_fallback_strict():
    document = yaml.safe_load(_render("tun"))
    groups = {group["name"]: group for group in document["proxy-groups"]}
    us_fast = groups["US-FAST"]
    assert us_fast["type"] == "fallback"
    assert us_fast["filter"] == config.US_FILTER
    assert us_fast["url"] == "https://www.gstatic.com/generate_204"
    assert us_fast["interval"] == 180
    assert us_fast["timeout"] == 5000
    assert us_fast["lazy"] is False
    assert us_fast["max-failed-times"] == 3
    assert "tolerance" not in us_fast


def test_proxy_select_has_no_raw_nodes_and_store_selected_false():
    document = yaml.safe_load(_render("tun"))
    groups = {group["name"]: group for group in document["proxy-groups"]}
    proxy = groups["PROXY"]
    assert proxy["type"] == "select"
    assert "use" not in proxy
    assert proxy["proxies"] == ["AUTO-FOREIGN", "US-FAST", "DIRECT"]
    assert document["profile"]["store-selected"] is False

    proxy["use"] = ["subscription"]
    broken = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    with pytest.raises(config.RenderError):
        config.audit(broken, mode="tun")


def test_fake_ip_range_avoids_198_18():
    import ipaddress

    document = yaml.safe_load(_render("tun"))
    assert document["dns"]["fake-ip-range"] == "198.19.0.1/16"
    network = ipaddress.ip_network(document["dns"]["fake-ip-range"], strict=False)
    assert not network.overlaps(ipaddress.ip_network("198.18.0.0/16"))

    mixed = yaml.safe_load(_render("mixed"))
    assert "fake-ip-range" not in mixed["dns"]

    document["dns"]["fake-ip-range"] = "198.18.0.1/16"
    broken = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    with pytest.raises(config.RenderError):
        config.audit(broken, mode="tun")
