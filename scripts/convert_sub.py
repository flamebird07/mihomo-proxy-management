#!/usr/bin/env python3
"""
convert_sub.py - Convert a proxy subscription into mihomo / Clash.Meta YAML.

Three subscription families are auto-detected:
  1. Clash / mihomo YAML -- original node fields and nesting preserved.
  2. sing-box JSON -- supported node outbounds mapped to mihomo.
  3. base64-encoded or plain-text URI lists, or a single share URI.

Supported URI schemes (parsed natively, never disguised):
  anytls, ss, trojan, vless, tuic, hysteria2 (hy2).
Unknown URI schemes (e.g. `vmess://`) are always warned, counted and skipped.
Unknown Clash YAML `type` values are kept by default and only dropped when
`--skip-unknown` is given explicitly.

Protocols are NEVER faked: an `anytls://` node becomes `type: anytls` with a
`password` field (mihomo >= 1.19.x supports anytls natively). It is not rewritten
into vless/uuid.

Security properties:
  * Subscription URLs and file paths are not printed in diagnostics.
  * Errors use a fixed classification, HTTP numeric status, or exception type.
  * PyYAML is mandatory: the tool fails closed rather than hand-building YAML.
  * Diagnostics use node ordinals and allowlisted types, never upstream names.
  * A malformed Clash YAML is never degraded into a URI list; only a pure URI
    list may take the URI fallback, and stray non-URI lines are counted.

Usage:
  python convert_sub.py --url-env SUB_URL -o providers/subscription.yaml
  python convert_sub.py -i subscription_raw.txt --list
  set SUB_URL=https://... & python convert_sub.py --url-env SUB_URL -o out.yaml
  python convert_sub.py --uri "anytls://pw@host:443?sni=host#node" --list
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
    HAS_YAML = True
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    yaml = None  # type: ignore
    HAS_YAML = False


DEFAULT_USER_AGENT = "ClashMeta/1.18.0"

# Proxy `type` values we recognise when reading Clash YAML input. Anything else
# is reported as unknown (and optionally skipped with --skip-unknown).
KNOWN_PROXY_TYPES = {
    "ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2",
    "tuic", "anytls", "snell", "http", "socks5",
}

# URI scheme -> mihomo proxy type. Only these schemes are converted.
URI_SCHEMES = {
    "anytls": "anytls",
    "ss": "ss",
    "trojan": "trojan",
    "vless": "vless",
    "tuic": "tuic",
    "hysteria2": "hysteria2",
    "hy2": "hysteria2",
}

EXIT_OK = 0
EXIT_EMPTY = 1
EXIT_USAGE = 2
EXIT_BAD_INPUT = 3
EXIT_NETWORK = 4
EXIT_PARTIAL = 5
DEFAULT_MIN_NODES = 1
SINGBOX_TYPES = {
    "shadowsocks": "ss", "trojan": "trojan", "vless": "vless",
    "tuic": "tuic", "hysteria2": "hysteria2", "anytls": "anytls",
}
NON_NODE_TYPES = {"selector", "urltest", "direct", "block", "dns"}
SOURCE_KEYS = {
    *(f"SUBSCRIPTION_URL_{i}" for i in range(1, 4)),
    *(f"SUBSCRIPTION_UA_{i}" for i in range(1, 4)),
    "MIN_CANDIDATE_NODES",
}


class ConvertError(Exception):
    """Raised for any recoverable, user-facing failure."""

    def __init__(self, message: str, exit_code: int = EXIT_BAD_INPUT):
        super().__init__(message)
        self.exit_code = exit_code


class Stats:
    """Counts reported to the operator so nothing is dropped silently."""

    def __init__(self) -> None:
        self.input_nodes = 0
        self.output_nodes = 0
        self.skipped_malformed = 0
        self.unknown_type = 0
        self.skipped_unknown = 0
        self.skipped_non_uri = 0
        self.skipped_non_node = 0
        self.skipped_unsupported = 0
        self.failed = 0
        self.input_format = "unrecognised"

    def summary(self) -> str:
        return (
            f"format={self.input_format} input={self.input_nodes} output={self.output_nodes} "
            f"skipped_malformed={self.skipped_malformed} "
            f"unknown_type={self.unknown_type} "
            f"skipped_unknown={self.skipped_unknown} "
            f"skipped_non_uri={self.skipped_non_uri} "
            f"skipped_non_node={self.skipped_non_node} "
            f"skipped_unsupported={self.skipped_unsupported} failed={self.failed}"
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _default_warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _p(params: Dict[str, List[str]], key: str, default: Optional[str] = None) -> Optional[str]:
    values = params.get(key)
    if not values:
        return default
    return values[0]


def _safe_type(value: Any) -> str:
    # Even a bare name or unknown type may be an upstream credential.
    allowed = KNOWN_PROXY_TYPES | set(URI_SCHEMES) | set(SINGBOX_TYPES) | NON_NODE_TYPES
    return value if isinstance(value, str) and value in allowed else "<unknown>"


def redact_url(url: str) -> str:
    """Return `scheme://host[:port]/<redacted>` without path/query/userinfo.

    Fully defensive: a malformed URL (bad port, broken IPv6 bracket, control
    characters, ...) degrades to a constant and never raises or echoes input.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return "<redacted-url>"
        try:
            port = parts.port
        except ValueError:
            port = None
        suffix = f":{port}" if port else ""
        return f"{parts.scheme}://{parts.hostname}{suffix}/<redacted>"
    except Exception:
        return "<redacted-url>"


