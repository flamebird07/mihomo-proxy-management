"""Configuration rendering (section 6) - template-free deterministic YAML.

The renderer builds text rather than parsing YAML so that the runtime has no
third-party dependency (Ubuntu/Debian base images ship python3 without
PyYAML).  Quoting is explicit, so a URL containing ``$``, ``&``, ``#`` or
unicode cannot break the document.

Structural guarantees produced here and re-checked by :func:`audit`:

* ``subscription-foreign`` and ``subscription-cn`` are separate providers
* foreign-only groups (``PROXY``, ``AUTO-FOREIGN``, ``US-FAST``, ``VIETNAM``)
  reference the foreign provider only
* CN-only groups (``AUTO-CN``, ``CN-EXIT``) reference the CN provider only
* CN is never inferred from node names
* no CN provider  -> no CN rules rendered at all (CN reported *disabled*)
* CN provider present -> CN groups never list ``DIRECT`` or a foreign group as
  an alternative, so a broken CN provider cannot silently fall back (D8b=2)
* ``allow-lan: false``, external controller bound to ``127.0.0.1`` only
* TUN mode: ``tun.enable: true`` + ``auto-route: true`` + fake-ip + hijack
* degraded mode: ``tun.enable: false``, no hijack, **no fake-ip at all**
  (``redir-host``, no fake-ip range/filter, ``store-fake-ip: false``) so the
  system resolver stays the only resolver
* CN provider configured but has no live node -> CN rules render an explicit
  ``REJECT`` refuse instead of selecting a dead node
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import FailClosed
from .preflight import Preflight
from .sanitize import describe_url
from .secrets_io import SecretInputs

SCHEMA_VERSION = 1

FOREIGN_PROVIDER = "subscription-foreign"
CN_PROVIDER = "subscription-cn"

PROXY_PORT = 7890
MIXED_PORT = 7890
CONTROLLER_PORT = 9090
DNS_PORT = 1053

# connectivity probe (not an egress-IP service); mihomo needs a reachable target
FOREIGN_HEALTHCHECK_URL = "https://www.gstatic.com/generate_204"

PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")

# keys a user override may never touch (security boundaries, D3/D4/D5/D6)
DENIED_OVERRIDE_KEYS = {
    "allow-lan",
    "allow_lan",
    "bind-address",
    "external-controller",
    "external_controller",
    "secret",
    "tun",
    "dns-hijack",
    "url",
    "subscription-url",
    "mixed-port",
    "port",
}

ALLOWED_OVERRIDE_KEYS = {
    "log_level",
    "interval_minutes",
    "fake_ip_range",
    "health_check_interval",
    "unified_delay",
    "sniffing",
    "ipv6",
    "tcp_concurrent",
    "geo_auto_update",
    "geo_update_interval",
}

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def yaml_quote(value: str) -> str:
    """Double-quote with escapes - safe for any URL / secret content."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _bool(value: str, name: str) -> str:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return "true"
    if lowered in _FALSE:
        return "false"
    raise FailClosed(f"override {name} must be a boolean")


@dataclass
class Overrides:
    log_level: str = "info"
    interval_minutes: int = 60
    fake_ip_range: str = "198.18.0.1/16"
    health_check_interval: int = 300
    unified_delay: str = "true"
    sniffing: str = "true"
    ipv6: str = "false"
    tcp_concurrent: str = "true"
    geo_auto_update: str = "true"
    geo_update_interval: int = 24
    extra_rules: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "log_level": self.log_level,
            "interval_minutes": self.interval_minutes,
            "fake_ip_range": self.fake_ip_range,
            "health_check_interval": self.health_check_interval,
            "extra_rules": len(self.extra_rules),
            "sources": list(self.sources),
        }


