"""Render the shared production template for macOS TUN / mixed mode.

The single policy source is ``config/config.yaml.template`` (shared with
Windows).  This module only:

* injects the host's secrets (subscription URL, controller secret);
* switches the *technical* fields between TUN and mixed-port mode;
* validates the result against the production rules, the policy filters and
  ``mihomo -t`` before anything replaces the live config.

Rules and group order are never rewritten here - that is what keeps Windows and
macOS on one policy.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import re
from typing import Any

from mpm_common import policy
from mpm_common.atomicio import write_atomic
from mpm_common.errors import FailClosed
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST

MIXED_PORT = 7890
CONTROLLER_PORT = 9090
DNS_PORT = 1053
PROVIDER_NAME = "subscription"

MODES = ("tun", "mixed")

#: Files mihomo loads from its ``-d`` directory when ``geodata-mode`` is true.
GEODATA_FILES = ("GeoIP.dat", "GeoSite.dat")

ARK_RULE = "DOMAIN,ark.cn-beijing.volces.com,DIRECT"
SUPERBOSS_RULE = "DOMAIN-SUFFIX,superboss.cc,DIRECT"
JINRITEMAI_RULE = "DOMAIN-SUFFIX,jinritemai.com,DIRECT"
TIKTOK_RULE = "GEOSITE,tiktok,VIETNAM"
CODEBUDDY_RULE = "DOMAIN-SUFFIX,codebuddy.ai,CODEBUDDY"
CURSOR_RULES = tuple(
    f"DOMAIN-SUFFIX,{domain},CURSOR"
    for domain in ("cursor.com", "cursor.sh", "cursorapi.com", "cursor-auth.com", "cursorvm.com")
)

# ---- macOS-only policy overrides ------------------------------------------
#
# The shared template is Windows' single source of truth and is never edited
# here.  macOS re-renders the *technical* groups from the same template because
# the 2026-11-14 outage was caused by macOS-only defects:
#
# * ``US-FAST`` was a url-test with a loose filter (``美|us|usa``) that matched
#   Russia/Australia and never failed over away from a dead node;
# * the provider interval was written as minutes into a seconds field;
# * ``PROXY`` was a ``select`` that included every raw provider node and
#   ``store-selected`` remembered a dead Taiwanese node across restarts;
# * fake-ip used Nextin's 198.18.0.1/16.

#: Strict region filters - identical to the CURSOR-US / CURSOR-JP constants in
#: ``mpm_common.policy`` so there is exactly one definition per region.
US_FILTER = policy.CURSOR_REGIONS["US"][1]
JP_FILTER = policy.CURSOR_REGIONS["JP"][1]
SG_FILTER = policy.CODEBUDDY_REGIONS["SG"][1]
REGION_FILTERS = {"US": US_FILTER, "SG": SG_FILTER, "JP": JP_FILTER}

US_FAST_URL = "https://www.gstatic.com/generate_204"
GPT_URL = "https://chatgpt.com/cdn-cgi/trace"

#: mihomo's provider ``interval`` is in **seconds**; the operator input is in
#: minutes and one hour is the floor (a 60-second refresh hammered the
#: subscription endpoint and could leave the provider empty).
PROVIDER_MIN_INTERVAL = 3600
PROVIDER_HEALTHCHECK_INTERVAL = 300

#: mihomo's own fake-ip range.  198.18.0.0/16 belongs to Nextin's
#: NetworkExtension (utun 198.18.0.1); overlapping it split DNS and routes.
FAKE_IP_RANGE = "198.19.0.1/16"

GPT_RULES = (
    "GEOSITE,openai,GPT",
    "DOMAIN-SUFFIX,chatgpt.com,GPT",
    "DOMAIN-SUFFIX,openai.com,GPT",
)

PROXY_MEMBERS = ["AUTO-FOREIGN", "US-FAST", "DIRECT"]
GLOBAL_MEMBERS = ["PROXY", "AUTO-FOREIGN", "CODEBUDDY", "GPT", "US-FAST", "VIETNAM", "DIRECT"]
GPT_MEMBERS = ["GPT-US", "GPT-SG", "GPT-JP"]

_PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")


class RenderError(FailClosed):
    """The rendered config was rejected; the live config was not touched."""


def _yaml():
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by install on a bare host
        raise RenderError(
            "PyYAML is required to render the macOS config (pip3 install pyyaml)"
        ) from exc
    return yaml


def load_template(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.read()
    except OSError as exc:
        raise RenderError(f"cannot read config template: {path}") from exc


def render(
    template_text: str,
    *,
    secret: str,
    subscription_url: str,
    interval_minutes: int = 60,
    mode: str = "tun",
    provider_path: str = "./providers/subscription.yaml",
) -> str:
    """Return the full ``config.yaml`` text for ``mode``."""
    if mode not in MODES:
        raise RenderError(f"unknown mode '{mode}' (expected tun or mixed)")
    if not secret:
        raise RenderError("controller secret is empty; refusing to render an unauthenticated config")
    if not subscription_url:
        raise RenderError("subscription URL is empty")

    yaml = _yaml()
    document = yaml.safe_load(template_text)
    if not isinstance(document, dict):
        raise RenderError("config template root is not a mapping")

    document["mode"] = "rule"
    document["mixed-port"] = MIXED_PORT
    document["external-controller"] = f"127.0.0.1:{CONTROLLER_PORT}"
    document["secret"] = secret
    # ``mihomo -t`` runs during install and must never reach the network: with
    # auto-update on, a missing geo database is fetched from GitHub and the
    # install stalls until it times out.  Geo data is installed locally instead
    # (see :func:`ensure_geodata`).
    document["geo-auto-update"] = False

    providers = document.get("proxy-providers")
    if not isinstance(providers, dict) or not isinstance(providers.get(PROVIDER_NAME), dict):
        raise RenderError(f"config template is missing the '{PROVIDER_NAME}' proxy provider")
    provider = providers[PROVIDER_NAME]
    provider["url"] = subscription_url
    provider["path"] = provider_path
    # The operator's ``SUBSCRIPTION_INTERVAL`` is in minutes; mihomo reads
    # seconds.  Writing minutes directly made mihomo refresh every 60 seconds.
    provider["interval"] = provider_interval_seconds(interval_minutes)
    health_check = provider.setdefault("health-check", {})
    if not isinstance(health_check, dict):
        health_check = {}
        provider["health-check"] = health_check
    health_check.update(
        {
            "enable": True,
            "url": US_FAST_URL,
            "interval": PROVIDER_HEALTHCHECK_INTERVAL,
            "timeout": 5000,
            "lazy": True,
        }
    )

    dns = document.setdefault("dns", {})
    profile = document.setdefault("profile", {})
    # A remembered selection survived restarts and kept PROXY on a dead node.
    profile["store-selected"] = False

    if mode == "tun":
        document["tun"] = {
            "enable": True,
            "stack": "gvisor",
            "auto-route": True,
            "auto-detect-interface": True,
            "dns-hijack": ["any:53", "tcp://any:53"],
            "sniffing": True,
        }
        dns["enhanced-mode"] = "fake-ip"
        dns["fake-ip-range"] = FAKE_IP_RANGE
        profile["store-fake-ip"] = True
    else:
        document["tun"] = {
            "enable": False,
            "stack": "gvisor",
            "auto-route": False,
            "auto-detect-interface": False,
            "dns-hijack": [],
        }
        # Without TUN nothing owns the fake-ip range: redir-host keeps the
        # system resolver authoritative and avoids handing out unroutable
        # 198.18.x.x answers.
        dns["enhanced-mode"] = "redir-host"
        for key in ("fake-ip-range", "fake-ip-filter", "fake-ip-range-v6"):
            dns.pop(key, None)
        profile["store-fake-ip"] = False

    _apply_proxy_groups(document)
    _apply_rules(document)

    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120)
    audit(text, mode=mode, secret=secret)
    return text


def provider_interval_seconds(interval_minutes: int) -> int:
    """Minutes (operator input) -> seconds (mihomo), never below one hour.

    ``0`` is rejected instead of being silently raised to the 3600 s floor: a
    zero interval is a configuration mistake (the field was once written in the
    wrong unit) and silently accepting it hides exactly that bug.
    """
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError) as exc:
        raise RenderError("SUBSCRIPTION_INTERVAL must be an integer number of minutes") from exc
    if minutes <= 0:
        raise RenderError("SUBSCRIPTION_INTERVAL must be at least 1 minute")
    return max(PROVIDER_MIN_INTERVAL, minutes * 60)


def _replace_group(groups: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Replace a group in place (position is semantic), or append it."""
    for index, group in enumerate(groups):
        if isinstance(group, dict) and group.get("name") == payload["name"]:
            groups[index] = payload
            return
    groups.append(payload)