def _split_host_port(hostpart: str) -> Tuple[str, Optional[int]]:
    hostpart = hostpart.strip()
    if not hostpart:
        raise ConvertError("missing host in URI")
    if hostpart.startswith("["):
        end = hostpart.find("]")
        if end == -1:
            raise ConvertError("malformed IPv6 host in URI")
        host = hostpart[1:end]
        rest = hostpart[end + 1:]
        if rest.startswith(":"):
            port_str = rest[1:]
        elif rest == "":
            port_str = ""
        else:
            raise ConvertError("malformed IPv6 host in URI")
    else:
        if ":" in hostpart:
            host, port_str = hostpart.rsplit(":", 1)
        else:
            host, port_str = hostpart, ""
    host = urllib.parse.unquote(host)
    if not host:
        raise ConvertError("missing host in URI")
    port: Optional[int] = None
    if port_str:
        try:
            port = int(port_str)
        except ValueError:
            raise ConvertError("invalid port in URI")
        if not 0 < port < 65536:
            raise ConvertError("port out of range in URI")
    return host, port


def _split_uri(uri: str) -> Tuple[str, str, str]:
    """Split a share URI into (scheme, rest, fragment-name)."""
    uri = uri.strip()
    if "://" not in uri:
        raise ConvertError("not a valid URI (missing scheme)")
    if "#" in uri:
        base, frag = uri.split("#", 1)
        name = urllib.parse.unquote(frag)
    else:
        base, name = uri, ""
    scheme, rest = base.split("://", 1)
    return scheme.lower(), rest, name


def _split_authority(rest: str) -> Dict[str, Any]:
    """Split the part after `scheme://` into userinfo/host/port/path/params."""
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    if "@" in rest:
        userinfo, hostpart = rest.rsplit("@", 1)
    else:
        userinfo, hostpart = "", rest
    path = ""
    if "/" in hostpart:
        hostpart, tail = hostpart.split("/", 1)
        path = "/" + tail
    host, port = _split_host_port(hostpart)
    params = urllib.parse.parse_qs(query, keep_blank_values=True) if query else {}
    return {
        "userinfo": urllib.parse.unquote(userinfo),
        "host": host,
        "port": port,
        "path": path,
        "params": params,
    }


def _alpn_list(params: Dict[str, List[str]]) -> List[str]:
    raw = _p(params, "alpn")
    if not raw:
        return []
    return [token.strip() for token in raw.split(",") if token.strip()]


def _apply_ws_opts(proxy: Dict[str, Any], params: Dict[str, List[str]], path: str) -> None:
    ws_path = _p(params, "path") or path or "/"
    ws_host = _p(params, "host") or proxy["server"]
    proxy["ws-opts"] = {"path": ws_path, "headers": {"Host": ws_host}}


def _apply_grpc_opts(proxy: Dict[str, Any], params: Dict[str, List[str]]) -> None:
    service = _p(params, "serviceName") or _p(params, "servicename") or ""
    proxy["grpc-opts"] = {"grpc-service-name": service}


# ---------------------------------------------------------------------------
# URI parsers (native -> mihomo types, never disguised)
# ---------------------------------------------------------------------------

def parse_anytls_uri(uri: str) -> Dict[str, Any]:
    """anytls://password@host:port?sni=..&insecure=1&fp=chrome&alpn=h2#name

    anytls carries a *password* (not a uuid) and is emitted as `type: anytls`.
    """
    scheme, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    password = parts["userinfo"]
    if not password:
        raise ConvertError("anytls URI is missing a password")
    port = parts["port"] or 443
    params = parts["params"]

    proxy: Dict[str, Any] = {
        "name": name or f"anytls-{parts['host']}:{port}",
        "type": "anytls",
        "server": parts["host"],
        "port": port,
        "password": password,
        "udp": True,
    }

    sni = _p(params, "sni") or _p(params, "peer") or parts["host"]
    proxy["sni"] = sni
    if _truthy(_p(params, "insecure")) or _truthy(_p(params, "allowInsecure")) \
            or _truthy(_p(params, "skip-cert-verify")):
        proxy["skip-cert-verify"] = True
    fp = _p(params, "fp") or _p(params, "client-fingerprint")
    if fp:
        proxy["client-fingerprint"] = fp
    alpn = _alpn_list(params)
    if alpn:
        proxy["alpn"] = alpn
    for key in ("idle-session-check-interval", "idle-session-timeout", "min-idle-session"):
        value = _p(params, key)
        if value is None:
            continue
        try:
            proxy[key] = int(value)
        except ValueError:
            proxy[key] = value
    return proxy


def _decode_ss_userinfo(userinfo: str) -> Tuple[str, str]:
    if ":" in userinfo:
        method, _, password = userinfo.partition(":")
        return method, password
    decoded = _b64decode(userinfo)
    method, _, password = decoded.partition(":")
    if not method:
        raise ConvertError("ss URI has a malformed userinfo")
    return method, password


def parse_ss_uri(uri: str) -> Dict[str, Any]:
    scheme, rest, name = _split_uri(uri)
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    params = urllib.parse.parse_qs(query, keep_blank_values=True) if query else {}

    if "@" in rest:
        userinfo, hostpart = rest.rsplit("@", 1)
        userinfo = urllib.parse.unquote(userinfo)
        method, password = _decode_ss_userinfo(userinfo)
    else:
        # legacy: ss://base64(method:password@host:port)
        decoded = _b64decode(rest)
        if "@" not in decoded:
            raise ConvertError("ss URI has a malformed payload")
        creds, hostpart = decoded.rsplit("@", 1)
        method, _, password = creds.partition(":")

    hostpart = hostpart.split("/", 1)[0]
    host, port = _split_host_port(hostpart)
    if port is None:
        raise ConvertError("ss URI is missing a port")

    proxy: Dict[str, Any] = {
        "name": name or f"ss-{host}:{port}",
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }

    plugin = _p(params, "plugin")
    if plugin:
        proxy["plugin"] = plugin
        plugin_opts = _p(params, "plugin-opts") or _p(params, "plugin_opts")
        if plugin_opts:
            proxy["plugin-opts"] = plugin_opts
    return proxy