def _parse_override_file(path: str, text: str, state: Overrides) -> None:
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("rule +=") or line.startswith("rules +="):
            rule = line.split("+=", 1)[1].strip()
            if not rule or "," not in rule:
                raise FailClosed(
                    f"{path}:{number} rule override needs the form 'rule += TYPE,target,policy'"
                )
            state.extra_rules.append(rule)
            continue
        if "=" not in line:
            raise FailClosed(f"{path}:{number} not a KEY=VALUE or 'rule +=' line")
        key, value = (part.strip() for part in line.split("=", 1))
        lowered = key.lower()
        if lowered in DENIED_OVERRIDE_KEYS:
            raise FailClosed(
                f"{path}:{number} override of '{key}' is refused; it would weaken "
                f"the loopback/LAN/TUN security boundaries"
            )
        if lowered not in ALLOWED_OVERRIDE_KEYS:
            raise FailClosed(f"{path}:{number} unknown override key '{key}'")
        if lowered == "log_level":
            if value not in {"silent", "error", "warning", "info", "debug"}:
                raise FailClosed(f"{path}:{number} invalid log_level")
            state.log_level = value
        elif lowered == "interval_minutes":
            state.interval_minutes = _int(value, f"{path}:{number} interval_minutes")
        elif lowered == "fake_ip_range":
            if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}", value):
                raise FailClosed(f"{path}:{number} fake_ip_range must be CIDR")
            state.fake_ip_range = value
        elif lowered == "health_check_interval":
            state.health_check_interval = _int(
                value, f"{path}:{number} health_check_interval"
            )
        elif lowered == "geo_update_interval":
            state.geo_update_interval = _int(value, f"{path}:{number} geo_update_interval")
        elif lowered in {"unified_delay", "sniffing", "ipv6", "tcp_concurrent", "geo_auto_update"}:
            setattr(state, lowered, _bool(value, f"{path}:{number} {key}"))
        else:  # pragma: no cover - guarded by ALLOWED_OVERRIDE_KEYS
            raise FailClosed(f"{path}:{number} unhandled override '{key}'")
    state.sources.append(path)


