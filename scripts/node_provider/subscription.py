"""Subscription fetching and parsing (self-contained copy/rewrite).

Moved here from node_ops in S3: node extraction belongs to the provider, and
node_ops is now a pure probe/measurement consumer of the Feishu table.

Three input families are auto-detected exactly like the existing converter:
  1. Clash / mihomo YAML
  2. sing-box JSON
  3. base64-encoded or plain-text URI lists (anytls, ss, trojan, vless, tuic,
     hysteria2/hy2)

Semantic de-duplication ignores the node ``name`` only: two nodes with the same
server/port/credentials/transport are duplicates.  Names are then made unique.

Security: subscription URLs and credentials are never printed.  Fetch errors
carry only a class name, an HTTP numeric status, or a fixed string.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

DEFAULT_USER_AGENT = "ClashMeta/1.18.0"

KNOWN_PROXY_TYPES = {
    "ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2",
    "tuic", "anytls", "snell", "http", "socks5",
}
URI_SCHEMES = {
    "anytls": "anytls", "ss": "ss", "trojan": "trojan", "vless": "vless",
    "tuic": "tuic", "hysteria2": "hysteria2", "hy2": "hysteria2",
}
SINGBOX_TYPES = {
    "shadowsocks": "ss", "trojan": "trojan", "vless": "vless",
    "tuic": "tuic", "hysteria2": "hysteria2", "anytls": "anytls",
}
NON_NODE_TYPES = {"selector", "urltest", "direct", "block", "dns"}


class SubscriptionError(Exception):
    def __init__(self, message: str, kind: str = "parse") -> None:
        super().__init__(message)
        self.kind = kind


def _default_warn(message: str) -> None:
    import sys
    print(f"warning: {message}", file=sys.stderr)


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _p(params: Dict[str, List[str]], key: str, default: Optional[str] = None) -> Optional[str]:
    values = params.get(key)
    return values[0] if values else default


def _safe_type(value: Any) -> str:
    allowed = KNOWN_PROXY_TYPES | set(URI_SCHEMES) | set(SINGBOX_TYPES) | NON_NODE_TYPES
    return value if isinstance(value, str) and value in allowed else "<unknown>"


def _split_host_port(hostpart: str) -> Tuple[str, Optional[int]]:
    hostpart = hostpart.strip()
    if not hostpart:
        raise SubscriptionError("missing host in URI")
    if hostpart.startswith("["):
        end = hostpart.find("]")
        if end == -1:
            raise SubscriptionError("malformed IPv6 host in URI")
        host = hostpart[1:end]
        rest = hostpart[end + 1:]
        if rest.startswith(":"):
            port_str = rest[1:]
        elif rest == "":
            port_str = ""
        else:
            raise SubscriptionError("malformed IPv6 host in URI")
    else:
        if ":" in hostpart:
            host, port_str = hostpart.rsplit(":", 1)
        else:
            host, port_str = hostpart, ""
    host = urllib.parse.unquote(host)
    if not host:
        raise SubscriptionError("missing host in URI")
    port: Optional[int] = None
    if port_str:
        try:
            port = int(port_str)
        except ValueError:
            raise SubscriptionError("invalid port in URI")
        if not 0 < port < 65536:
            raise SubscriptionError("port out of range in URI")
    return host, port


def _split_uri(uri: str) -> Tuple[str, str, str]:
    uri = uri.strip()
    if "://" not in uri:
        raise SubscriptionError("not a valid URI (missing scheme)")
    if "#" in uri:
        base, frag = uri.split("#", 1)
        name = urllib.parse.unquote(frag)
    else:
        base, name = uri, ""
    scheme, rest = base.split("://", 1)
    return scheme.lower(), rest, name


def _split_authority(rest: str) -> Dict[str, Any]:
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
    return [token.strip() for token in raw.split(",") if token.strip()] if raw else []


def _apply_ws_opts(proxy: Dict[str, Any], params: Dict[str, List[str]], path: str) -> None:
    ws_path = _p(params, "path") or path or "/"
    ws_host = _p(params, "host") or proxy["server"]
    proxy["ws-opts"] = {"path": ws_path, "headers": {"Host": ws_host}}


def _apply_grpc_opts(proxy: Dict[str, Any], params: Dict[str, List[str]]) -> None:
    service = _p(params, "serviceName") or _p(params, "servicename") or ""
    proxy["grpc-opts"] = {"grpc-service-name": service}


def parse_anytls_uri(uri: str) -> Dict[str, Any]:
    _, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    password = parts["userinfo"]
    if not password:
        raise SubscriptionError("anytls URI is missing a password")
    port = parts["port"] or 443
    params = parts["params"]
    proxy: Dict[str, Any] = {
        "name": name or f"anytls-{parts['host']}:{port}",
        "type": "anytls", "server": parts["host"], "port": port,
        "password": password, "udp": True,
    }
    proxy["sni"] = _p(params, "sni") or _p(params, "peer") or parts["host"]
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


def _b64decode(text: str) -> str:
    compact = "".join(text.split())
    if not compact:
        raise SubscriptionError("empty base64 payload")
    padded = compact + "=" * ((4 - len(compact) % 4) % 4)
    last: Optional[Exception] = None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError) as exc:
            last = exc
            continue
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            last = exc
    raise SubscriptionError("invalid base64 payload") from last


def _decode_ss_userinfo(userinfo: str) -> Tuple[str, str]:
    if ":" in userinfo:
        method, _, password = userinfo.partition(":")
        return method, password
    decoded = _b64decode(userinfo)
    method, _, password = decoded.partition(":")
    if not method:
        raise SubscriptionError("ss URI has a malformed userinfo")
    return method, password


def parse_ss_uri(uri: str) -> Dict[str, Any]:
    _, rest, name = _split_uri(uri)
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    params = urllib.parse.parse_qs(query, keep_blank_values=True) if query else {}
    if "@" in rest:
        userinfo, hostpart = rest.rsplit("@", 1)
        method, password = _decode_ss_userinfo(urllib.parse.unquote(userinfo))
    else:
        decoded = _b64decode(rest)
        if "@" not in decoded:
            raise SubscriptionError("ss URI has a malformed payload")
        creds, hostpart = decoded.rsplit("@", 1)
        method, _, password = creds.partition(":")
    hostpart = hostpart.split("/", 1)[0]
    host, port = _split_host_port(hostpart)
    if port is None:
        raise SubscriptionError("ss URI is missing a port")
    proxy: Dict[str, Any] = {
        "name": name or f"ss-{host}:{port}", "type": "ss",
        "server": host, "port": port, "cipher": method,
        "password": password, "udp": True,
    }
    plugin = _p(params, "plugin")
    if plugin:
        proxy["plugin"] = plugin
        plugin_opts = _p(params, "plugin-opts") or _p(params, "plugin_opts")
        if plugin_opts:
            proxy["plugin_opts"] = plugin_opts
    return proxy


def parse_trojan_uri(uri: str) -> Dict[str, Any]:
    _, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    password = parts["userinfo"]
    if not password:
        raise SubscriptionError("trojan URI is missing a password")
    port = parts["port"] or 443
    params = parts["params"]
    proxy: Dict[str, Any] = {
        "name": name or f"trojan-{parts['host']}:{port}", "type": "trojan",
        "server": parts["host"], "port": port, "password": password, "udp": True,
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
    _, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    uuid = parts["userinfo"]
    if not uuid:
        raise SubscriptionError("vless URI is missing a uuid")
    port = parts["port"] or 443
    params = parts["params"]
    network = _p(params, "type") or "tcp"
    proxy: Dict[str, Any] = {
        "name": name or f"vless-{parts['host']}:{port}", "type": "vless",
        "server": parts["host"], "port": port, "uuid": uuid,
        "network": network, "udp": True,
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
    _, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    userinfo = parts["userinfo"]
    if ":" not in userinfo:
        raise SubscriptionError("tuic URI is missing uuid:password")
    uuid, _, password = userinfo.partition(":")
    if not uuid or not password:
        raise SubscriptionError("tuic URI is missing uuid or password")
    port = parts["port"] or 443
    params = parts["params"]
    proxy: Dict[str, Any] = {
        "name": name or f"tuic-{parts['host']}:{port}", "type": "tuic",
        "server": parts["host"], "port": port, "uuid": uuid,
        "password": password, "udp": True,
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
    _, rest, name = _split_uri(uri)
    parts = _split_authority(rest)
    port = parts["port"] or 443
    params = parts["params"]
    proxy: Dict[str, Any] = {
        "name": name or f"hysteria2-{parts['host']}:{port}", "type": "hysteria2",
        "server": parts["host"], "port": port, "password": parts["userinfo"], "udp": True,
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
    if _truthy(_p(params, "fast-open")):
        proxy["fast-open"] = True
    up, down = _p(params, "up"), _p(params, "down")
    if up and down:
        try:
            proxy["up"] = int(up)
            proxy["down"] = int(down)
        except ValueError:
            pass
    return proxy


_URI_PARSERS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "anytls": parse_anytls_uri, "ss": parse_ss_uri, "trojan": parse_trojan_uri,
    "vless": parse_vless_uri, "tuic": parse_tuic_uri, "hysteria2": parse_hysteria2_uri,
}


def _looks_like_base64(text: str) -> bool:
    compact = "".join(text.split())
    return bool(compact) and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact) is not None


def _split_uri_lines(text: str) -> Tuple[List[str], int]:
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
        raise SubscriptionError("invalid top-level: expected a YAML mapping")
    if "proxies" not in data:
        raise SubscriptionError("invalid top-level: YAML mapping has no 'proxies' key")
    proxies = data["proxies"]
    if proxies is None:
        raise SubscriptionError("invalid top-level: 'proxies' is empty")
    if not isinstance(proxies, list):
        raise SubscriptionError("invalid top-level: 'proxies' must be a list")
    return proxies


def detect_input(content: str) -> Tuple[str, Any, int]:
    content = content.lstrip("\ufeff").strip()
    if not content:
        raise SubscriptionError("empty input", "empty")
    if re.match(r'^\{\s*"', content):
        try:
            data = json.loads(content)
        except (ValueError, RecursionError):
            raise SubscriptionError("malformed JSON input") from None
        if "outbounds" in data:
            if not isinstance(data["outbounds"], list):
                raise SubscriptionError("JSON 'outbounds' must be a list")
            return "sing-box", data["outbounds"], 0
        return "yaml", _extract_proxies(data), 0
    if yaml is not None:
        try:
            data = yaml.safe_load(content)
        except Exception as exc:
            uris, dropped = _split_uri_lines(content)
            if uris and dropped == 0:
                return "uris", uris, 0
            raise SubscriptionError(
                f"malformed YAML input ({type(exc).__name__}); refusing URI fallback"
            )
        if isinstance(data, dict):
            return "yaml", _extract_proxies(data), 0
        if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
            return "yaml", data, 0
    if _looks_like_base64(content):
        try:
            decoded = _b64decode(content)
        except SubscriptionError:
            decoded = ""
        if "://" in decoded:
            uris, dropped = _split_uri_lines(decoded)
            if uris:
                return "uris", uris, dropped
    uris, dropped = _split_uri_lines(content)
    if uris:
        return "uris", uris, dropped
    raise SubscriptionError(
        "unrecognised input: not Clash YAML, sing-box JSON, base64 subscription, or URI list"
    )


def process_yaml_proxies(proxies: List[Any], warn: Callable[[str], None]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for index, node in enumerate(proxies):
        if not isinstance(node, dict):
            warn(f"malformed node #{index + 1}: not a mapping; skipped")
            continue
        ptype = node.get("type")
        if not isinstance(ptype, str) or not ptype:
            warn(f"malformed node #{index + 1}: missing type; skipped")
            continue
        if ptype not in KNOWN_PROXY_TYPES:
            warn(f"unknown proxy type '{_safe_type(ptype)}' in node #{index + 1}")
        output.append(node)
    return output


def process_uris(uris: List[str], warn: Callable[[str], None]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for index, uri in enumerate(uris):
        scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
        parser = _URI_PARSERS.get(URI_SCHEMES.get(scheme, ""))
        if parser is None:
            warn(f"unsupported URI scheme '{_safe_type(scheme)}' (node #{index + 1}); skipped")
            continue
        try:
            output.append(parser(uri))
        except SubscriptionError as exc:
            warn(f"malformed {_safe_type(scheme)} URI (node #{index + 1}): {exc}")
        except Exception as exc:  # pragma: no cover
            warn(f"failed to parse URI (node #{index + 1}): {type(exc).__name__}")
    return output


def _duration(value: Any, scale: int = 1) -> int:
    if not isinstance(value, str):
        raise SubscriptionError("invalid duration")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if not match:
        raise SubscriptionError("invalid duration")
    result = float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]] * scale
    if not result.is_integer():
        raise SubscriptionError("duration precision cannot be represented")
    return int(result)


def _check_fields(data: Dict[str, Any], allowed: set) -> None:
    if set(data) - allowed:
        raise SubscriptionError("outbound has unmapped options")


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
    allowed = {"type", "tag", "server", "server_port", "tls", "multiplex"} | set(fields) \
        | set(durations) | set(dial_fields)
    if ptype in {"vless", "trojan"}:
        allowed.add("transport")
    if ptype == "hysteria2":
        allowed.add("obfs")
    _check_fields(node, allowed)
    server, port = node.get("server"), node.get("server_port")
    if not isinstance(server, str) or not server.strip() or type(port) is not int or not 0 < port < 65536:
        raise SubscriptionError("missing or invalid endpoint")
    proxy: Dict[str, Any] = {"name": node.get("tag") or f"node#{index + 1}", "type": ptype,
                             "server": server, "port": port, "udp": True}
    if not isinstance(proxy["name"], str):
        raise SubscriptionError("invalid node name")
    for source, (target, expected) in dial_fields.items():
        if source in node:
            value = node[source]
            if type(value) is not expected or (expected is int and value < 0):
                raise SubscriptionError("invalid dial option")
            proxy[target] = value
    for source, target in fields.items():
        if source not in node:
            continue
        value = node[source]
        expected = bool if source == "zero_rtt_handshake" else (
            int if source in {"up_mbps", "down_mbps", "min_idle_session"} else str)
        if type(value) is not expected or (expected is int and value < 0):
            raise SubscriptionError("invalid outbound option")
        proxy[target] = value
    required = ["uuid"] if ptype == "vless" else ["password"]
    if ptype == "tuic":
        required.append("uuid")
    if ptype == "ss":
        required.append("cipher")
    if any(not proxy.get(key) for key in required):
        raise SubscriptionError("missing authentication fields")
    for source, target in durations.items():
        if source in node:
            proxy[target] = _duration(node[source], 1000 if ptype == "tuic" else 1)

    tls = node.get("tls", {})
    if not isinstance(tls, dict):
        raise SubscriptionError("invalid TLS options")
    _check_fields(tls, {"enabled", "server_name", "insecure", "alpn", "utls", "reality"})
    enabled = tls.get("enabled", False)
    if type(enabled) is not bool:
        raise SubscriptionError("invalid TLS enabled flag")
    if enabled:
        if ptype == "ss":
            raise SubscriptionError("TLS on shadowsocks is not mapped")
        if ptype == "vless":
            proxy["tls"] = True
        for source, target, expected in (
            ("server_name", "servername" if ptype == "vless" else "sni", str),
            ("insecure", "skip-cert-verify", bool), ("alpn", "alpn", list),
        ):
            if source in tls:
                value = tls[source]
                if type(value) is not expected or (
                        expected is list and not all(isinstance(v, str) for v in value)):
                    raise SubscriptionError("invalid TLS option")
                proxy[target] = value
        for key in ("utls", "reality"):
            option = tls.get(key, {})
            if not isinstance(option, dict) or type(option.get("enabled", False)) is not bool:
                raise SubscriptionError("invalid TLS extension")
            _check_fields(option, {"enabled", "fingerprint"} if key == "utls"
                          else {"enabled", "public_key", "short_id"})
            if option.get("enabled"):
                if key == "utls":
                    if ptype not in {"vless", "trojan", "anytls"}:
                        raise SubscriptionError("uTLS is not mapped for this protocol")
                    if not isinstance(option.get("fingerprint"), str) or not option["fingerprint"]:
                        raise SubscriptionError("invalid TLS fingerprint")
                    proxy["client-fingerprint"] = option["fingerprint"]
                else:
                    if ptype != "vless":
                        raise SubscriptionError("Reality is only mapped for vless")
                    if not isinstance(option.get("public_key"), str) or not option["public_key"]:
                        raise SubscriptionError("invalid Reality public key")
                    if not isinstance(option.get("short_id", ""), str):
                        raise SubscriptionError("invalid Reality short ID")
                    proxy["reality-opts"] = {
                        "public-key": option["public_key"],
                        "short-id": option.get("short_id", ""),
                    }
    elif tls and ptype in {"trojan", "anytls", "tuic", "hysteria2"}:
        raise SubscriptionError("TLS cannot be disabled for this protocol")

    transport = node.get("transport")
    if transport is not None:
        if not isinstance(transport, dict):
            raise SubscriptionError("invalid transport")
        network = transport.get("type")
        if network == "ws":
            _check_fields(transport, {"type", "path", "headers", "max_early_data",
                                      "early_data_header_name"})
            opts: Dict[str, Any] = {}
            for source, target, expected in (
                ("path", "path", str), ("headers", "headers", dict),
                ("max_early_data", "max-early-data", int),
                ("early_data_header_name", "early-data-header-name", str),
            ):
                if source in transport:
                    value = transport[source]
                    if type(value) is not expected:
                        raise SubscriptionError("invalid WebSocket option")
                    opts[target] = value
            proxy.update({"network": "ws", "ws-opts": opts})
        elif network == "grpc":
            _check_fields(transport, {"type", "service_name"})
            if not isinstance(transport.get("service_name", ""), str):
                raise SubscriptionError("invalid gRPC service name")
            proxy.update({"network": "grpc", "grpc-opts": {
                "grpc-service-name": transport.get("service_name", "")}})
        else:
            raise SubscriptionError("transport is not mapped")

    if "obfs" in node:
        obfs = node["obfs"]
        if not isinstance(obfs, dict):
            raise SubscriptionError("invalid obfuscation options")
        _check_fields(obfs, {"type", "password"})
        if obfs.get("type") != "salamander":
            raise SubscriptionError("obfuscation type is not mapped")
        if not isinstance(obfs.get("password"), str) or not obfs["password"]:
            raise SubscriptionError("missing obfuscation password")
        proxy.update({"obfs": "salamander", "obfs-password": obfs["password"]})

    if "multiplex" in node:
        mux = node["multiplex"]
        if not isinstance(mux, dict):
            raise SubscriptionError("invalid multiplex options")
        mux_fields = {"enabled": ("enabled", bool), "protocol": ("protocol", str),
                      "max_connections": ("max-connections", int),
                      "min_streams": ("min-streams", int),
                      "max_streams": ("max-streams", int), "padding": ("padding", bool)}
        _check_fields(mux, set(mux_fields))
        if type(mux.get("enabled", False)) is not bool:
            raise SubscriptionError("invalid multiplex enabled flag")
        if not mux.get("enabled", False):
            return proxy
        if ptype not in {"ss", "vless", "trojan"}:
            raise SubscriptionError("multiplex is not mapped for this protocol")
        opts = {}
        for source, value in mux.items():
            target, expected = mux_fields[source]
            if type(value) is not expected:
                raise SubscriptionError("invalid multiplex option")
            opts[target] = value
        proxy["smux"] = opts
    return proxy


def process_singbox_outbounds(outbounds: List[Any], warn: Callable[[str], None]) -> List[Dict[str, Any]]:
    output = []
    for index, node in enumerate(outbounds):
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            warn(f"node#{index + 1}: malformed outbound; skipped")
            continue
        if node["type"] in NON_NODE_TYPES:
            continue
        if node["type"] not in SINGBOX_TYPES:
            warn(f"node#{index + 1}: unknown outbound type; skipped")
            continue
        try:
            output.append(convert_singbox_node(node, index))
        except (SubscriptionError, ValueError, TypeError, OverflowError) as exc:
            warn(f"node#{index + 1}: outbound skipped ({type(exc).__name__})")
    return output


def semantic_key(proxy: Dict[str, Any]) -> str:
    try:
        return json.dumps({k: v for k, v in proxy.items() if k != "name"},
                          sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise SubscriptionError("invalid node structure") from None


def convert_content(content: str, warn: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    warn = warn or _default_warn
    mode, payload, dropped = detect_input(content)
    if dropped:
        warn(f"ignored {dropped} non-URI line(s) while parsing URI list")
    if mode == "yaml":
        proxies = process_yaml_proxies(payload, warn)
    elif mode == "sing-box":
        proxies = process_singbox_outbounds(payload, warn)
    else:
        proxies = process_uris(payload, warn)
    valid = []
    for index, proxy in enumerate(proxies):
        try:
            semantic_key(proxy)
        except SubscriptionError:
            warn(f"node#{index + 1}: non-serializable node; skipped")
            continue
        valid.append(proxy)
    return valid


def dedupe_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop semantic duplicates (name ignored) and make every name unique."""
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    used_names: set[str] = set()
    reserved = {n["name"] for n in nodes if isinstance(n.get("name"), str)}
    for index, node in enumerate(nodes):
        key = semantic_key(node)
        if key in seen:
            continue
        seen.add(key)
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            name = f"node#{index + 1}"
        candidate, suffix = name, 2
        while candidate in used_names or (candidate != name and candidate in reserved):
            candidate = f"{name} [{suffix}]"
            suffix += 1
        used_names.add(candidate)
        output.append({**node, "name": candidate})
    return output