def parse_trojan_uri(uri: str) -> Dict[str, Any]:
    scheme, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    password = parts["userinfo"]
    if not password:
        raise ConvertError("trojan URI is missing a password")
    port = parts["port"] or 443
    params = parts["params"]

    proxy: Dict[str, Any] = {
        "name": name or f"trojan-{parts['host']}:{port}",
        "type": "trojan",
        "server": parts["host"],
        "port": port,
        "password": password,
        "udp": True,
    }
    proxy["sni"] = _p(params, "sni") or _p(params, "peer") or parts["host"]
    if _truthy(_p(params, "insecure")) or _truthy(_p(params, "allowInsecure")) \
            or _truthy(_p(params, "skip-cert-verify")):
        proxy["skip-cert-verify"] = True
    alpn = _alpn_list(params)
    if alpn:
        proxy["alpn"] = alpn
    network = _p(params, "type") or "tcp"
    if network == "ws":
        proxy["network"] = "ws"
        _apply_ws_opts(proxy, params, parts["path"])
    elif network == "grpc":
        proxy["network"] = "grpc"
        _apply_grpc_opts(proxy, params)
    return proxy


def parse_vless_uri(uri: str) -> Dict[str, Any]:
    scheme, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    uuid = parts["userinfo"]
    if not uuid:
        raise ConvertError("vless URI is missing a uuid")
    port = parts["port"] or 443
    params = parts["params"]

    network = _p(params, "type") or "tcp"
    proxy: Dict[str, Any] = {
        "name": name or f"vless-{parts['host']}:{port}",
        "type": "vless",
        "server": parts["host"],
        "port": port,
        "uuid": uuid,
        "network": network,
        "udp": True,
    }

    security = (_p(params, "security") or "").lower()
    tls = security in ("tls", "reality", "xtls")
    if tls:
        proxy["tls"] = True
        proxy["servername"] = _p(params, "sni") or _p(params, "servername") \
            or _p(params, "peer") or parts["host"]
    fp = _p(params, "fp")
    if fp:
        proxy["client-fingerprint"] = fp
    flow = _p(params, "flow")
    if flow:
        proxy["flow"] = flow
    alpn = _alpn_list(params)
    if alpn:
        proxy["alpn"] = alpn
    if _truthy(_p(params, "insecure")) or _truthy(_p(params, "allowInsecure")):
        proxy["skip-cert-verify"] = True
    if network == "ws":
        _apply_ws_opts(proxy, params, parts["path"])
    elif network == "grpc":
        _apply_grpc_opts(proxy, params)
    if security == "reality":
        proxy["reality-opts"] = {
            "public-key": _p(params, "pbk") or "",
            "short-id": _p(params, "sid") or "",
        }
    return proxy


def parse_tuic_uri(uri: str) -> Dict[str, Any]:
    scheme, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    userinfo = parts["userinfo"]
    if ":" not in userinfo:
        raise ConvertError("tuic URI is missing uuid:password")
    uuid, _, password = userinfo.partition(":")
    if not uuid or not password:
        raise ConvertError("tuic URI is missing uuid or password")
    port = parts["port"] or 443
    params = parts["params"]

    proxy: Dict[str, Any] = {
        "name": name or f"tuic-{parts['host']}:{port}",
        "type": "tuic",
        "server": parts["host"],
        "port": port,
        "uuid": uuid,
        "password": password,
        "udp": True,
    }
    proxy["sni"] = _p(params, "sni") or _p(params, "peer") or parts["host"]
    udp_relay = _p(params, "udp_relay_mode") or _p(params, "udp-relay-mode")
    if udp_relay:
        proxy["udp-relay-mode"] = udp_relay
    congestion = _p(params, "congestion_control") or _p(params, "congestion-controller")
    if congestion:
        proxy["congestion-controller"] = congestion
    alpn = _alpn_list(params)
    if alpn:
        proxy["alpn"] = alpn
    if _truthy(_p(params, "insecure")) or _truthy(_p(params, "allow_insecure")) \
            or _truthy(_p(params, "allowInsecure")):
        proxy["skip-cert-verify"] = True
    return proxy


def parse_hysteria2_uri(uri: str) -> Dict[str, Any]:
    scheme, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    port = parts["port"] or 443
    params = parts["params"]

    proxy: Dict[str, Any] = {
        "name": name or f"hysteria2-{parts['host']}:{port}",
        "type": "hysteria2",
        "server": parts["host"],
        "port": port,
        "password": parts["userinfo"],
        "udp": True,
    }
    proxy["sni"] = _p(params, "sni") or _p(params, "peer") or parts["host"]
    if _truthy(_p(params, "insecure")) or _truthy(_p(params, "allowInsecure")) \
            or _truthy(_p(params, "skip-cert-verify")):
        proxy["skip-cert-verify"] = True
    alpn = _alpn_list(params)
    if alpn:
        proxy["alpn"] = alpn
    obfs = _p(params, "obfs")
    if obfs:
        proxy["obfs"] = obfs
        proxy["obfs-password"] = _p(params, "obfs-password") or _p(params, "obfs_password") or ""
    fast_open = _p(params, "fast-open")
    if fast_open is not None and _truthy(fast_open):
        proxy["fast-open"] = True
    up = _p(params, "up")
    down = _p(params, "down")
    if up and down:
        try:
            proxy["up"] = int(up)
            proxy["down"] = int(down)
        except ValueError:
            pass
    return proxy


_URI_PARSERS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "anytls": parse_anytls_uri,
    "ss": parse_ss_uri,
    "trojan": parse_trojan_uri,
    "vless": parse_vless_uri,
    "tuic": parse_tuic_uri,
    "hysteria2": parse_hysteria2_uri,
}