def _int(value: str, label: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise FailClosed(f"{label} must be an integer") from exc
    if number < 0:
        raise FailClosed(f"{label} must not be negative")
    return number


def load_overrides(paths: list[str]) -> Overrides:
    """Load ``overrides.d/*.conf`` in sorted order; refuse secret-looking input."""
    state = Overrides()
    for path in sorted(paths):
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        if "\ufeff" in text:
            text = text.lstrip("\ufeff")
        if re.search(r"(?i)\bhttps?://\S*[?&](?:token|key|password|secret)=", text):
            raise FailClosed(
                f"{path} looks like it contains a credentialed URL; overrides.d is "
                f"for non-secret overrides only"
            )
        _parse_override_file(path, text, state)
    return state


def render(
    inputs: SecretInputs,
    preflight: Preflight,
    overrides: Overrides,
    *,
    cn_health: str = "OK",
) -> str:
    """Produce the full ``config.yaml`` text.

    ``cn_health`` is the *persisted* CN state (``OK``/``DEGRADED``/``DISABLED``):
    a DEGRADED CN provider renders an explicit refuse instead of CN-EXIT, so CN
    traffic is never sent to a dead node and never falls back (D8b=2).
    """
    mode = preflight.mode
    tun = mode == "tun"
    cn_enabled = bool(inputs.cn_url)
    cn_refuse = cn_enabled and cn_health == "DEGRADED"
    health_url = inputs.cn_healthcheck_url

    lines: list[str] = []
    add = lines.append

    add("# Rendered by mihomo-proxy-management - DO NOT COMMIT (0600 secret file).")
    add(f"# schema_version: {SCHEMA_VERSION}")
    add(f"# mode: {mode}  (degraded={not tun})")
    add(f"# foreign provider: {FOREIGN_PROVIDER} -> {describe_url(inputs.foreign_url)}")
    if cn_enabled:
        add(f"# cn provider: {CN_PROVIDER} -> {describe_url(inputs.cn_url)}")
    else:
        add("# cn provider: disabled (no MPM_CN_SUBSCRIPTION_URL)")
    if not tun:
        for reason in preflight.degraded_reasons:
            add(f"# DEGRADED reason: {reason}")
    add("")
    add("external-controller: 127.0.0.1:%d" % CONTROLLER_PORT)
    add("secret: %s" % yaml_quote(inputs.controller_secret))
    add("allow-lan: false")
    add("bind-address: 127.0.0.1")
    add("mixed-port: %d" % MIXED_PORT)
    add("ipv6: %s" % overrides.ipv6)
    add("log-level: %s" % overrides.log_level)
    add("")
    add("unified-delay: %s" % overrides.unified_delay)
    add("tcp-concurrent: %s" % overrides.tcp_concurrent)
    add("find-process-mode: off")
    add("global-client-fingerprint: chrome")
    add("")

    # ---- DNS -----------------------------------------------------------
    add("dns:")
    add("  enable: true")
    add("  listen: 127.0.0.1:%d" % DNS_PORT)
    add("  ipv6: %s" % overrides.ipv6)
    if tun:
        add("  enhanced-mode: fake-ip")
        add("  fake-ip-range: %s" % overrides.fake_ip_range)
        add("  fake-ip-filter:")
        for pattern in ("'*.lan'", "'*.local'", "'.localhost'", '"+.msftconnecttest.com"', '"+.msftncsi.com"'):
            add(f"    - {pattern}")
    else:
        # Degraded mode hijacks nothing, so fake-ip would only hand out loopback
        # answers for names the system resolver can already resolve.
        add("  enhanced-mode: redir-host")
    add("  default-nameserver:")
    add("    - 223.5.5.5")
    add("    - 119.29.29.29")
    add("  nameserver:")
    add("    - 223.5.5.5")
    add("  proxy-server-nameserver:")
    add("    - 223.5.5.5")
    if cn_enabled:
        # CN names must resolve over a domestic resolver so CN rules work
        add("  direct-nameserver:")
        add("    - 223.5.5.5")
        add("  direct-nameserver-follow-policy: true")
    add("  fallback:")
    add("    - 8.8.8.8")
    add("    - 1.1.1.1")
    add("  fallback-filter:")
    add("    geoip: true")
    add("    geoip-code: CN")
    add("    geosite:")
    add("      - gfw")
    add("")

    # ---- Geo -----------------------------------------------------------
    add("geodata-mode: true")
    add("geo-auto-update: %s" % overrides.geo_auto_update)
    add("geo-update-interval: %d" % overrides.geo_update_interval)
    add("")

    add("profile:")
    add("  store-selected: true")
    add("  store-fake-ip: %s" % ("true" if tun else "false"))
    add("")

    add("sniffing:")
    add("  enable: %s" % overrides.sniffing)
    add("  sniff-tls-sni: true")
    add("  over-dest-ip-only: false")
    add("  ports:")
    for port in (80, 443, 8443):
        add(f"    - {port}")
    add("")

    # ---- TUN -----------------------------------------------------------
    add("tun:")
    add("  enable: %s" % ("true" if tun else "false"))
    add("  stack: system")
    if tun:
        # D4=A: mihomo owns all routing/firewall.  We never touch nftables,
        # iptables or ip-rule ourselves.
        add("  auto-route: true")
        add("  auto-redirect: false")
        add("  auto-detect-interface: true")
        add("  dns-hijack:")
        add("    - any:53")
        add("    - tcp://any:53")
    else:
        add("  auto-route: false")
        add("  auto-redirect: false")
        add("  auto-detect-interface: false")
        # no hijack in degraded mode - the system resolver stays untouched
        add("  dns-hijack: []")
    add("")

    # ---- providers -----------------------------------------------------
    add("proxy-providers:")
    add(f"  {FOREIGN_PROVIDER}:")
    add("    type: http")
    add("    url: %s" % yaml_quote(inputs.foreign_url))
    add(f"    path: ./providers/{FOREIGN_PROVIDER}.yaml")
    add(f"    interval: {overrides.interval_minutes}")
    add("    health-check:")
    add("      enable: true")
    add("      url: %s" % yaml_quote(FOREIGN_HEALTHCHECK_URL))
    add(f"      interval: {overrides.health_check_interval}")
    add("      timeout: 5000")
    add("      lazy: true")
    if cn_enabled:
        add(f"  {CN_PROVIDER}:")
        add("    type: http")
        add("    url: %s" % yaml_quote(inputs.cn_url))
        add(f"    path: ./providers/{CN_PROVIDER}.yaml")
        add(f"    interval: {overrides.interval_minutes}")
        add("    health-check:")
        add("      enable: true")
        add("      url: %s" % yaml_quote(health_url))
        add(f"      interval: {overrides.health_check_interval}")
        add("      timeout: 5000")
        add("      lazy: true")
    add("")

    # ---- groups --------------------------------------------------------
    add("proxy-groups:")
    add("  - name: PROXY")
    add("    type: select")
    add("    use:")
    add(f"      - {FOREIGN_PROVIDER}")
    add("    proxies:")
    add("      - AUTO-FOREIGN")
    add("      - US-FAST")
    add("      - VIETNAM")
    add("      - DIRECT")
    add("")
    add("  - name: AUTO-FOREIGN")
    add("    type: url-test")
    add("    use:")
    add(f"      - {FOREIGN_PROVIDER}")
    add("    url: %s" % yaml_quote(FOREIGN_HEALTHCHECK_URL))
    add(f"    interval: {overrides.health_check_interval}")
    add("    tolerance: 100")
    add("    lazy: true")
    add("")
    add("  - name: US-FAST")
    add("    type: url-test")
    add("    use:")
    add(f"      - {FOREIGN_PROVIDER}")
    add('    filter: "(?i)(us|usa|united states|america|\U0001F1FA\U0001F1F8)"')
    add("    url: %s" % yaml_quote(FOREIGN_HEALTHCHECK_URL))
    add(f"    interval: {overrides.health_check_interval}")
    add("    tolerance: 50")
    add("    lazy: true")
    add("")
    add("  - name: VIETNAM")
    add("    type: select")
    add("    use:")
    add(f"      - {FOREIGN_PROVIDER}")
    add('    filter: "(?i)(vietnam|sg|singapore|hanoi|\U0001F1FB\U0001F1F3)"')
    add("    proxies:")
    add("      - AUTO-FOREIGN")
    add("")
    add("  - name: GLOBAL")
    add("    type: select")
    add("    proxies:")
    add("      - PROXY")
    add("      - AUTO-FOREIGN")
    add("      - US-FAST")
    add("      - VIETNAM")
    add("      - DIRECT")
    if cn_enabled:
        add("")
        add("  - name: AUTO-CN")
        add("    type: url-test")
        add("    use:")
        add(f"      - {CN_PROVIDER}")
        add("    url: %s" % yaml_quote(health_url))
        add(f"    interval: {overrides.health_check_interval}")
        add("    tolerance: 80")
        add("    lazy: true")
        add("")
        add("  - name: CN-EXIT")
        add("    type: select")
        add("    use:")
        add(f"      - {CN_PROVIDER}")
        add("    proxies:")
        add("      - AUTO-CN")
        add("    # deliberately no DIRECT and no foreign group here (D8b=2)")
        if cn_refuse:
            add("    # CN provider has no live node: CN rules below REFUSE instead of")
            add("    # selecting a dead node, and never fall back to DIRECT or foreign")
    add("")

    # ---- rules ---------------------------------------------------------
    add("rules:")
    add("  - GEOSITE,private,DIRECT")
    add("  - GEOIP,private,DIRECT,no-resolve")
    for rule in overrides.extra_rules:
        # validated against the CN semantics of *this* host before it is placed
        # above the built-in rules (mihomo uses the first matching rule)
        add(f"  - {validate_extra_rule(rule, cn_configured=cn_enabled, cn_live=not cn_refuse)}")
    add("  - GEOSITE,openai,US-FAST")
    add("  - GEOSITE,anthropic,US-FAST")
    add("  - GEOSITE,github,US-FAST")
    add("  - GEOSITE,google,PROXY")
    add("  - GEOSITE,tiktok,VIETNAM")
    add("  - DOMAIN-SUFFIX,tiktok.com,VIETNAM")
    if cn_enabled:
        # CN destinations are pinned to the CN provider only; a broken CN
        # provider fails these rules instead of falling back (D8b=2).
        policy = "REJECT" if cn_refuse else "CN-EXIT"
        add(f"  - GEOSITE,cn,{policy}")
        add(f"  - GEOIP,cn,{policy},no-resolve")
        if cn_refuse:
            add("# CN health: DEGRADED - the CN rules above refuse instead of selecting")
            add("# a dead node; they do not fall back to DIRECT or to a foreign node")
    # No CN provider => no CN rules are rendered at all (D8b=2): a
    # GEOSITE/GEOIP,cn,DIRECT row would both claim a CN feature that does not
    # exist and be a silent DIRECT fallback.
    add("  - MATCH,PROXY")
    add("")
    return "\n".join(lines)


# ---- static audit -----------------------------------------------------------

_GROUP_BLOCK = re.compile(r"^  - name: (?P<name>[A-Za-z0-9_-]+)$")
_PROVIDER_BLOCK = re.compile(r"^  (?P<name>[a-z0-9-]+):$", re.IGNORECASE)

FOREIGN_ONLY_GROUPS = ("PROXY", "AUTO-FOREIGN", "US-FAST", "VIETNAM")
CN_ONLY_GROUPS = ("AUTO-CN", "CN-EXIT")
# A rule may only send traffic here.  ``REJECT`` is the explicit refuse used
# when the CN provider is configured but has no live node.
ALLOWED_RULE_POLICIES = set(FOREIGN_ONLY_GROUPS) | set(CN_ONLY_GROUPS) | {
    "DIRECT",
    "REJECT",
    "PASS",
    "GLOBAL",
}

_RULE_TYPES = {
    "DOMAIN",
    "DOMAIN-SUFFIX",
    "DOMAIN-KEYWORD",
    "GEOSITE",
    "GEOIP",
    "IPCIDR",
    "SRC-IP-CIDR",
    "SRC-IP-CIDR-PRIVATE",
    "DST-PORT",
    "SRC-PORT",
    "PROCESS-PATH",
    "PROCESS-NAME",
    "RULE-SET",
    "NETWORK_TYPE",
}


def split_rule(rule: str) -> tuple[str, str, str]:
    """``(TYPE, target, policy)`` of a rule line, normalised for comparison."""
    parts = [part.strip() for part in rule.split(",")]
    if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
        raise FailClosed(f"rule '{rule}' is not TYPE,target,policy")
    return parts[0].upper(), parts[1].lower(), parts[2]


# targets that mean "this is CN traffic" - a rule matching one of these may only
# use the CN groups or the explicit REJECT refuse (D8b=2)
CN_RULE_TARGETS = {"cn"}
CN_DOMAIN_SUFFIXES = {"cn", ".cn", "*.cn"}


def _matches_cn_target(rule_type: str, target: str) -> bool:
    if rule_type in {"GEOSITE", "GEOIP", "RULE-SET"}:
        return target in CN_RULE_TARGETS
    if rule_type in {"DOMAIN-SUFFIX", "DOMAIN"}:
        return target in CN_DOMAIN_SUFFIXES
    return False


def _matches_non_cn_target(rule_type: str, target: str) -> bool:
    """Everything-but-CN pseudo sets: a superset of our foreign rules."""
    return rule_type in {"GEOSITE", "GEOIP", "RULE-SET"} and target == "geolocation-!cn"


def validate_extra_rule(rule: str, *, cn_configured: bool, cn_live: bool) -> str:
    """Return ``rule`` if it cannot weaken CN semantics, else fail closed.

    Three bypass classes are refused (D8b=2):

    * CN -> DIRECT (silent local fallback)
    * CN -> a foreign group (silent overseas fallback)
    * an *earlier-matching equivalent*: ``MATCH`` or ``geolocation-!cn`` rows,
      or a CN-shaped domain suffix, placed before the built-in rules shadows
      them, because mihomo uses the first matching rule.
    """
    rule_type, target, policy = split_rule(rule)
    if rule_type == "MATCH":
        raise FailClosed("a MATCH override would shadow every built-in rule below it")
    if rule_type not in _RULE_TYPES:
        raise FailClosed(f"rule type '{rule_type}' is not allowed in an override")
    if policy not in ALLOWED_RULE_POLICIES:
        raise FailClosed(
            f"rule policy '{policy}' is not a group this project renders; an unknown "
            f"target silently drops the rule at runtime"
        )
    if _matches_cn_target(rule_type, target):
        if not cn_configured:
            raise FailClosed(
                "no CN provider is configured, so a CN rule may not be added "
                "(it would be a DIRECT/foreign fallback in disguise)"
            )
        if policy == "DIRECT":
            raise FailClosed("a CN rule may not fall back to DIRECT")
        if policy in FOREIGN_ONLY_GROUPS or policy == "GLOBAL":
            raise FailClosed("a CN rule may not fall back to a foreign group")
        if policy in CN_ONLY_GROUPS and not cn_live:
            raise FailClosed(
                "the CN provider has no live node, so CN rules must be a refuse, "
                "not a CN group that would select a dead node"
            )
        if policy == "REJECT" and cn_live:
            raise FailClosed("a REJECT CN rule contradicts a healthy CN provider")
    if _matches_non_cn_target(rule_type, target) and policy == "DIRECT":
        raise FailClosed(
            "a 'geolocation-!cn' DIRECT rule bypasses every foreign rule above it"
        )
    return rule


def _rendered_rules(text: str) -> list[str]:
    rules: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("rules:"):
            inside = True
            continue
        if inside and line and not line.startswith((" ", "#")):
            break
        if inside and line.startswith("  - "):
            rules.append(line[4:].strip())
    return rules


def _rule_parts(line: str) -> tuple[str, str, str] | None:
    """Lenient rule parse for the audit: ``None`` when it is not a triple."""
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
        return None
    return parts[0].upper(), parts[1].lower(), parts[2]


def _groups(text: str) -> dict[str, dict[str, list[str]]]:
    """Extract ``use``/``proxies`` lists per proxy-group name."""
    groups: dict[str, dict[str, list[str]]] = {}
    section = ""
    current = ""
    bucket = ""
    for line in text.splitlines():
        if line.startswith("proxy-providers:"):
            section = "providers"
            continue
        if line.startswith("proxy-groups:"):
            section = "groups"
            current = ""
            continue
        if line.startswith("rules:"):
            section = "rules"
            continue
        if line and not line.startswith((" ", "#")) and line.rstrip().endswith(":"):
            section = "other"
            continue
        if section != "groups":
            continue
        match = _GROUP_BLOCK.match(line)
        if match:
            current = match.group("name")
            groups[current] = {"use": [], "proxies": []}
            bucket = ""
            continue
        stripped = line.strip()
        if not current:
            continue
        if stripped in ("use:", "proxies:"):
            bucket = stripped.rstrip(":")
            continue
        if stripped.startswith("- ") and bucket:
            groups[current][bucket].append(stripped[2:].strip().strip('"'))
    return groups


def _providers(text: str) -> list[str]:
    names: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("proxy-providers:"):
            inside = True
            continue
        if inside and line and not line.startswith((" ", "#")):
            break
        if inside:
            match = _PROVIDER_BLOCK.match(line)
            if match:
                names.append(match.group("name"))
    return names


def audit(text: str, *, cn_configured: bool, cn_live: bool = True) -> None:
    """Fail closed on any structural or secret-hygiene problem.

    ``cn_live`` says whether the CN provider currently has a live node: a
    configured-but-dead provider must render an explicit refuse, and the audit
    is what guarantees no CN rule can quietly point at DIRECT or a foreign
    group (D8b=2).
    """
    if PLACEHOLDER_RE.search(text):
        leftover = sorted(set(PLACEHOLDER_RE.findall(text)))
        raise FailClosed(f"unresolved placeholders in rendered config: {', '.join(leftover)}")

    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise FailClosed(f"config audit failed: {message}")

    _require(re.search(r"^allow-lan: false$", text, re.M) is not None, "allow-lan must be false")
    _require(
        re.search(r"^external-controller: 127\.0\.0\.1:\d+$", text, re.M) is not None,
        "external-controller must bind 127.0.0.1",
    )
    _require(
        re.search(r"^bind-address: 127\.0\.0\.1$", text, re.M) is not None,
        "bind-address must be 127.0.0.1",
    )
    secret_match = re.search(r'^secret: "(?P<v>[^"]*)"', text, re.M)
    _require(secret_match is not None and secret_match.group("v") != "", "controller secret is empty")
    foreign_url = re.search(
        rf'^    url: "(?P<v>[^"]*)"$', text, re.M
    )
    _require(foreign_url is not None and foreign_url.group("v") != "", "foreign provider url is empty")

    providers = _providers(text)
    _require(FOREIGN_PROVIDER in providers, f"{FOREIGN_PROVIDER} provider missing")
    if cn_configured:
        _require(CN_PROVIDER in providers, f"{CN_PROVIDER} provider missing")
    else:
        _require(CN_PROVIDER not in providers, f"{CN_PROVIDER} must not exist without a CN url")

    groups = _groups(text)
    for name in FOREIGN_ONLY_GROUPS:
        _require(name in groups, f"group {name} missing")
        use = groups[name]["use"]
        _require(
            use in ([], [FOREIGN_PROVIDER]),
            f"group {name} must only use {FOREIGN_PROVIDER} (got {use})",
        )
        _require(
            CN_PROVIDER not in use,
            f"foreign group {name} must not reference {CN_PROVIDER}",
        )
    for name in CN_ONLY_GROUPS:
        if not cn_configured:
            _require(name not in groups, f"{name} must not be rendered when CN is disabled")
            continue
        _require(name in groups, f"group {name} missing")
        use = groups[name]["use"]
        _require(use == [CN_PROVIDER], f"CN group {name} must only use {CN_PROVIDER} (got {use})")
        _require(FOREIGN_PROVIDER not in use, f"CN group {name} leaks the foreign provider")
    if cn_configured:
        cn_exit = groups[CN_ONLY_GROUPS[1]]
        _require(
            "DIRECT" not in cn_exit["proxies"],
            "CN-EXIT may not offer DIRECT (silent fallback is forbidden)",
        )
        _require(
            not any(p in FOREIGN_ONLY_GROUPS for p in cn_exit["proxies"]),
            "CN-EXIT may not offer a foreign group (silent fallback is forbidden)",
        )
        wanted = "CN-EXIT" if cn_live else "REJECT"
        _require(
            re.search(rf"^[ \t]*- GEOSITE,cn,{wanted}$", text, re.M) is not None,
            f"CN rules must route to {wanted} when CN is configured (live={cn_live})",
        )
        for line in _rendered_rules(text):
            parts = _rule_parts(line)
            if parts is None or not _matches_cn_target(parts[0], parts[1]):
                continue
            _require(
                parts[2] == wanted,
                f"rule '{line}' contradicts the CN routing policy '{wanted}'",
            )
    else:
        for line in _rendered_rules(text):
            parts = _rule_parts(line)
            if parts is None:
                continue
            _require(
                not _matches_cn_target(parts[0], parts[1]),
                f"no CN rule may be rendered when CN is disabled (got '{line}')",
            )

    tun_enable = re.search(r"^tun:\n  enable: (?P<v>true|false)$", text, re.M)
    _require(tun_enable is not None, "tun.enable missing")
    if tun_enable.group("v") == "true":
        _require(
            re.search(r"^  auto-route: true$", text, re.M) is not None,
            "TUN mode requires mihomo auto-route (D4=A)",
        )
        _require(
            re.search(r"^  dns-hijack:\n    - any:53$", text, re.M) is not None,
            "TUN mode requires transparent DNS hijack (D5=A)",
        )
        _require(
            re.search(r"^  enhanced-mode: fake-ip$", text, re.M) is not None,
            "TUN mode requires fake-ip",
        )
    else:
        _require(
            re.search(r"^  dns-hijack: \[\]$", text, re.M) is not None,
            "degraded mode must disable DNS hijack",
        )
        _require(
            re.search(r"^  auto-route: false$", text, re.M) is not None,
            "degraded mode must not ask mihomo to auto-route",
        )
        # Degraded mode owns no traffic path and hijacks nothing, so fake-ip
        # would only invent loopback answers the host did not ask for.  Reject
        # every trace of it, and any hijack target, explicitly.
        for token, label in (
            (r"^[ \t]*enhanced-mode:[ \t]*['\"]?fake-ip", "enhanced-mode: fake-ip"),
            (r"^[ \t]*fake-ip-range:", "fake-ip-range"),
            (r"^[ \t]*fake-ip-filter:", "fake-ip-filter"),
            (r"^[ \t]*fake-ip-range-v6:", "fake-ip-range-v6"),
            (r"^[ \t]*store-fake-ip:[ \t]*true", "store-fake-ip: true"),
            (r"^[ \t]*-[ \t]*['\"]?any:53", "a dns-hijack any:53 entry"),
            (r"^[ \t]*dns-hijack:[ \t]*['\"]?(?:any|tcp://|\d)", "a non-empty dns-hijack list"),
        ):
            _require(
                re.search(token, text, re.M) is None,
                f"degraded mode must not enable {label} (DNS hijack/fake-ip is TUN-only, D5=A)",
            )
        _require(
            re.search(r"^[ \t]*enhanced-mode:[ \t]*['\"]?redir-host", text, re.M) is not None,
            "degraded mode must state dns.enhanced-mode: redir-host explicitly",
        )
    _require(
        re.search(r"^\s*(?:ip tables|nftables|iptables)", text, re.I) is None,
        "the project must not emit firewall directives (D4=A)",
    )


__all__ = [
    "ALLOWED_RULE_POLICIES",
    "CN_ONLY_GROUPS",
    "CN_PROVIDER",
    "CONTROLLER_PORT",
    "DENIED_OVERRIDE_KEYS",
    "DNS_PORT",
    "FOREIGN_ONLY_GROUPS",
    "FOREIGN_PROVIDER",
    "MIXED_PORT",
    "Overrides",
    "PLACEHOLDER_RE",
    "PROXY_PORT",
    "SCHEMA_VERSION",
    "audit",
    "load_overrides",
    "render",
    "split_rule",
    "validate_extra_rule",
    "yaml_quote",
]
