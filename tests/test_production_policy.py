"""Production policy consistency tests for config/config.yaml.template.

Single source of truth for Windows/macOS production policy (网管裁定 2026-11-13,
impl/RULINGS.md sections "策略组" and "生产规则"):

  * explicit ``mode: rule``, mixed-port 7890, external-controller 127.0.0.1:9090;
  * rules[0] is the Volcengine Ark DIRECT rule;
  * the five cursor domains route to CURSOR (fallback, US-then-JP via
    provider-filter subgroups, no DIRECT);
  * superboss.cc / jinritemai.com DIRECT before GEOSITE,tiktok;
  * codebuddy.ai routes to CODEBUDDY (fallback, DIRECT first, then HK/TW/SG/JP);
  * region subgroups take members only from the subscription provider via
    ``use`` + ``filter`` -- the template itself never lists node names;
  * placeholders stay in the template; no real subscription URLs, secrets or
    production node names ever appear.

The template carries ``__PLACEHOLDER__`` tokens, so it is not parseable YAML
as-is: placeholders are replaced with obviously fake values first, then the
document is parsed with PyYAML and checked structurally.
"""

import importlib.util
import os
import re
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(REPO_ROOT, "config", "config.yaml.template")

# fake values for parsing only - never real subscription URLs or secrets
PLACEHOLDER_VALUES = {
    "__SUBSCRIPTION_URL__": "https://sub.example.invalid/fake.yaml",
    "__SUBSCRIPTION_INTERVAL__": "0",
    "__SECRET__": "fake-secret-for-parsing",
}

CURSOR_DOMAINS = (
    "cursor.com",
    "cursor.sh",
    "cursorapi.com",
    "cursor-auth.com",
    "cursorvm.com",
)

BUILTIN_POLICIES = {"DIRECT", "REJECT", "COMPATIBLE", "PASS"}

FILTERS = {
    "CURSOR-US": r"(?i)美国|美國|\bUS\b|USA|United States|🇺🇸",
    "CURSOR-JP": r"(?i)日本|\bJP\b|Japan|🇯🇵",
    "CODEBUDDY-HK": r"(?i)香港|\bHK\b|Hong ?Kong|🇭🇰",
    "CODEBUDDY-TW": r"(?i)台湾|台灣|\bTW\b|Taiwan|🇹🇼",
    "CODEBUDDY-SG": r"(?i)新加坡|\bSG\b|Singapore|🇸🇬",
    "CODEBUDDY-JP": r"(?i)日本|\bJP\b|Japan|🇯🇵",
}


def template_text() -> str:
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def load_template() -> dict:
    text = template_text()
    for placeholder, fake in PLACEHOLDER_VALUES.items():
        text = text.replace(placeholder, fake)
    return yaml.safe_load(text)