# ---------------------------------------------------------------------------
# base64 / input detection
# ---------------------------------------------------------------------------

def _b64decode(text: str) -> str:
    compact = "".join(text.split())
    if not compact:
        raise ConvertError("empty base64 payload")
    padded = compact + "=" * ((4 - len(compact) % 4) % 4)
    last_error: Optional[Exception] = None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError) as exc:
            last_error = exc
            continue
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ConvertError("invalid base64 payload") from last_error


def _looks_like_base64(text: str) -> bool:
    compact = "".join(text.split())
    if not compact:
        return False
    return re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact) is not None


def _split_uri_lines(text: str) -> Tuple[List[str], int]:
    """Split text into (uri_lines, dropped_non_uri_lines).

    Blank lines are ignored; any non-blank line without `://` is *counted* so
    it is never dropped silently.
    """
    uris: List[str] = []
    dropped = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "://" in stripped:
            uris.append(stripped)
        else:
            dropped += 1
    return uris, dropped


def _extract_proxies(data: Any) -> List[Any]:
    if not isinstance(data, dict):
        raise ConvertError("invalid top-level: expected a YAML mapping", EXIT_BAD_INPUT)
    if "proxies" not in data:
        raise ConvertError("invalid top-level: YAML mapping has no 'proxies' key", EXIT_BAD_INPUT)
    proxies = data["proxies"]
    if proxies is None:
        raise ConvertError("invalid top-level: 'proxies' is empty", EXIT_BAD_INPUT)
    if not isinstance(proxies, list):
        raise ConvertError("invalid top-level: 'proxies' must be a list", EXIT_BAD_INPUT)
    return proxies


def detect_input(content: str) -> Tuple[str, Any, int]:
    """Return (yaml/sing-box/uris mode, payload, dropped_non_uri_lines)."""
    content = content.lstrip("\ufeff").strip()
    if not content:
        raise ConvertError("empty input", EXIT_BAD_INPUT)

    if re.match(r'^\{\s*"', content):
        try:
            data = json.loads(content)
        except (ValueError, RecursionError):
            raise ConvertError("malformed JSON input; output not written") from None
        if "outbounds" in data:
            if not isinstance(data["outbounds"], list):
                raise ConvertError("JSON 'outbounds' must be a list")
            return "sing-box", data["outbounds"], 0
        return "yaml", _extract_proxies(data), 0

    if HAS_YAML:
        try:
            data = yaml.safe_load(content)
        except Exception as exc:
            # A *pure* URI list is never valid YAML either, so only that exact
            # shape may fall back to URI parsing. A malformed Clash YAML that
            # merely *contains* URLs must fail closed instead of having its
            # inner URLs mined out as nodes.
            uris, dropped = _split_uri_lines(content)
            if uris and dropped == 0:
                return "uris", uris, 0
            raise ConvertError(
                f"malformed YAML input ({type(exc).__name__}); "
                "refusing to fall back to URI parsing",
                EXIT_BAD_INPUT,
            )
        if isinstance(data, dict):
            return "yaml", _extract_proxies(data), 0
        if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
            return "yaml", data, 0

    if _looks_like_base64(content):
        try:
            decoded = _b64decode(content)
        except ConvertError:
            decoded = ""
        if "://" in decoded:
            uris, dropped = _split_uri_lines(decoded)
            if uris:
                return "uris", uris, dropped

    uris, dropped = _split_uri_lines(content)
    if uris:
        return "uris", uris, dropped

    raise ConvertError(
        "unrecognised input: not Clash YAML, sing-box JSON, base64 subscription, or URI list",
        EXIT_BAD_INPUT,
    )


# ---------------------------------------------------------------------------
# Node processing
# ---------------------------------------------------------------------------

def process_yaml_proxies(
    proxies: List[Any],
    stats: Stats,
    warn: Callable[[str], None],
    skip_unknown: bool = False,
) -> List[Dict[str, Any]]:
    """Pass through Clash YAML nodes, preserving every original field."""
    output: List[Dict[str, Any]] = []
    for index, node in enumerate(proxies):
        stats.input_nodes += 1
        if not isinstance(node, dict):
            stats.skipped_malformed += 1
            warn(f"malformed node #{index + 1}: not a mapping; skipped")
            continue
        ptype = node.get("type")
        label = f"node#{index + 1}"
        if not isinstance(ptype, str) or not ptype:
            stats.skipped_malformed += 1
            warn(f"malformed node '{label}': missing or invalid 'type'; skipped")
            continue
        if ptype not in KNOWN_PROXY_TYPES:
            stats.unknown_type += 1
            safe_ptype = _safe_type(ptype)
            warn(f"unknown proxy type '{safe_ptype}' in node '{label}'")
            if skip_unknown:
                stats.skipped_unknown += 1
                warn(f"explicitly skipped node '{label}' with unknown type '{safe_ptype}'")
                continue
        output.append(node)
    return output


def process_uris(
    uris: List[str],
    stats: Stats,
    warn: Callable[[str], None],
) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for index, uri in enumerate(uris):
        stats.input_nodes += 1
        label = f"node#{index + 1}"
        scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
        safe_scheme = _safe_type(scheme)
        parser = _URI_PARSERS.get(URI_SCHEMES.get(scheme, ""))
        if parser is None:
            stats.unknown_type += 1
            stats.skipped_unknown += 1
            warn(f"unsupported URI scheme '{safe_scheme}' (node '{label}'); skipped")
            continue
        try:
            proxy = parser(uri)
        except ConvertError as exc:
            stats.skipped_malformed += 1
            warn(f"malformed {safe_scheme} URI (node '{label}'): {exc}")
            continue
        except Exception as exc:  # pragma: no cover - defensive
            stats.skipped_malformed += 1
            warn(f"failed to parse {safe_scheme} URI (node '{label}'): {type(exc).__name__}")
            continue
        output.append(proxy)
    return output