def _region_group(name: str, region: str, *, url: str, interval: int, timeout: int) -> dict[str, Any]:
    return {
        "name": name,
        "type": "fallback",
        "use": [PROVIDER_NAME],
        "filter": REGION_FILTERS[region],
        "url": url,
        "expected-status": 200,
        "interval": interval,
        "timeout": timeout,
        "lazy": False,
        "max-failed-times": 3,
    }


def _apply_proxy_groups(document: dict[str, Any]) -> None:
    """Apply the macOS group layout on top of the shared template.

    * ``PROXY`` never lists raw provider nodes (``use`` removed) and cannot
      remember an old selection;
    * ``US-FAST`` is a strict, failing-over fallback instead of a url-test with
      a filter that matched Russia/Australia;
    * ``GPT`` is US -> SG -> JP, stable and slow to switch;
    * ``GLOBAL`` exposes the same GPT entry.
    """
    groups = document.get("proxy-groups")
    if not isinstance(groups, list):
        raise RenderError("config template is missing proxy-groups")

    _replace_group(
        groups,
        {
            "name": "PROXY",
            "type": "select",
            "proxies": list(PROXY_MEMBERS),
        },
    )
    _replace_group(
        groups,
        {
            "name": "US-FAST",
            "type": "fallback",
            "use": [PROVIDER_NAME],
            "filter": US_FILTER,
            "url": US_FAST_URL,
            "interval": 180,
            "timeout": 5000,
            "lazy": False,
            "max-failed-times": 3,
        },
    )

    gpt_groups = [
        _region_group("GPT-US", "US", url=GPT_URL, interval=300, timeout=8000),
        _region_group("GPT-SG", "SG", url=GPT_URL, interval=300, timeout=8000),
        _region_group("GPT-JP", "JP", url=GPT_URL, interval=300, timeout=8000),
    ]
    gpt = {
        "name": "GPT",
        "type": "fallback",
        "proxies": list(GPT_MEMBERS),
        "url": GPT_URL,
        "expected-status": 200,
        "interval": 300,
        "timeout": 8000,
        "lazy": False,
        "max-failed-times": 3,
    }

    # Keep GPT next to the other AI groups, immediately before US-FAST.
    for payload in gpt_groups:
        _replace_group(groups, payload)
    _replace_group(groups, gpt)

    desired = [*GPT_MEMBERS, "GPT"]
    moved = sorted(
        (group for group in groups if isinstance(group, dict) and group.get("name") in desired),
        key=lambda group: desired.index(group["name"]),
    )
    remaining = [
        group
        for group in groups
        if not (isinstance(group, dict) and group.get("name") in desired)
    ]
    order = [group.get("name") for group in remaining if isinstance(group, dict)]
    insert_at = order.index("US-FAST") if "US-FAST" in order else len(remaining)
    groups[:] = remaining[:insert_at] + moved + remaining[insert_at:]

    _replace_group(
        groups,
        {
            "name": "GLOBAL",
            "type": "select",
            "proxies": list(GLOBAL_MEMBERS),
        },
    )