# ---------------------------------------------------------------------------
# Source settings and HTTP
# ---------------------------------------------------------------------------

def source_settings(values: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """Extract SUBSCRIPTION_URL_n / SUBSCRIPTION_UA_n for n=1..4."""
    settings: Dict[str, Dict[str, str]] = {}
    for index in range(1, 5):
        url = (values.get(f"SUBSCRIPTION_URL_{index}") or "").strip()
        ua = (values.get(f"SUBSCRIPTION_UA_{index}") or "").strip() or DEFAULT_USER_AGENT
        if url:
            settings[str(index)] = {"url": url, "ua": ua}
    return settings


def build_request(url: str, user_agent: str) -> urllib.request.Request:
    try:
        if urllib.parse.urlsplit(url).scheme.lower() not in {"https", "http"}:
            raise ValueError
        return urllib.request.Request(url, headers={"User-Agent": user_agent})
    except Exception as exc:
        raise SubscriptionError(
            f"invalid subscription URL (URL redacted): {type(exc).__name__}", "network"
        ) from None


DEFAULT_FETCH_PROXY = "http://127.0.0.1:7890"


def fetch_subscription(url: str, user_agent: str, timeout: int = 30,
                       fetch_proxy: str = DEFAULT_FETCH_PROXY) -> str:
    request = build_request(url, user_agent)
    proxy = (fetch_proxy or "").strip()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        open_url = opener.open
    else:
        open_url = urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SubscriptionError(
            f"HTTP error {exc.code if isinstance(exc.code, int) else 'unknown'} "
            "(URL redacted)",
            "http",
        ) from None
    except urllib.error.URLError as exc:
        raise SubscriptionError(
            f"network error (URL redacted; {type(exc).__name__})", "network"
        ) from None
    except Exception as exc:  # pragma: no cover
        raise SubscriptionError(
            f"failed to fetch subscription (URL redacted): {type(exc).__name__}", "network"
        ) from None
    return raw.decode("utf-8", errors="replace")