class UnsupportedOutbound(ConvertError):
    pass


def _check_fields(data: Dict[str, Any], allowed: set) -> None:
    if set(data) - allowed:
        raise UnsupportedOutbound("outbound has unmapped options")


def _duration(value: Any, scale: int = 1) -> int:
    if not isinstance(value, str):
        raise ConvertError("invalid duration")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if not match:
        raise ConvertError("invalid duration")
    result = float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]] * scale
    if not result.is_integer():
        raise UnsupportedOutbound("duration precision cannot be represented")
    return int(result)


def convert_singbox_node(node: Dict[str, Any], index: int) -> Dict[str, Any]:
    ptype = SINGBOX_TYPES[node["type"]]
    fields = {
        "ss": {"method": "cipher", "password": "password"},
        "trojan": {"password": "password"},
        "vless": {"uuid": "uuid", "flow": "flow", "packet_encoding": "packet-encoding"},
        "tuic": {"uuid": "uuid", "password": "password", "congestion_control": "congestion-controller",
                 "udp_relay_mode": "udp-relay-mode", "zero_rtt_handshake": "reduce-rtt"},
        "hysteria2": {"password": "password", "up_mbps": "up", "down_mbps": "down"},
        "anytls": {"password": "password", "min_idle_session": "min-idle-session"},
    }[ptype]
    durations = {
        "anytls": {"idle_session_check_interval": "idle-session-check-interval",
                   "idle_session_timeout": "idle-session-timeout"},
        "tuic": {"heartbeat": "heartbeat-interval"},
    }.get(ptype, {})
    dial_fields = {"bind_interface": ("interface-name", str), "routing_mark": ("routing-mark", int),
                   "tcp_fast_open": ("tfo", bool), "tcp_multi_path": ("mptcp", bool)}
    allowed = {"type", "tag", "server", "server_port", "tls", "multiplex"} | set(fields) | set(durations) | set(dial_fields)
    if ptype in {"vless", "trojan"}:
        allowed.add("transport")
    if ptype == "hysteria2":
        allowed.add("obfs")
    _check_fields(node, allowed)
    server, port = node.get("server"), node.get("server_port")
    if not isinstance(server, str) or not server.strip() or type(port) is not int or not 0 < port < 65536:
        raise ConvertError("missing or invalid endpoint")
    proxy = {"name": node.get("tag") or f"node#{index + 1}", "type": ptype,
             "server": server, "port": port, "udp": True}
    if not isinstance(proxy["name"], str):
        raise ConvertError("invalid node name")
    for source, (target, expected) in dial_fields.items():
        if source in node:
            value = node[source]
            if type(value) is not expected or (expected is int and value < 0):
                raise ConvertError("invalid dial option")
            proxy[target] = value
    for source, target in fields.items():
        if source not in node:
            continue
        value = node[source]
        expected = bool if source == "zero_rtt_handshake" else (
            int if source in {"up_mbps", "down_mbps", "min_idle_session"} else str)
        if type(value) is not expected or (expected is int and value < 0):
            raise ConvertError("invalid outbound option")
        proxy[target] = value
    required = ["uuid"] if ptype == "vless" else ["password"]
    if ptype == "tuic":
        required.append("uuid")
    if ptype == "ss":
        required.append("cipher")
    if any(not proxy.get(key) for key in required):
        raise ConvertError("missing authentication fields")
    for source, target in durations.items():
        if source in node:
            proxy[target] = _duration(node[source], 1000 if ptype == "tuic" else 1)

    tls = node.get("tls", {})
    if not isinstance(tls, dict):
        raise ConvertError("invalid TLS options")
    _check_fields(tls, {"enabled", "server_name", "insecure", "alpn", "utls", "reality"})
    enabled = tls.get("enabled", False)
    if type(enabled) is not bool:
        raise ConvertError("invalid TLS enabled flag")
    if enabled:
        if ptype == "ss":
            raise UnsupportedOutbound("TLS on shadowsocks is not mapped")
        if ptype == "vless":
            proxy["tls"] = True
        for source, target, expected in (
            ("server_name", "servername" if ptype == "vless" else "sni", str),
            ("insecure", "skip-cert-verify", bool), ("alpn", "alpn", list),
        ):
            if source in tls:
                value = tls[source]
                if type(value) is not expected or (expected is list and not all(isinstance(v, str) for v in value)):
                    raise ConvertError("invalid TLS option")
                proxy[target] = value
        for key in ("utls", "reality"):
            option = tls.get(key, {})
            if not isinstance(option, dict) or type(option.get("enabled", False)) is not bool:
                raise ConvertError("invalid TLS extension")
            _check_fields(option, {"enabled", "fingerprint"} if key == "utls" else {"enabled", "public_key", "short_id"})
            if option.get("enabled"):
                if key == "utls":
                    if ptype not in {"vless", "trojan", "anytls"}:
                        raise UnsupportedOutbound("uTLS is not mapped for this protocol")
                    if not isinstance(option.get("fingerprint"), str) or not option["fingerprint"]:
                        raise ConvertError("invalid TLS fingerprint")
                    proxy["client-fingerprint"] = option["fingerprint"]
                else:
                    if ptype != "vless":
                        raise UnsupportedOutbound("Reality is only mapped for vless")
                    if not isinstance(option.get("public_key"), str) or not option["public_key"]:
                        raise ConvertError("invalid Reality public key")
                    if not isinstance(option.get("short_id", ""), str):
                        raise ConvertError("invalid Reality short ID")
                    proxy["reality-opts"] = {"public-key": option["public_key"], "short-id": option.get("short_id", "")}
    elif tls and ptype in {"trojan", "anytls", "tuic", "hysteria2"}:
        raise UnsupportedOutbound("TLS cannot be disabled for this protocol")

    transport = node.get("transport")
    if transport is not None:
        if not isinstance(transport, dict):
            raise ConvertError("invalid transport")
        network = transport.get("type")
        if network == "ws":
            _check_fields(transport, {"type", "path", "headers", "max_early_data", "early_data_header_name"})
            opts = {}
            for source, target, expected in (
                ("path", "path", str), ("headers", "headers", dict),
                ("max_early_data", "max-early-data", int), ("early_data_header_name", "early-data-header-name", str),
            ):
                if source in transport:
                    value = transport[source]
                    if type(value) is not expected:
                        raise ConvertError("invalid WebSocket option")
                    if expected is dict and not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                        raise UnsupportedOutbound("multi-valued WebSocket headers are not mapped")
                    opts[target] = value
            proxy.update({"network": "ws", "ws-opts": opts})
        elif network == "grpc":
            _check_fields(transport, {"type", "service_name"})
            if not isinstance(transport.get("service_name", ""), str):
                raise ConvertError("invalid gRPC service name")
            proxy.update({"network": "grpc", "grpc-opts": {"grpc-service-name": transport.get("service_name", "")}})
        else:
            raise UnsupportedOutbound("transport is not mapped")

    if "obfs" in node:
        obfs = node["obfs"]
        if not isinstance(obfs, dict):
            raise ConvertError("invalid obfuscation options")
        _check_fields(obfs, {"type", "password"})
        if obfs.get("type") != "salamander":
            raise UnsupportedOutbound("obfuscation type is not mapped")
        if not isinstance(obfs.get("password"), str) or not obfs["password"]:
            raise ConvertError("missing obfuscation password")
        proxy.update({"obfs": "salamander", "obfs-password": obfs["password"]})

    if "multiplex" in node:
        mux = node["multiplex"]
        if not isinstance(mux, dict):
            raise ConvertError("invalid multiplex options")
        mux_fields = {"enabled": ("enabled", bool), "protocol": ("protocol", str),
                      "max_connections": ("max-connections", int), "min_streams": ("min-streams", int),
                      "max_streams": ("max-streams", int), "padding": ("padding", bool)}
        _check_fields(mux, set(mux_fields))
        if type(mux.get("enabled", False)) is not bool:
            raise ConvertError("invalid multiplex enabled flag")
        if not mux.get("enabled", False):
            return proxy
        if ptype not in {"ss", "vless", "trojan"}:
            raise UnsupportedOutbound("multiplex is not mapped for this protocol")
        opts = {}
        for source, value in mux.items():
            target, expected = mux_fields[source]
            if type(value) is not expected:
                raise ConvertError("invalid multiplex option")
            opts[target] = value
        proxy["smux"] = opts
    return proxy