def _apply_rules(document: dict[str, Any]) -> None:
    """Route OpenAI/ChatGPT through GPT before any US-FAST/PROXY match."""
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise RenderError("config template is missing rules")
    text_rules = [str(rule) for rule in rules]

    # Drop the template's OpenAI/ChatGPT -> US-FAST rules (and any stale copy),
    # remembering where the OpenAI block used to be.
    anchor: int | None = None
    cleaned: list[str] = []
    for rule in text_rules:
        if rule in GPT_RULES:
            continue
        if rule == "GEOSITE,openai,US-FAST":
            if anchor is None:
                anchor = len(cleaned)
            continue
        if rule == "DOMAIN-SUFFIX,chatgpt.com,US-FAST":
            continue
        cleaned.append(rule)
    if anchor is None:
        anchor = next(
            (i for i, rule in enumerate(cleaned) if rule.startswith("GEOSITE,openai,")),
            0,
        )
    cleaned[anchor:anchor] = list(GPT_RULES)
    document["rules"] = cleaned


def _groups(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for group in document.get("proxy-groups") or []:
        if isinstance(group, dict) and isinstance(group.get("name"), str):
            groups[group["name"]] = group
    return groups


def _rules(document: dict[str, Any]) -> list[str]:
    return [str(rule) for rule in document.get("rules") or []]


def _in_cidr(value: str, cidr: str) -> bool:
    """Whether ``value`` (address or range) overlaps ``cidr``."""
    try:
        network = ipaddress.ip_network(value, strict=False)
        other = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return network.overlaps(other)


def audit(text: str, *, mode: str, secret: str | None = None) -> None:
    """Fail closed on any policy, security or mode regression."""
    leftover = sorted(set(_PLACEHOLDER.findall(text)))
    if leftover:
        raise RenderError("unresolved placeholders in rendered config: " + ", ".join(leftover))

    yaml = _yaml()
    try:
        document = yaml.safe_load(text)
    except Exception as exc:  # pragma: no cover - render just produced it
        raise RenderError(f"rendered config is not valid YAML: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise RenderError("rendered config root is not a mapping")

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise RenderError(f"config audit failed: {message}")

    require(document.get("mode") == "rule", "mode must be 'rule'")
    require(document.get("mixed-port") == MIXED_PORT, f"mixed-port must be {MIXED_PORT}")
    require(
        document.get("external-controller") == f"127.0.0.1:{CONTROLLER_PORT}",
        f"external-controller must be 127.0.0.1:{CONTROLLER_PORT}",
    )
    require(bool(document.get("secret")), "controller secret is empty")
    if secret is not None:
        require(document.get("secret") == secret, "rendered secret does not match the input")
    require(not document.get("proxies"), "the rendered config must not inline proxy nodes")
    require(
        document.get("geo-auto-update") is False,
        "geo-auto-update must be false (mihomo -t must never download geo data)",
    )

    tun = document.get("tun")
    require(isinstance(tun, dict), "tun section is missing")
    dns = document.get("dns") or {}
    profile = document.get("profile") or {}
    if mode == "tun":
        require(tun.get("enable") is True, "TUN mode requires tun.enable: true")
        require(tun.get("auto-route") is True, "TUN mode requires auto-route: true")
        require(tun.get("auto-detect-interface") is True, "TUN mode requires auto-detect-interface")
        require(tun.get("stack") == "gvisor", "TUN mode requires stack: gvisor")
        require(bool(tun.get("dns-hijack")), "TUN mode requires dns-hijack")
        require(dns.get("enhanced-mode") == "fake-ip", "TUN mode requires fake-ip DNS")
    else:
        require(tun.get("enable") is False, "mixed mode must disable TUN")
        require(tun.get("auto-route") is False, "mixed mode must not auto-route")
        require(not tun.get("dns-hijack"), "mixed mode must not hijack DNS")
        require(
            dns.get("enhanced-mode") == "redir-host",
            "mixed mode requires dns.enhanced-mode: redir-host (fake-ip has no owner)",
        )
        for key in ("fake-ip-range", "fake-ip-filter", "fake-ip-range-v6"):
            require(key not in dns, f"mixed mode must not configure {key}")
        require(profile.get("store-fake-ip") is False, "mixed mode must disable store-fake-ip")

    rules = _rules(document)
    require(bool(rules), "rules are missing")
    require(rules[0] == ARK_RULE, f"first rule must be {ARK_RULE}")
    for earlier, later in ((SUPERBOSS_RULE, TIKTOK_RULE), (JINRITEMAI_RULE, TIKTOK_RULE)):
        require(earlier in rules, f"missing rule {earlier}")
        require(later in rules, f"missing rule {later}")
        require(rules.index(earlier) < rules.index(later), f"{earlier} must precede {later}")
    for rule in CURSOR_RULES:
        require(rule in rules, f"missing rule {rule}")
    require(CODEBUDDY_RULE in rules, f"missing rule {CODEBUDDY_RULE}")

    provider = (document.get("proxy-providers") or {}).get(PROVIDER_NAME)
    require(isinstance(provider, dict), f"missing proxy provider '{PROVIDER_NAME}'")
    require(bool(provider.get("path")), "provider path must be kept (offline cache fallback)")
    interval = provider.get("interval")
    require(
        isinstance(interval, int) and interval >= PROVIDER_MIN_INTERVAL,
        f"provider interval must be at least {PROVIDER_MIN_INTERVAL} seconds",
    )
    health_check = provider.get("health-check") or {}
    require(health_check.get("enable") is True, "provider health-check must be enabled")
    require(
        health_check.get("interval") == PROVIDER_HEALTHCHECK_INTERVAL,
        f"provider health-check interval must be {PROVIDER_HEALTHCHECK_INTERVAL}",
    )
    require(health_check.get("lazy") is True, "provider health-check must be lazy")

    require(
        profile.get("store-selected") is False,
        "profile.store-selected must be false (no remembered dead node)",
    )

    groups = _groups(document)
    for name in ("CURSOR", "CURSOR-US", "CURSOR-JP", "CODEBUDDY"):
        require(name in groups, f"missing proxy group {name}")
    require(
        groups["CURSOR"].get("type") == "fallback",
        "CURSOR must be a fallback group",
    )
    require(
        groups["CURSOR"].get("proxies") == ["CURSOR-US", "CURSOR-JP"],
        "CURSOR must list CURSOR-US then CURSOR-JP",
    )
    require(groups["CURSOR"].get("interval") == 180, "CURSOR interval must be 180")
    require(groups["CURSOR"].get("lazy") is False, "CURSOR must not be lazy")
    require(groups["CURSOR"].get("timeout") == 8000, "CURSOR timeout must be 8000")
    require(
        groups["CODEBUDDY"].get("proxies", [None])[0] == "DIRECT",
        "CODEBUDDY must list DIRECT first",
    )
    for group_name, pattern in policy.expected_filters().items():
        require(group_name in groups, f"missing region subgroup {group_name}")
        require(
            groups[group_name].get("filter") == pattern,
            f"{group_name} filter drifted from the network-ops ruling",
        )
        require(
            groups[group_name].get("type") == "fallback",
            f"{group_name} must be a fallback group",
        )
        require(
            groups[group_name].get("use") == [PROVIDER_NAME],
            f"{group_name} must draw from the '{PROVIDER_NAME}' provider",
        )

    # ---- macOS-only group invariants ----------------------------------
    proxy = groups.get("PROXY")
    require(isinstance(proxy, dict), "missing proxy group PROXY")
    require(proxy.get("type") == "select", "PROXY must stay a select group")
    require("use" not in proxy, "PROXY must not list raw provider nodes (no use)")
    require(
        proxy.get("proxies") == PROXY_MEMBERS,
        f"PROXY must list {PROXY_MEMBERS}",
    )

    us_fast = groups.get("US-FAST")
    require(isinstance(us_fast, dict), "missing proxy group US-FAST")
    require(us_fast.get("type") == "fallback", "US-FAST must be a fallback group")
    require(us_fast.get("use") == [PROVIDER_NAME], "US-FAST must use the provider")
    require(us_fast.get("filter") == US_FILTER, "US-FAST filter must be the strict US filter")
    require(us_fast.get("url") == US_FAST_URL, f"US-FAST url must be {US_FAST_URL}")
    require(us_fast.get("interval") == 180, "US-FAST interval must be 180")
    require(us_fast.get("timeout") == 5000, "US-FAST timeout must be 5000")
    require(us_fast.get("lazy") is False, "US-FAST must not be lazy")
    require(us_fast.get("max-failed-times") == 3, "US-FAST max-failed-times must be 3")

    for name, region in (("GPT-US", "US"), ("GPT-SG", "SG"), ("GPT-JP", "JP")):
        group = groups.get(name)
        require(isinstance(group, dict), f"missing proxy group {name}")
        require(group.get("type") == "fallback", f"{name} must be a fallback group")
        require(group.get("use") == [PROVIDER_NAME], f"{name} must use the provider")
        require(
            group.get("filter") == REGION_FILTERS[region],
            f"{name} filter must be the strict {region} filter",
        )
        require(group.get("url") == GPT_URL, f"{name} url must be {GPT_URL}")
        require(group.get("expected-status") == 200, f"{name} expected-status must be 200")
        require(group.get("interval") == 300, f"{name} interval must be 300")
        require(group.get("timeout") == 8000, f"{name} timeout must be 8000")
        require(group.get("lazy") is False, f"{name} must not be lazy")
        require(group.get("max-failed-times") == 3, f"{name} max-failed-times must be 3")

    gpt = groups.get("GPT")
    require(isinstance(gpt, dict), "missing proxy group GPT")
    require(gpt.get("type") == "fallback", "GPT must be a fallback group")
    require(gpt.get("proxies") == GPT_MEMBERS, f"GPT must list {GPT_MEMBERS}")
    require(gpt.get("url") == GPT_URL, f"GPT url must be {GPT_URL}")
    require(gpt.get("interval") == 300, "GPT interval must be 300")
    require(gpt.get("max-failed-times") == 3, "GPT max-failed-times must be 3")

    order = [
        group.get("name")
        for group in document.get("proxy-groups") or []
        if isinstance(group, dict)
    ]
    positions = [order.index(name) for name in (*GPT_MEMBERS, "GPT") if name in order]
    require(
        len(positions) == 4 and positions == sorted(positions),
        "GPT subgroups must appear as GPT-US, GPT-SG, GPT-JP, GPT",
    )

    global_group = groups.get("GLOBAL")
    require(isinstance(global_group, dict), "missing proxy group GLOBAL")
    require("GPT" in (global_group.get("proxies") or []), "GLOBAL must include GPT")

    for rule in GPT_RULES:
        require(rule in rules, f"missing rule {rule}")
    gpt_rule_index = min(rules.index(rule) for rule in GPT_RULES)
    early = [
        index
        for index, rule in enumerate(rules)
        if rule.endswith(",US-FAST") or rule.endswith(",PROXY")
    ]
    require(
        not early or gpt_rule_index < min(early),
        "GPT rules must precede every US-FAST/PROXY rule",
    )

    if mode == "tun":
        require(
            dns.get("fake-ip-range") == FAKE_IP_RANGE,
            f"TUN fake-ip-range must be {FAKE_IP_RANGE} (Nextin owns 198.18.0.0/16)",
        )
        require(
            not _in_cidr(str(dns.get("fake-ip-range", "")), "198.18.0.0/16"),
            "fake-ip-range must not overlap Nextin's 198.18.0.0/16",
        )


def provider_node_names(text: str) -> list[str]:
    """Node names from a converted provider file (``proxies[].name``)."""
    yaml = _yaml()
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        return []
    names: list[str] = []
    for item in document.get("proxies") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def load_convert_module(path: str):
    """Import ``convert_sub.py`` by path (never copied into this package)."""
    spec = importlib.util.spec_from_file_location("mpm_convert_sub", path)
    if spec is None or spec.loader is None:
        raise RenderError(f"cannot load subscription converter: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RenderError(
            f"subscription converter failed to load: {type(exc).__name__}"
        ) from exc
    return module


def convert_provider(
    url: str,
    destination: str,
    *,
    module,
    timeout: int = 30,
    user_agent: str | None = None,
) -> list[str]:
    """Fetch + convert a subscription and write the provider atomically.

    The conversion itself is ``scripts/convert_sub.py``; nothing is
    re-implemented here.  Returned names feed the policy check.
    """
    agent = user_agent or getattr(module, "DEFAULT_USER_AGENT", "ClashMeta/1.18.0")
    try:
        content = module.fetch_subscription(url, timeout, agent)
        proxies, _stats = module.convert_content(content)
        merged = module.merge_proxies(proxies)
    except Exception as exc:
        detail = HOST.text(str(exc))[:200]
        raise RenderError(f"subscription conversion failed: {detail}") from exc
    if not merged:
        raise RenderError("subscription produced no usable nodes")
    text = module.to_yaml(merged)
    write_atomic(destination, text, mode=0o600)
    return [str(proxy["name"]) for proxy in merged if proxy.get("name")]


def required_geodata(text: str) -> tuple[str, ...]:
    """Geo database files mihomo loads from its ``-d`` directory.

    ``geodata-mode: true`` (the production template) makes mihomo read
    ``GeoIP.dat`` and ``GeoSite.dat`` from its home directory (``-d``).  When
    either is missing mihomo downloads it, which is exactly what a static
    validation must not do on a host with no outbound access.
    """
    yaml = _yaml()
    document = yaml.safe_load(text)
    if isinstance(document, dict) and document.get("geodata-mode"):
        return GEODATA_FILES
    return ()


def ensure_geodata(text: str, home_dir: str) -> None:
    """Fail closed before ``mihomo -t`` instead of letting mihomo download."""
    missing = [
        name
        for name in required_geodata(text)
        if not os.path.isfile(os.path.join(home_dir, name))
    ]
    if not missing:
        return
    raise RenderError(
        "geo database missing from the mihomo home directory "
        f"({home_dir}): {', '.join(missing)}; put GeoIP.dat and GeoSite.dat next to "
        "the binary given to MPM_MIHOMO_BINARY (the installer copies them into the "
        "mihomo home, i.e. the -d directory) - mihomo -t must not download them"
    )


def validate(binary: str, config_path: str, home_dir: str, executor: Executor) -> None:
    """Static ``mihomo -t`` validation; a failure never replaces the live config."""
    result = executor.run([binary, "-t", "-f", config_path, "-d", home_dir])
    if not result.ok:
        raise RenderError(
            f"mihomo -t rejected the rendered config (exit={result.returncode})"
        )


__all__ = [
    "ARK_RULE",
    "CODEBUDDY_RULE",
    "CONTROLLER_PORT",
    "CURSOR_RULES",
    "DNS_PORT",
    "FAKE_IP_RANGE",
    "GEODATA_FILES",
    "GLOBAL_MEMBERS",
    "GPT_MEMBERS",
    "GPT_RULES",
    "GPT_URL",
    "JINRITEMAI_RULE",
    "MIXED_PORT",
    "MODES",
    "PROVIDER_HEALTHCHECK_INTERVAL",
    "PROVIDER_MIN_INTERVAL",
    "PROVIDER_NAME",
    "PROXY_MEMBERS",
    "REGION_FILTERS",
    "RenderError",
    "SUPERBOSS_RULE",
    "TIKTOK_RULE",
    "US_FAST_URL",
    "US_FILTER",
    "audit",
    "convert_provider",
    "ensure_geodata",
    "load_convert_module",
    "load_template",
    "provider_interval_seconds",
    "provider_node_names",
    "render",
    "required_geodata",
    "validate",
]