def load_group_refs_checker():
    path = os.path.join(REPO_ROOT, "scripts", "lib", "check_group_refs.py")
    spec = importlib.util.spec_from_file_location("check_group_refs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rule_target(rule: str) -> str:
    fields = [part.strip() for part in rule.split(",")]
    if fields[-1] == "no-resolve":
        fields.pop()
    return fields[-1]


class TemplateTextTests(unittest.TestCase):
    """Checks that run on the raw (un-substituted) template text."""

    @classmethod
    def setUpClass(cls):
        cls.text = template_text()

    def test_placeholders_are_preserved_verbatim(self):
        for placeholder in PLACEHOLDER_VALUES:
            self.assertIn(placeholder, self.text, placeholder)

    def test_provider_url_and_secret_lines_hold_only_placeholders(self):
        url_lines = re.findall(r'(?m)^\s+url:\s*"([^"]*)"\s*$', self.text)
        # the provider url is the only double-quoted url-shaped line; health
        # check / probe urls are written unquoted in this template
        self.assertIn("__SUBSCRIPTION_URL__", url_lines, "proxy-provider url must stay the placeholder")
        for value in url_lines:
            self.assertTrue(
                value == "__SUBSCRIPTION_URL__" or "://" not in value,
                "unexpected literal url in quotes: %r" % value,
            )
        secret = re.search(r'(?m)^secret:\s*"([^"]*)"\s*$', self.text)
        self.assertIsNotNone(secret)
        self.assertEqual(secret.group(1), "__SECRET__")

    def test_parsable_after_placeholder_substitution(self):
        self.assertIsInstance(load_template(), dict)


class TemplateStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_template()
        cls.groups = {g["name"]: g for g in cls.config["proxy-groups"]}
        cls.rules = [str(r).strip() for r in cls.config["rules"]]

    def rule_index(self, needle: str):
        for index, rule in enumerate(self.rules):
            if needle in rule:
                return index
        return None

    # ---- global fields -------------------------------------------------
    def test_mode_ports_and_controller(self):
        self.assertEqual(self.config.get("mode"), "rule")
        self.assertEqual(self.config.get("mixed-port"), 7890)
        self.assertEqual(self.config.get("external-controller"), "127.0.0.1:9090")

    def test_fake_substitution_reaches_provider_and_secret_slots(self):
        provider = self.config["proxy-providers"]["subscription"]
        self.assertEqual(provider["url"], PLACEHOLDER_VALUES["__SUBSCRIPTION_URL__"])
        self.assertEqual(self.config["secret"], PLACEHOLDER_VALUES["__SECRET__"])

    # ---- rules ----------------------------------------------------------
    def test_ark_is_the_first_rule(self):
        self.assertEqual(self.rules[0], "DOMAIN,ark.cn-beijing.volces.com,DIRECT")

    def test_cursor_domains_all_route_to_cursor(self):
        for domain in CURSOR_DOMAINS:
            rule = "DOMAIN-SUFFIX,%s,CURSOR" % domain
            self.assertIn(rule, self.rules, rule)

    def test_direct_rules_precede_tiktok(self):
        tiktok = self.rule_index("GEOSITE,tiktok")
        self.assertIsNotNone(tiktok, "GEOSITE,tiktok rule missing")
        for domain in ("superboss.cc", "jinritemai.com"):
            index = self.rule_index("DOMAIN-SUFFIX,%s,DIRECT" % domain)
            self.assertIsNotNone(index, "%s DIRECT rule missing" % domain)
            self.assertLess(index, tiktok, "%s must precede GEOSITE,tiktok" % domain)

    def test_codebuddy_routes_to_group(self):
        self.assertIn("DOMAIN-SUFFIX,codebuddy.ai,CODEBUDDY", self.rules)

    def test_rule_targets_are_declared_groups_or_builtins(self):
        for rule in self.rules:
            target = rule_target(rule)
            self.assertTrue(
                target in self.groups or target in BUILTIN_POLICIES,
                "rule %r targets undeclared policy %r" % (rule, target),
            )

    def test_match_rule_is_last(self):
        self.assertEqual(self.rules[-1], "MATCH,PROXY")

    # ---- CURSOR ---------------------------------------------------------
    def test_cursor_group_shape(self):
        group = self.groups["CURSOR"]
        self.assertEqual(group["type"], "fallback")
        self.assertEqual(group["proxies"], ["CURSOR-US", "CURSOR-JP"])
        self.assertEqual(group["interval"], 180)
        self.assertIs(group["lazy"], False)
        self.assertEqual(group["timeout"], 8000)

    def test_cursor_probe_urls_are_official_and_token_free(self):
        for name in ("CURSOR", "CURSOR-US", "CURSOR-JP"):
            url = str(self.groups[name].get("url") or "")
            host = re.match(r"https://([^/?#]+)(/[^\?]*)?$", url)
            self.assertIsNotNone(host, "%s probe url must be a plain official path: %r" % (name, url))
            self.assertTrue(
                host.group(1) == "cursor.com" or host.group(1).endswith(".cursor.com"),
                "%s probe url must use the official domain: %r" % (name, url),
            )

    def test_cursor_subgroups_pull_us_then_jp_from_the_subscription(self):
        for name in ("CURSOR-US", "CURSOR-JP"):
            group = self.groups[name]
            self.assertEqual(group["type"], "fallback", name)
            self.assertEqual(group.get("use"), ["subscription"], name)
            self.assertNotIn("proxies", group, "%s must not list literal members" % name)
            self.assertEqual(str(group.get("filter")), FILTERS[name], name)

    # ---- CODEBUDDY ------------------------------------------------------
    def test_codebuddy_group_shape(self):
        group = self.groups["CODEBUDDY"]
        self.assertEqual(group["type"], "fallback")
        self.assertEqual(group["interval"], 60)
        self.assertEqual(
            group["proxies"],
            ["DIRECT", "CODEBUDDY-HK", "CODEBUDDY-TW", "CODEBUDDY-SG", "CODEBUDDY-JP"],
        )
        self.assertEqual(group["proxies"][0], "DIRECT", "DIRECT must stay first (fallback)")

    def test_codebuddy_probe_urls(self):
        for name in ("CODEBUDDY", "CODEBUDDY-HK", "CODEBUDDY-TW", "CODEBUDDY-SG", "CODEBUDDY-JP"):
            url = str(self.groups[name].get("url") or "")
            self.assertRegex(url, r"^https://([a-z0-9-]+\.)*codebuddy\.ai(/[^\?]*)?$", url)

    def test_region_subgroups_use_filtered_providers(self):
        for name, pattern in FILTERS.items():
            group = self.groups[name]
            self.assertEqual(group["type"], "fallback", name)
            self.assertEqual(group.get("use"), ["subscription"], name)
            self.assertNotIn("proxies", group, "%s must get members only from the provider" % name)
            actual = str(group.get("filter") or "")
            self.assertEqual(actual, pattern, "%s filter" % name)
            re.compile(actual)  # the filter must be a valid regex

    # ---- no literal node names ------------------------------------------
    def test_no_literal_node_names_anywhere(self):
        # every listed group member is another group or a built-in policy;
        # concrete node names are only ever reached through use + filter
        checker = load_group_refs_checker()
        problems = checker.find_missing_group_refs(self.config, config_dir=None)
        self.assertEqual(problems, [], "unresolvable group members: %s" % problems)
        for group in self.groups.values():
            for member in group.get("proxies") or []:
                self.assertTrue(
                    member in self.groups or member in BUILTIN_POLICIES,
                    "%r looks like a literal node name" % member,
                )


if __name__ == "__main__":
    unittest.main()