def process_singbox_outbounds(outbounds: List[Any], stats: Stats, warn: Callable[[str], None]) -> List[Dict[str, Any]]:
    output = []
    for index, node in enumerate(outbounds):
        stats.input_nodes += 1
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            stats.skipped_malformed += 1
            warn(f"node#{index + 1}: malformed outbound; skipped")
            continue
        if node["type"] in NON_NODE_TYPES:
            stats.skipped_non_node += 1
            continue
        if node["type"] not in SINGBOX_TYPES:
            stats.unknown_type += 1
            stats.skipped_unknown += 1
            warn(f"node#{index + 1}: unknown outbound type; skipped")
            continue
        try:
            output.append(convert_singbox_node(node, index))
        except UnsupportedOutbound:
            stats.skipped_unsupported += 1
            warn(f"node#{index + 1}: unsupported outbound options; skipped")
        except (ConvertError, ValueError, TypeError, OverflowError):
            stats.skipped_malformed += 1
            warn(f"node#{index + 1}: malformed outbound; skipped")
    return output


def convert_content(
    content: str,
    *,
    skip_unknown: bool = False,
    warn: Optional[Callable[[str], None]] = None,
) -> Tuple[List[Dict[str, Any]], Stats]:
    """Convert subscription text into (proxy dicts, Stats)."""
    if warn is None:
        warn = _default_warn
    mode, payload, dropped_non_uri = detect_input(content)
    stats = Stats()
    stats.input_format = mode
    if dropped_non_uri:
        stats.skipped_non_uri = dropped_non_uri
        warn(f"ignored {dropped_non_uri} non-URI line(s) while parsing URI list")
    if mode == "yaml":
        proxies = process_yaml_proxies(payload, stats, warn, skip_unknown=skip_unknown)
    elif mode == "sing-box":
        proxies = process_singbox_outbounds(payload, stats, warn)
    else:
        proxies = process_uris(payload, stats, warn)
    valid = []
    for index, proxy in enumerate(proxies):
        try:
            semantic_key(proxy)
        except ConvertError:
            stats.skipped_malformed += 1
            warn(f"node#{index + 1}: non-serializable node; skipped")
            continue
        valid.append(proxy)
    stats.output_nodes = len(valid)
    return valid, stats


