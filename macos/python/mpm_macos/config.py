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
    providers[PROVIDER_NAME]["url"] = subscription_url
    providers[PROVIDER_NAME]["path"] = provider_path
    providers[PROVIDER_NAME]["interval"] = int(interval_minutes)

    dns = document.setdefault("dns", {})
    profile = document.setdefault("profile", {})

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
        dns.setdefault("fake-ip-range", "198.18.0.1/16")
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

    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120)
    audit(text, mode=mode, secret=secret)
    return text


def _groups(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for group in document.get("proxy-groups") or []:
        if isinstance(group, dict) and isinstance(group.get("name"), str):
            groups[group["name"]] = group
    return groups


def _rules(document: dict[str, Any]) -> list[str]:
    return [str(rule) for rule in document.get("rules") or []]


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
    ``GeoIP.dat`` and ``GeoSite.dat`` from the config directory.  When either is
    missing mihomo downloads it, which is exactly what a static validation must
    not do on a host with no outbound access.
    """
    yaml = _yaml()
    document = yaml.safe_load(text)
    if isinstance(document, dict) and document.get("geodata-mode"):
        return GEODATA_FILES
    return ()


def ensure_geodata(text: str, state_dir: str) -> None:
    """Fail closed before ``mihomo -t`` instead of letting mihomo download."""
    missing = [
        name
        for name in required_geodata(text)
        if not os.path.isfile(os.path.join(state_dir, name))
    ]
    if not missing:
        return
    raise RenderError(
        "geo database missing from the mihomo state directory "
        f"({state_dir}): {', '.join(missing)}; put GeoIP.dat and GeoSite.dat next to "
        "the binary given to MPM_MIHOMO_BINARY (the installer copies them into the "
        "state directory) - mihomo -t must not download them"
    )


def validate(binary: str, config_path: str, state_dir: str, executor: Executor) -> None:
    """Static ``mihomo -t`` validation; a failure never replaces the live config."""
    result = executor.run([binary, "-t", "-f", config_path, "-d", state_dir])
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
    "GEODATA_FILES",
    "JINRITEMAI_RULE",
    "MIXED_PORT",
    "MODES",
    "PROVIDER_NAME",
    "RenderError",
    "SUPERBOSS_RULE",
    "TIKTOK_RULE",
    "audit",
    "convert_provider",
    "ensure_geodata",
    "load_convert_module",
    "load_template",
    "provider_node_names",
    "render",
    "required_geodata",
    "validate",
]