def semantic_key(proxy: Dict[str, Any]) -> str:
    try:
        return json.dumps({k: v for k, v in proxy.items() if k != "name"},
                          sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ConvertError("invalid node structure") from None


def merge_proxies(proxies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output, seen, used_names = [], set(), set()
    reserved = {p["name"] for p in proxies if isinstance(p.get("name"), str)}
    for index, proxy in enumerate(proxies):
        key = semantic_key(proxy)
        if key in seen:
            continue
        seen.add(key)
        name = proxy.get("name")
        if not isinstance(name, str) or not name.strip():
            name = f"node#{index + 1}"
        candidate, suffix = name, 2
        while candidate in used_names or (candidate != name and candidate in reserved):
            candidate = f"{name} [{suffix}]"
            suffix += 1
        used_names.add(candidate)
        output.append({**proxy, "name": candidate})
    return output


def read_sources_env(path: str) -> Dict[str, str]:
    values = {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                key = key.strip()
                if key not in SOURCE_KEYS:
                    continue
                if not sep or key in values:
                    raise ConvertError("invalid or duplicate source setting", EXIT_USAGE)
                value = value.strip()
                if value.startswith(('"', "'")):
                    if len(value) < 2 or value[-1] != value[0]:
                        raise ConvertError("unclosed source setting quote", EXIT_USAGE)
                    value = value[1:-1]
                values[key] = value
    except (OSError, UnicodeError):
        raise ConvertError("cannot read source settings", EXIT_USAGE) from None
    return values


def resolve_min_nodes(value: Any) -> int:
    if value is None:
        return DEFAULT_MIN_NODES
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ConvertError("minimum candidate count must be a positive integer", EXIT_USAGE)
    try:
        return int(value)
    except ValueError:
        raise ConvertError("invalid minimum candidate count", EXIT_USAGE) from None


def collect_sources(settings: Dict[str, str], timeout: int, skip_unknown: bool,
                    warn: Callable[[str], None] = _default_warn) -> Tuple[List[Dict[str, Any]], int]:
    candidates, failed = [], 0
    for index in range(1, 4):
        stats = Stats()
        source_warn = lambda message: warn(f"source#{index}: {message}")
        try:
            url = settings.get(f"SUBSCRIPTION_URL_{index}", "").strip()
            if not url:
                raise ConvertError("source URL is unset", EXIT_USAGE)
            ua = settings.get(f"SUBSCRIPTION_UA_{index}", "").strip() or DEFAULT_USER_AGENT
            content = fetch_subscription(url, timeout, ua)
            nodes, stats = convert_content(content, skip_unknown=skip_unknown, warn=source_warn)
            if not nodes:
                raise ConvertError("source produced no candidates", EXIT_EMPTY)
            candidates.extend(nodes)
        except ConvertError as exc:
            stats.failed = 1
            failed += 1
            source_warn(f"source failed (code={exc.exit_code}); excluded")
        warn(f"source#{index}: {stats.summary()}")
    merged = merge_proxies(candidates)
    warn(f"merged={len(merged)} duplicates={len(candidates) - len(merged)} failed_sources={failed}")
    return merged, failed


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _require_yaml() -> None:
    if not HAS_YAML:
        raise ConvertError(
            "PyYAML is required but not installed; refusing to build YAML by hand. "
            "Install with: pip install pyyaml",
            EXIT_USAGE,
        )


def to_yaml(proxies: List[Dict[str, Any]], as_proxy_provider: bool = True) -> str:
    _require_yaml()
    data = {"proxies": proxies} if as_proxy_provider else proxies
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def merge_into_yaml(proxies: List[Dict[str, Any]], existing_path: str) -> Tuple[str, int]:
    _require_yaml()
    try:
        with open(existing_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError:
        raise ConvertError("cannot read merge target", EXIT_USAGE) from None
    except yaml.YAMLError as exc:
        raise ConvertError(f"merge target is not valid YAML ({type(exc).__name__})", EXIT_BAD_INPUT)

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConvertError("merge target top-level must be a mapping", EXIT_BAD_INPUT)
    existing = data.get("proxies", [])
    if not isinstance(existing, list):
        raise ConvertError("merge target 'proxies' must be a list", EXIT_BAD_INPUT)

    if any(not isinstance(p, dict) for p in existing):
        raise ConvertError("merge target contains a non-mapping node")
    existing_keys = {semantic_key(p) for p in existing}
    merged = merge_proxies(existing + proxies)
    added = sum(semantic_key(p) not in existing_keys for p in merged)
    data["proxies"] = merged
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False), added


def write_output_safely(path: str, text: str) -> None:
    """Atomically write `text` to `path`; a failure leaves `path` untouched."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".convert_sub.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def build_request(url: str, user_agent: str) -> urllib.request.Request:
    """Build a request, converting an invalid URL into a redacted error.

    `urllib` raises ``ValueError`` (whose text embeds the raw URL) for control
    characters or otherwise unparseable input, so the exception text is never
    echoed here — only the exception type is kept.
    """
    try:
        if urllib.parse.urlsplit(url).scheme.lower() not in {"https", "http"}:
            raise ValueError
        return urllib.request.Request(url, headers={"User-Agent": user_agent})
    except Exception as exc:
        raise ConvertError(
            f"invalid subscription URL (URL redacted): {type(exc).__name__}",
            EXIT_USAGE,
        ) from None


def _safe_http_status(code: Any) -> str:
    """Return an HTTP status for printing, or a constant if it is not numeric."""
    return str(code) if isinstance(code, int) else "unknown"


def fetch_subscription(url: str, timeout: int, user_agent: str) -> str:
    """Fetch a subscription. Never leaks the URL or credentials on failure."""
    request = build_request(url, user_agent)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Only the numeric status is reported: the HTTP reason phrase is
        # upstream-controlled and may itself contain a token or URL.
        raise ConvertError(
            f"HTTP error {_safe_http_status(exc.code)} while fetching subscription "
            "(URL redacted)",
            EXIT_NETWORK,
        )
    except urllib.error.URLError as exc:
        # The upstream reason is attacker-controlled; report only the type.
        raise ConvertError(
            "network error while fetching subscription "
            f"(URL redacted; {type(exc).__name__})",
            EXIT_NETWORK,
        )
    except Exception as exc:  # pragma: no cover - defensive, keep message URL-free
        raise ConvertError(
            f"failed to fetch subscription (URL redacted): {type(exc).__name__}",
            EXIT_NETWORK,
        )
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_url(args: argparse.Namespace) -> str:
    if args.url_env:
        value = os.environ.get(args.url_env, "")
        if not value.strip():
            raise ConvertError("subscription URL environment variable is unset or empty", EXIT_USAGE)
        return value.strip()
    if args.url_stdin:
        value = sys.stdin.read().strip()
        if not value:
            raise ConvertError("no URL received on stdin", EXIT_USAGE)
        return value
    return args.url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Clash YAML, sing-box JSON or URI subscriptions into a mihomo candidate pool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python convert_sub.py --url-env SUB_URL -o providers/subscription.yaml
  python convert_sub.py -i subscription_raw.txt --list
  python convert_sub.py --merge providers/subscription.yaml -o out.yaml -i raw.txt
  python convert_sub.py --uri "anytls://pw@host:443?sni=host#node" --list
""",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-u", "--url", help="Subscription URL (never printed in full)")
    group.add_argument("--url-env", metavar="VAR", help="Read subscription URL from environment variable")
    group.add_argument("--url-stdin", action="store_true", help="Read subscription URL from stdin")
    group.add_argument("-i", "--input", help="Input subscription file")
    group.add_argument("--uri", help="Single share URI")
    group.add_argument("--sources-env-file", metavar="PATH", help="Read three sources and per-source UAs from a local env file")
    group.add_argument("--sources-env", action="store_true", help="Read three sources from process environment variables")

    parser.add_argument("--min-nodes", help="Positive minimum merged candidates (default: 1; multi-source uses MIN_CANDIDATE_NODES)")
    parser.add_argument("-o", "--output", help="Output YAML file path")
    parser.add_argument("--merge", help="Merge into an existing YAML config file")
    parser.add_argument("--list", action="store_true", help="Preview only node ordinals and safe types; never write output")
    parser.add_argument("--suffix", default="", help="Suffix appended to node names")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    parser.add_argument("--user-agent", default=None,
                        help=f"HTTP User-Agent (default: {DEFAULT_USER_AGENT})")
    parser.add_argument("--skip-unknown", action="store_true",
                        help="Explicitly drop nodes whose type is not recognised (counted)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        _require_yaml()
        multi_source = args.sources_env_file is not None or args.sources_env
        if multi_source and not args.output and not args.list:
            raise ConvertError("multi-source mode requires --output or --list", EXIT_USAGE)
        if multi_source and args.user_agent:
            raise ConvertError("use per-source SUBSCRIPTION_UA_1/2/3 in multi-source mode", EXIT_USAGE)
        settings = read_sources_env(args.sources_env_file) if args.sources_env_file else (
            {key: os.environ[key] for key in SOURCE_KEYS if key in os.environ} if args.sources_env else {})
        minimum = resolve_min_nodes(args.min_nodes if args.min_nodes is not None else settings.get("MIN_CANDIDATE_NODES"))
        failed_sources = 0
        if multi_source:
            proxies, failed_sources = collect_sources(settings, args.timeout, args.skip_unknown)
        else:
            if args.url or args.url_env or args.url_stdin:
                url = _resolve_url(args)
                if not args.quiet:
                    print("Fetching subscription (URL redacted)", file=sys.stderr)
                content = fetch_subscription(url, args.timeout, resolve_user_agent(args))
            elif args.input:
                if not args.quiet:
                    print("Reading subscription file (path redacted)", file=sys.stderr)
                try:
                    with open(args.input, "r", encoding="utf-8-sig", errors="replace") as handle:
                        content = handle.read()
                except OSError:
                    raise ConvertError("cannot read input file", EXIT_USAGE) from None
            else:
                content = args.uri or ""
            proxies, stats = convert_content(content, skip_unknown=args.skip_unknown)
            if not args.quiet:
                print(f"stats: {stats.summary()}", file=sys.stderr)
            original_count = len(proxies)
            proxies = merge_proxies(proxies)
            if not args.quiet:
                print(f"merged={len(proxies)} duplicates={original_count - len(proxies)}", file=sys.stderr)

        if len(proxies) < minimum:
            raise ConvertError(
                f"candidate count {len(proxies)} below minimum {minimum}; output not written", EXIT_EMPTY)
        if args.suffix:
            proxies = [{**proxy, "name": proxy["name"] + args.suffix} for proxy in proxies]

        if args.list:
            for index, proxy in enumerate(proxies):
                print(f"node#{index + 1} [{_safe_type(proxy.get('type'))}]")
        else:
            if args.merge:
                output, added = merge_into_yaml(proxies, args.merge)
                if not args.quiet:
                    print(f"merged {added} new candidate(s)", file=sys.stderr)
            else:
                output = to_yaml(proxies, as_proxy_provider=True)
            if args.output:
                write_output_safely(args.output, output)
                if not args.quiet:
                    print(f"wrote candidate pool ({len(proxies)} incoming candidates; path redacted)", file=sys.stderr)
            else:
                sys.stdout.write(output)
        if failed_sources:
            _default_warn("partial source failure; candidate pool accepted, not a fully successful refresh (exit=5)")
            return EXIT_PARTIAL
        return EXIT_OK
    except ConvertError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        print(f"error: conversion or output failed ({type(exc).__name__}); output not replaced", file=sys.stderr)
        return EXIT_BAD_INPUT


def resolve_user_agent(args: argparse.Namespace) -> str:
    if args.user_agent:
        return args.user_agent
    env_ua = os.environ.get("CONVERT_SUB_UA")
    if env_ua and env_ua.strip():
        return env_ua.strip()
    return DEFAULT_USER_AGENT


if __name__ == "__main__":
    sys.exit(main())
