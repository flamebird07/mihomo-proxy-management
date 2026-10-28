"""Tests for scripts/convert_sub.py.

Focus areas (kept deliberately small, one test per contract point):
  * 56-node synthetic YAML: 52 anytls preserved verbatim + mixed known types
  * AnyTLS URI mapping (password / SNI / TLS, percent-encoding, IPv6)
  * unknown types are warned without leaking secrets and counted
  * malformed / invalid-top-level YAML diagnostics
  * default and overridable User-Agent
  * HTTP errors never leak the URL or credentials
  * missing PyYAML fails closed
  * a failing run never overwrites an existing output file
  * merge de-duplicates by full semantics and retains distinct same-name nodes
  * URI regression for ss / trojan / vless / tuic / hysteria2 / base64
  * output-safety regressions: upstream HTTP reason / URLError reason / raw
    port must never leak a credential, and invalid URLs must not traceback
  * input-integrity regressions: a malformed Clash YAML containing URLs fails
    closed, only a pure URI list falls back, non-URI lines are counted
"""

import base64
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "convert_sub.py")

spec = importlib.util.spec_from_file_location("convert_sub", SCRIPT)
convert_sub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(convert_sub)

import yaml  # noqa: E402  (after module import is fine)


def setUpModule():
    guard = patch.object(convert_sub.urllib.request, "urlopen", side_effect=AssertionError("network disabled in tests"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)


def anytls_node(i: int):
    return {
        "name": f"anytls-{i:02d}",
        "type": "anytls",
        "server": f"10.0.0.{i % 250 + 1}",
        "port": 443,
        "password": f"pw-{i}",
        "sni": "example.com",
        "client-fingerprint": "chrome",
        "alpn": ["h2", "http/1.1"],
        "udp": True,
        "smux": {"enabled": True},
    }


def synthetic_56():
    nodes = [anytls_node(i) for i in range(52)]
    nodes.append({
        "name": "vless-1", "type": "vless", "server": "1.1.1.1", "port": 443,
        "uuid": "u-1", "tls": True, "servername": "s.com", "network": "ws",
        "ws-opts": {"path": "/ws", "headers": {"Host": "s.com"}},
    })
    nodes.append({
        "name": "trojan-1", "type": "trojan", "server": "2.2.2.2", "port": 443,
        "password": "tj1", "sni": "t.com",
        "grpc-opts": {"grpc-service-name": "g"},
    })
    nodes.append({
        "name": "trojan-2", "type": "trojan", "server": "2.2.2.3", "port": 8443,
        "password": "tj2", "sni": "t.com",
    })
    nodes.append({
        "name": "ss-1", "type": "ss", "server": "3.3.3.3", "port": 8388,
        "cipher": "aes-256-gcm", "password": "ss1", "udp": True,
    })
    return nodes


class YamlPassthroughTests(unittest.TestCase):
    def test_56_nodes_preserved(self):
        nodes = synthetic_56()
        content = yaml.safe_dump({"proxies": nodes}, sort_keys=False, allow_unicode=True)
        out, stats = convert_sub.convert_content(content, warn=lambda _m: None)

        self.assertEqual(len(out), 56)
        self.assertEqual(stats.input_nodes, 56)
        self.assertEqual(stats.output_nodes, 56)
        self.assertEqual(stats.skipped_malformed, 0)
        self.assertEqual(stats.unknown_type, 0)

        anytls_out = [p for p in out if p["type"] == "anytls"]
        self.assertEqual(len(anytls_out), 52)
        # type and password unchanged, full nesting preserved
        for original, converted in zip([n for n in nodes if n["type"] == "anytls"], anytls_out):
            self.assertEqual(converted, original)
            self.assertEqual(converted["type"], "anytls")
            self.assertEqual(converted["password"], original["password"])
            self.assertEqual(converted["smux"], {"enabled": True})

        dumped = convert_sub.to_yaml(out)
        reloaded = yaml.safe_load(dumped)
        self.assertEqual(len(reloaded["proxies"]), 56)
        self.assertEqual(
            len([p for p in reloaded["proxies"] if p["type"] == "anytls"]), 52
        )

    def test_unknown_type_warned_and_counted_without_leak(self):
        nodes = [{
            "name": "mystery", "type": "wormhole", "server": "9.9.9.9", "port": 1,
            "password": "TOP-SECRET-VALUE",
        }]
        warns = []
        content = yaml.safe_dump({"proxies": nodes}, sort_keys=False)
        out, stats = convert_sub.convert_content(content, warn=warns.append)

        self.assertEqual(stats.unknown_type, 1)
        self.assertEqual(len(out), 1)  # kept by default, not silently dropped
        joined = "\n".join(warns)
        self.assertIn("<unknown>", joined)
        self.assertIn("node#1", joined)
        self.assertNotIn("wormhole", joined)
        self.assertNotIn("mystery", joined)
        self.assertNotIn("TOP-SECRET-VALUE", joined)

    def test_skip_unknown_is_explicit(self):
        nodes = [{"name": "mystery", "type": "wormhole", "server": "9.9.9.9", "port": 1}]
        content = yaml.safe_dump({"proxies": nodes}, sort_keys=False)
        out, stats = convert_sub.convert_content(
            content, skip_unknown=True, warn=lambda _m: None
        )
        self.assertEqual(len(out), 0)
        self.assertEqual(stats.unknown_type, 1)
        self.assertEqual(stats.skipped_unknown, 1)

    def test_malformed_node_skipped_with_warning(self):
        nodes = [
            {"name": "ok", "type": "ss", "server": "1.1.1.1", "port": 1,
             "cipher": "aes-128-gcm", "password": "x"},
            {"name": "no-type", "server": "1.1.1.1", "port": 2},
            "not-a-mapping",
        ]
        content = yaml.safe_dump({"proxies": nodes}, sort_keys=False)
        warns = []
        out, stats = convert_sub.convert_content(content, warn=warns.append)
        self.assertEqual(len(out), 1)
        self.assertEqual(stats.skipped_malformed, 2)
        self.assertTrue(any("malformed" in w for w in warns))

    def test_invalid_top_level(self):
        with self.assertRaises(convert_sub.ConvertError):
            convert_sub.detect_input("foo: bar")
        with self.assertRaises(convert_sub.ConvertError):
            convert_sub.detect_input("proxies: notalist")
        with self.assertRaises(convert_sub.ConvertError):
            convert_sub.detect_input("proxies: [a, b")  # malformed YAML
        with self.assertRaises(convert_sub.ConvertError):
            convert_sub.detect_input("")


class UriParsingTests(unittest.TestCase):
    def test_anytls_percent_and_ipv6(self):
        uri = ("anytls://p%40ss@[2001:db8::1]:8443"
               "?sni=example.com&insecure=1&fp=chrome&alpn=h2%2Chttp%2F1.1#My%20Node")
        proxy = convert_sub.parse_anytls_uri(uri)
        self.assertEqual(proxy["type"], "anytls")
        self.assertEqual(proxy["server"], "2001:db8::1")
        self.assertEqual(proxy["port"], 8443)
        self.assertEqual(proxy["password"], "p@ss")
        self.assertEqual(proxy["sni"], "example.com")
        self.assertTrue(proxy["skip-cert-verify"])
        self.assertEqual(proxy["client-fingerprint"], "chrome")
        self.assertEqual(proxy["alpn"], ["h2", "http/1.1"])
        self.assertEqual(proxy["name"], "My Node")
        self.assertNotIn("uuid", proxy)
        self.assertNotIn("vless", str(proxy.values()))

    def test_ss_sip002(self):
        userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:pa:ss").decode().rstrip("=")
        proxy = convert_sub.parse_ss_uri(f"ss://{userinfo}@1.2.3.4:8388#ssnode")
        self.assertEqual(proxy["type"], "ss")
        self.assertEqual(proxy["cipher"], "aes-256-gcm")
        self.assertEqual(proxy["password"], "pa:ss")
        self.assertEqual(proxy["port"], 8388)

    def test_trojan(self):
        proxy = convert_sub.parse_trojan_uri(
            "trojan://p%40ss@5.6.7.8:443?sni=x.com&allowInsecure=1#tj")
        self.assertEqual(proxy["type"], "trojan")
        self.assertEqual(proxy["password"], "p@ss")
        self.assertEqual(proxy["sni"], "x.com")
        self.assertTrue(proxy["skip-cert-verify"])

    def test_vless_ws(self):
        proxy = convert_sub.parse_vless_uri(
            "vless://uuid-1@h.example:443?security=tls&sni=s.com&type=ws&path=%2Fws#v")
        self.assertEqual(proxy["type"], "vless")
        self.assertEqual(proxy["uuid"], "uuid-1")
        self.assertTrue(proxy["tls"])
        self.assertEqual(proxy["servername"], "s.com")
        self.assertEqual(proxy["network"], "ws")
        self.assertEqual(proxy["ws-opts"]["path"], "/ws")

    def test_tuic(self):
        proxy = convert_sub.parse_tuic_uri(
            "tuic://u1:p1@h.example:443?sni=s.com&congestion_control=bbr&alpn=h3#t")
        self.assertEqual(proxy["type"], "tuic")
        self.assertEqual(proxy["uuid"], "u1")
        self.assertEqual(proxy["password"], "p1")
        self.assertEqual(proxy["congestion-controller"], "bbr")

    def test_hysteria2(self):
        proxy = convert_sub.parse_hysteria2_uri(
            "hysteria2://auth1@h.example:8443?sni=s.com&insecure=1"
            "&obfs=salamander&obfs-password=ob#hy")
        self.assertEqual(proxy["type"], "hysteria2")
        self.assertEqual(proxy["password"], "auth1")
        self.assertTrue(proxy["skip-cert-verify"])
        self.assertEqual(proxy["obfs"], "salamander")
        self.assertEqual(proxy["obfs-password"], "ob")

    def test_base64_uri_input(self):
        raw = "anytls://pw@1.2.3.4:443?sni=x.com#b64node"
        content = base64.b64encode(raw.encode()).decode()
        out, stats = convert_sub.convert_content(content, warn=lambda _m: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "anytls")
        self.assertEqual(out[0]["name"], "b64node")

    def test_unsupported_scheme_counted_and_warned(self):
        warns = []
        content = "vmess://whatever\nanytls://pw@1.2.3.4:443#ok"
        out, stats = convert_sub.convert_content(content, warn=warns.append)
        self.assertEqual(len(out), 1)
        self.assertEqual(stats.unknown_type, 1)
        self.assertEqual(stats.skipped_unknown, 1)
        self.assertTrue(any("vmess" in w for w in warns))


class UserAgentTests(unittest.TestCase):
    def test_default_and_override(self):
        req = convert_sub.build_request("https://example.com/x", convert_sub.DEFAULT_USER_AGENT)
        self.assertEqual(req.get_header("User-agent"), "ClashMeta/1.18.0")

        req2 = convert_sub.build_request("https://example.com/x", "CustomUA/9")
        self.assertEqual(req2.get_header("User-agent"), "CustomUA/9")

    def test_resolve_user_agent_env_override(self):
        class Args:
            user_agent = None

        os.environ["CONVERT_SUB_UA"] = "EnvUA/2"
        try:
            self.assertEqual(convert_sub.resolve_user_agent(Args()), "EnvUA/2")
        finally:
            os.environ.pop("CONVERT_SUB_UA", None)
        self.assertEqual(convert_sub.resolve_user_agent(Args()), "ClashMeta/1.18.0")


class FetchSecurityTests(unittest.TestCase):
    def test_http_error_redacted(self):
        token_url = "https://provider.example/sub?token=SUPERSECRET"

        def boom(*_a, **_k):
            raise urllib.error.HTTPError(token_url, 403, "Forbidden", {}, None)

        original = convert_sub.urllib.request.urlopen
        convert_sub.urllib.request.urlopen = boom
        try:
            with self.assertRaises(convert_sub.ConvertError) as ctx:
                convert_sub.fetch_subscription(token_url, 5, "UA")
        finally:
            convert_sub.urllib.request.urlopen = original
        message = str(ctx.exception)
        self.assertNotIn("SUPERSECRET", message)
        self.assertNotIn("token=", message)
        self.assertIn("403", message)

    def test_url_error_sanitized(self):
        def boom(*_a, **_k):
            raise urllib.error.URLError("cannot connect to https://x/?token=SUPERSECRET")

        original = convert_sub.urllib.request.urlopen
        convert_sub.urllib.request.urlopen = boom
        try:
            with self.assertRaises(convert_sub.ConvertError) as ctx:
                convert_sub.fetch_subscription("https://x/?token=SUPERSECRET", 5, "UA")
        finally:
            convert_sub.urllib.request.urlopen = original
        self.assertNotIn("SUPERSECRET", str(ctx.exception))

    def test_redact_url(self):
        redacted = convert_sub.redact_url("https://user:pw@host.example:8443/path?token=SECRET")
        self.assertNotIn("SECRET", redacted)
        self.assertNotIn("pw", redacted)
        self.assertEqual(redacted, "https://host.example:8443/<redacted>")

    def test_redact_url_never_raises_on_bad_input(self):
        # A malformed port / IPv6 bracket must not raise nor echo input.
        for bad in ("https://h:99999/path?token=SECRET", "http://[::1", "not a url"):
            redacted = convert_sub.redact_url(bad)
            self.assertNotIn("SECRET", redacted)
            self.assertNotIn("99999", redacted)

    def test_http_error_reason_with_token_not_echoed(self):
        # HTTP reason is upstream-controlled and may itself carry a token.
        token_url = "https://provider.example/sub?token=SYNTH_SECRET"

        def boom(*_a, **_k):
            raise urllib.error.HTTPError(token_url, 403, "bad token=SYNTH_SECRET", {}, None)

        original = convert_sub.urllib.request.urlopen
        convert_sub.urllib.request.urlopen = boom
        try:
            with self.assertRaises(convert_sub.ConvertError) as ctx:
                convert_sub.fetch_subscription(token_url, 5, "UA")
        finally:
            convert_sub.urllib.request.urlopen = original
        message = str(ctx.exception)
        self.assertNotIn("SYNTH_SECRET", message)
        self.assertNotIn("token=", message)
        self.assertNotIn("bad", message)
        self.assertIn("403", message)

    def test_http_error_reason_with_url_not_echoed(self):
        def boom(*_a, **_k):
            raise urllib.error.HTTPError(
                "https://x/", 502, "see https://x/?token=SYNTH_SECRET", {}, None
            )

        original = convert_sub.urllib.request.urlopen
        convert_sub.urllib.request.urlopen = boom
        try:
            with self.assertRaises(convert_sub.ConvertError) as ctx:
                convert_sub.fetch_subscription("https://x/?token=SYNTH_SECRET", 5, "UA")
        finally:
            convert_sub.urllib.request.urlopen = original
        message = str(ctx.exception)
        self.assertNotIn("SYNTH_SECRET", message)
        self.assertNotIn("https://", message)
        self.assertIn("502", message)

    def test_url_error_bare_token_not_echoed(self):
        # A bare credential in the URLError reason must not leak either.
        def boom(*_a, **_k):
            raise urllib.error.URLError("token=SYNTH_SECRET rejected")

        original = convert_sub.urllib.request.urlopen
        convert_sub.urllib.request.urlopen = boom
        try:
            with self.assertRaises(convert_sub.ConvertError) as ctx:
                convert_sub.fetch_subscription("https://x/?token=SYNTH_SECRET", 5, "UA")
        finally:
            convert_sub.urllib.request.urlopen = original
        message = str(ctx.exception)
        self.assertNotIn("SYNTH_SECRET", message)
        self.assertNotIn("token=", message)
        self.assertNotIn("rejected", message)

    def test_build_request_invalid_url_no_credential(self):
        # An unparseable (scheme-less) URL makes urllib raise
        # ValueError("unknown url type: '<raw url>'"); the wrapper must
        # surface only a redacted classification, never the raw URL.
        with self.assertRaises(convert_sub.ConvertError) as ctx:
            convert_sub.build_request("/SYNTH_SECRET@host:443/x", "UA")
        message = str(ctx.exception)
        self.assertNotIn("SYNTH_SECRET", message)
        self.assertNotIn("/x", message)
        self.assertIn("invalid subscription URL", message)


class FailClosedTests(unittest.TestCase):
    def test_missing_pyyaml_fails_closed(self):
        saved_has, saved_yaml = convert_sub.HAS_YAML, convert_sub.yaml
        convert_sub.HAS_YAML = False
        convert_sub.yaml = None
        try:
            with self.assertRaises(convert_sub.ConvertError):
                convert_sub.to_yaml([{"name": "a", "type": "ss"}])
            with tempfile.TemporaryDirectory() as tmp:
                out_path = os.path.join(tmp, "out.yaml")
                with open(out_path, "w", encoding="utf-8") as handle:
                    handle.write("SENTINEL")
                rc = convert_sub.main(["--uri", "anytls://pw@h:443#n", "-o", out_path])
                self.assertEqual(rc, convert_sub.EXIT_USAGE)
                with open(out_path, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "SENTINEL")
        finally:
            convert_sub.HAS_YAML = saved_has
            convert_sub.yaml = saved_yaml


class NoOverwriteTests(unittest.TestCase):
    def test_failure_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "providers.yaml")
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write("ORIGINAL")
            in_path = os.path.join(tmp, "in.txt")
            with open(in_path, "w", encoding="utf-8") as handle:
                handle.write("vmess://unsupported-only\n")

            rc = convert_sub.main(["-i", in_path, "-o", out_path, "-q"])
            self.assertEqual(rc, convert_sub.EXIT_EMPTY)
            with open(out_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "ORIGINAL")

    def test_write_output_safely_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "out.yaml")
            convert_sub.write_output_safely(out_path, "hello\n")
            with open(out_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "hello\n")


class MergeTests(unittest.TestCase):
    def test_merge_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = os.path.join(tmp, "existing.yaml")
            with open(existing, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "proxies": [
                        {"name": "A", "type": "ss", "server": "1", "port": 1},
                        {"name": "B", "type": "ss", "server": "2", "port": 2},
                    ],
                    "mode": "rule",
                }, handle, sort_keys=False)

            incoming = [
                {"name": "B", "type": "ss", "server": "2", "port": 2},
                {"name": "C", "type": "ss", "server": "3", "port": 3},
            ]
            text, added = convert_sub.merge_into_yaml(incoming, existing)
            self.assertEqual(added, 1)
            data = yaml.safe_load(text)
            names = [p["name"] for p in data["proxies"]]
            self.assertEqual(names, ["A", "B", "C"])
            self.assertEqual(data["mode"], "rule")


class EndToEndTests(unittest.TestCase):
    def test_main_writes_file(self):
        nodes = synthetic_56()
        content = yaml.safe_dump({"proxies": nodes}, sort_keys=False)
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "in.yaml")
            out_path = os.path.join(tmp, "out.yaml")
            with open(in_path, "w", encoding="utf-8") as handle:
                handle.write(content)
            rc = convert_sub.main(["-i", in_path, "-o", out_path, "-q"])
            self.assertEqual(rc, convert_sub.EXIT_OK)
            with open(out_path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            self.assertEqual(len(data["proxies"]), 56)

    def test_list_mode(self):
        content = yaml.safe_dump({"proxies": [anytls_node(1)]}, sort_keys=False)
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "in.yaml")
            with open(in_path, "w", encoding="utf-8") as handle:
                handle.write(content)
            buffer = io.StringIO()
            saved = sys.stdout
            sys.stdout = buffer
            try:
                rc = convert_sub.main(["-i", in_path, "--list", "-q"])
            finally:
                sys.stdout = saved
            self.assertEqual(rc, convert_sub.EXIT_OK)
            self.assertIn("node#1 [anytls]", buffer.getvalue())
            self.assertNotIn("anytls-01", buffer.getvalue())


class MalformedUriSafetyTests(unittest.TestCase):
    def test_invalid_port_not_echoed_in_warning(self):
        warns = []
        out, stats = convert_sub.convert_content(
            "anytls://pw@host:SYNTH_SECRET#node", warn=warns.append
        )
        self.assertEqual(len(out), 0)
        self.assertEqual(stats.skipped_malformed, 1)
        joined = "\n".join(warns)
        self.assertNotIn("SYNTH_SECRET", joined)
        self.assertIn("invalid port", joined)


class InputIntegrityTests(unittest.TestCase):
    def test_malformed_clash_yaml_with_url_fails_closed(self):
        # Broken Clash YAML that merely *contains* a URL must not be mined.
        content = "proxies: [anytls, ss\nurl: anytls://EVIL@host:443#x"
        with self.assertRaises(convert_sub.ConvertError):
            convert_sub.detect_input(content)

    def test_pure_uri_list_detected(self):
        content = "anytls://pw@1.2.3.4:443#a\nanytls://pw@5.6.7.8:443#b"
        mode, payload, dropped = convert_sub.detect_input(content)
        self.assertEqual(mode, "uris")
        self.assertEqual(len(payload), 2)
        self.assertEqual(dropped, 0)

    def test_split_uri_lines_counts_non_uri(self):
        uris, dropped = convert_sub._split_uri_lines(
            "anytls://a@h:443#x\n\nnot a uri\nss://b@h:8388#y"
        )
        self.assertEqual(len(uris), 2)
        self.assertEqual(dropped, 1)

    def test_base64_non_uri_lines_counted_and_warned(self):
        raw = "anytls://pw@1.2.3.4:443#b64\n# not a uri line"
        content = base64.b64encode(raw.encode()).decode()
        warns = []
        out, stats = convert_sub.convert_content(content, warn=warns.append)
        self.assertEqual(len(out), 1)
        self.assertEqual(stats.skipped_non_uri, 1)
        self.assertTrue(any("non-URI line" in w for w in warns))

    def test_list_empty_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "in.txt")
            with open(in_path, "w", encoding="utf-8") as handle:
                handle.write("vmess://unsupported-only\n")
            rc = convert_sub.main(["-i", in_path, "--list", "-q"])
            self.assertNotEqual(rc, convert_sub.EXIT_OK)
            self.assertEqual(rc, convert_sub.EXIT_EMPTY)


def singbox_nodes():
    nodes = []
    for index, ptype in enumerate(convert_sub.SINGBOX_TYPES):
        node = {"type": ptype, "tag": "shared", "server": f"node-{index}.example",
                "server_port": 443}
        if ptype in {"vless", "tuic"}:
            node["uuid"] = "00000000-0000-4000-8000-000000000001"
        if ptype != "vless":
            node["password"] = "SYNTH_PASSWORD"
        if ptype == "shadowsocks":
            node["method"] = "aes-128-gcm"
        else:
            node["tls"] = {"enabled": True, "server_name": "tls.example", "insecure": False,
                           "alpn": ["h3" if ptype in {"tuic", "hysteria2"} else "h2"]}
        nodes.append(node)
    return nodes


class SingboxTests(unittest.TestCase):
    def test_six_protocols_and_non_nodes(self):
        nodes = singbox_nodes()
        nodes += [{"type": ptype} for ptype in sorted(convert_sub.NON_NODE_TYPES)]
        nodes += [{"type": "SYNTH_UNKNOWN_SECRET", "tag": "SYNTH_TAG_SECRET"}]
        warns = []
        proxies, stats = convert_sub.convert_content(json.dumps({"outbounds": nodes}), warn=warns.append)
        self.assertEqual([p["type"] for p in proxies], list(convert_sub.SINGBOX_TYPES.values()))
        self.assertEqual(stats.input_nodes, 12)
        self.assertEqual(stats.output_nodes, 6)
        self.assertEqual(stats.skipped_non_node, 5)
        self.assertEqual(stats.skipped_unknown, 1)
        self.assertEqual(stats.unknown_type, 1)
        self.assertEqual(stats.input_format, "sing-box")
        self.assertEqual(proxies[0]["cipher"], "aes-128-gcm")
        self.assertEqual(proxies[2]["servername"], "tls.example")
        self.assertTrue(proxies[2]["tls"])
        self.assertEqual(proxies[3]["uuid"], nodes[3]["uuid"])
        self.assertEqual(proxies[5]["password"], "SYNTH_PASSWORD")
        self.assertNotIn("uuid", proxies[5])
        self.assertFalse(proxies[5]["skip-cert-verify"])
        self.assertNotIn("SYNTH_", "\n".join(warns))

    def test_tls_transport_and_protocol_options(self):
        nodes = singbox_nodes()
        nodes[1]["transport"] = {"type": "grpc", "service_name": "service"}
        nodes[2].update({"flow": "xtls-rprx-vision", "packet_encoding": "xudp",
                         "transport": {"type": "ws", "path": "/ws", "headers": {"Host": "ws.example"},
                                       "max_early_data": 2048, "early_data_header_name": "Sec-WebSocket-Protocol"},
                         "multiplex": {"enabled": True, "protocol": "h2mux", "max_connections": 4, "padding": True}})
        nodes[2]["tls"].update({"utls": {"enabled": True, "fingerprint": "chrome"},
                               "reality": {"enabled": True, "public_key": "SYNTH_PUBLIC", "short_id": "abcd"}})
        nodes[3].update({"congestion_control": "bbr", "udp_relay_mode": "native",
                         "zero_rtt_handshake": True, "heartbeat": "2.5s"})
        nodes[4].update({"up_mbps": 20, "down_mbps": 100,
                         "obfs": {"type": "salamander", "password": "SYNTH_OBFS"}})
        nodes[5].update({"idle_session_check_interval": "30s", "idle_session_timeout": "1m",
                         "min_idle_session": 2})
        proxies, stats = convert_sub.convert_content(json.dumps({"outbounds": nodes}))
        self.assertEqual(stats.output_nodes, 6)
        self.assertEqual(proxies[1]["grpc-opts"], {"grpc-service-name": "service"})
        self.assertEqual(proxies[2]["ws-opts"]["path"], "/ws")
        self.assertEqual(proxies[2]["ws-opts"]["max-early-data"], 2048)
        self.assertEqual(proxies[2]["client-fingerprint"], "chrome")
        self.assertEqual(proxies[2]["reality-opts"]["public-key"], "SYNTH_PUBLIC")
        self.assertEqual(proxies[2]["smux"]["max-connections"], 4)
        self.assertEqual(proxies[3]["heartbeat-interval"], 2500)
        self.assertTrue(proxies[3]["reduce-rtt"])
        self.assertEqual(proxies[3]["congestion-controller"], "bbr")
        self.assertEqual(proxies[4]["obfs-password"], "SYNTH_OBFS")
        self.assertEqual(proxies[4]["down"], 100)
        self.assertEqual(proxies[5]["idle-session-timeout"], 60)
        self.assertEqual(proxies[5]["min-idle-session"], 2)

    def test_invalid_and_unmapped_options_are_counted_not_exposed(self):
        base = singbox_nodes()[5]
        bad = [None, {"type": []}, {**base, "server_port": True}, {**base, "password": ""},
               {**base, "tls": {"enabled": "SYNTH_SECRET"}},
               {**base, "detour": "SYNTH_SECRET"},
               {**base, "tls": {"enabled": True, "certificate_path": "SYNTH_SECRET"}}]
        warns = []
        proxies, stats = convert_sub.convert_content(json.dumps({"outbounds": bad + [base]}), warn=warns.append)
        self.assertEqual(len(proxies), 1)
        self.assertEqual(stats.skipped_malformed, 5)
        self.assertEqual(stats.skipped_unsupported, 2)
        self.assertNotIn("SYNTH_SECRET", "\n".join(warns))

    def test_common_dial_fields_and_disabled_mux_preserve_candidate(self):
        node = singbox_nodes()[5]
        node.update({"bind_interface": "fixture-interface", "routing_mark": 42,
                     "tcp_fast_open": True, "tcp_multi_path": False,
                     "multiplex": {"enabled": False}})
        out, stats = convert_sub.convert_content(json.dumps({"outbounds": [node]}))
        self.assertEqual(stats.output_nodes, 1)
        self.assertEqual(out[0]["interface-name"], "fixture-interface")
        self.assertEqual(out[0]["routing-mark"], 42)
        self.assertTrue(out[0]["tfo"])
        self.assertFalse(out[0]["mptcp"])
        self.assertNotIn("smux", out[0])

    def test_unsupported_transport_does_not_become_tcp(self):
        node = singbox_nodes()[2]
        node["transport"] = {"type": "quic"}
        out, stats = convert_sub.convert_content(json.dumps({"outbounds": [node]}), warn=lambda _: None)
        self.assertEqual(out, [])
        self.assertEqual(stats.skipped_unsupported, 1)

    def test_malformed_json_never_mines_embedded_uri(self):
        for text in ('{"outbounds": ["anytls://SYNTH_SECRET@h:443"',
                     '{"outbounds": null}', '{"outbounds": {}}', '{"unrelated": []}'):
            with self.subTest(text=text), self.assertRaises(convert_sub.ConvertError):
                convert_sub.convert_content(text)

    def test_bom_and_json_clash_passthrough(self):
        proxy = anytls_node(1)
        out, _ = convert_sub.convert_content('\ufeff' + json.dumps({"proxies": [proxy]}))
        self.assertEqual(out, [proxy])


class SemanticMergeTests(unittest.TestCase):
    def test_auth_transport_and_endpoint_variants_retained(self):
        base = anytls_node(1)
        nodes = [base, {**base, "name": "different-label"},
                 {**base, "server": "different.example"}, {**base, "port": 8443},
                 {**base, "password": "other-synthetic-password"}, {**base, "sni": "other.example"}]
        original = copy.deepcopy(nodes)
        merged = convert_sub.merge_proxies(nodes)
        self.assertEqual(len(merged), 5)
        self.assertEqual(len({p["name"] for p in merged}), 5)
        self.assertEqual(convert_sub.merge_proxies(nodes), merged)
        self.assertEqual(nodes, original)

    def test_protocol_uuid_and_nested_key_order(self):
        base = {"name": "n", "type": "vless", "server": "h", "port": 443, "uuid": "SYNTH_UUID",
                "ws-opts": {"path": "/", "headers": {"Host": "h"}}}
        reordered = {**base, "ws-opts": {"headers": {"Host": "h"}, "path": "/"}}
        merged = convert_sub.merge_proxies([base, reordered, {**base, "uuid": "OTHER_UUID"},
                                           {**base, "type": "vmess"}])
        self.assertEqual(len(merged), 3)

    def test_suffix_collision_reserved_and_missing_names(self):
        base = anytls_node(1)
        nodes = [{**base, "name": "A"}, {**anytls_node(2), "name": "A"},
                 {**anytls_node(3), "name": "A [2]"}, {**anytls_node(4), "name": None}]
        merged = convert_sub.merge_proxies(nodes)
        self.assertEqual([p["name"] for p in merged], ["A", "A [3]", "A [2]", "node#4"])

    def test_merge_config_retains_same_name_new_endpoint_and_groups(self):
        base = anytls_node(1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            data = {"proxies": [base], "proxy-groups": [{"name": "select", "type": "select", "proxies": [base["name"]]}]}
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle)
            text, added = convert_sub.merge_into_yaml([{**base, "server": "new.example"}], path)
            result = yaml.safe_load(text)
            self.assertEqual(added, 1)
            self.assertEqual(len(result["proxies"]), 2)
            self.assertEqual(result["proxy-groups"], data["proxy-groups"])


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "candidate.yaml")
        self.settings = {f"SUBSCRIPTION_URL_{i}": f"https://source-{i}.example/sub?token=SYNTH_URL_{i}"
                         for i in range(1, 4)}
        self.settings.update({f"SUBSCRIPTION_UA_{i}": f"FixtureUA/{i}" for i in range(1, 4)})
        self.responses = [yaml.safe_dump({"proxies": synthetic_56()}),
                          json.dumps({"outbounds": singbox_nodes()}),
                          base64.b64encode(b"anytls://SYNTH_PASSWORD@extra.example:443#extra").decode()]
        self.write_sentinel()

    def write_sentinel(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("ORIGINAL")

    def read_output(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def run_sources(self, *args, env_file=False, responses=None):
        stderr, stdout = io.StringIO(), io.StringIO()
        if env_file:
            path = os.path.join(self.tmp.name, "synthetic.env")
            with open(path, "w", encoding="utf-8-sig") as handle:
                handle.write("# fixture only\nUNRELATED=ignored\n")
                for key, value in self.settings.items():
                    handle.write(f'{key}="{value}"\n')
            source_args = ["--sources-env-file", path]
        else:
            source_args = ["--sources-env"]
        with patch.object(convert_sub.os, "environ", self.settings), \
                patch.object(convert_sub, "fetch_subscription", side_effect=self.responses if responses is None else responses) as fetch, \
                redirect_stderr(stderr), redirect_stdout(stdout):
            rc = convert_sub.main(source_args + ["-o", self.path] + list(args))
        return rc, stderr.getvalue(), stdout.getvalue(), fetch

    def test_three_formats_env_file_and_per_source_ua(self):
        rc, logs, stdout, fetch = self.run_sources(env_file=True)
        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "")
        proxies = yaml.safe_load(self.read_output())["proxies"]
        self.assertEqual(len(proxies), 63)
        self.assertEqual(len({p["name"] for p in proxies}), 63)
        for i, call in enumerate(fetch.call_args_list, 1):
            self.assertEqual(call.args, (self.settings[f"SUBSCRIPTION_URL_{i}"], 30, f"FixtureUA/{i}"))
            self.assertIn(f"source#{i}:", logs)
        self.assertIn("merged=63", logs)
        self.assertIn("failed_sources=0", logs)
        self.assertNotIn("SYNTH_", logs)
        self.assertNotIn("token=", logs)

    def test_mock_http_responses_use_actual_fetch_and_per_source_ua(self):
        responses = [io.BytesIO(text.encode("utf-8")) for text in self.responses]
        with patch.object(convert_sub.os, "environ", self.settings), \
                patch.object(convert_sub.urllib.request, "urlopen", side_effect=responses) as http, \
                redirect_stderr(io.StringIO()):
            rc = convert_sub.main(["--sources-env", "-o", self.path])
        self.assertEqual(rc, 0)
        self.assertEqual(len(yaml.safe_load(self.read_output())["proxies"]), 63)
        for index, call in enumerate(http.call_args_list, 1):
            self.assertEqual(call.args[0].get_header("User-agent"), f"FixtureUA/{index}")
            self.assertEqual(call.args[0].full_url, self.settings[f"SUBSCRIPTION_URL_{index}"])

    def test_same_url_changes_format_and_ua(self):
        url = self.settings["SUBSCRIPTION_URL_1"]
        for i in range(1, 4):
            self.settings[f"SUBSCRIPTION_URL_{i}"] = url
        rc, _, _, fetch = self.run_sources()
        self.assertEqual(rc, 0)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(len(yaml.safe_load(self.read_output())["proxies"]), 63)

    def test_network_and_format_failure_still_write_partial_pool(self):
        responses = [self.responses[0], convert_sub.ConvertError("SYNTH_SECRET", convert_sub.EXIT_NETWORK),
                     '{"outbounds": ["SYNTH_SECRET"']
        rc, logs, _, _ = self.run_sources("--min-nodes", "50", responses=responses)
        self.assertEqual(rc, convert_sub.EXIT_PARTIAL)
        self.assertEqual(len(yaml.safe_load(self.read_output())["proxies"]), 56)
        self.assertIn("failed_sources=2", logs)
        self.assertIn("source#2: format=unrecognised input=0 output=0", logs)
        self.assertIn("exit=5", logs)
        self.assertNotIn("SYNTH_SECRET", logs)

    def test_partial_below_minimum_keeps_existing_output(self):
        rc, logs, _, _ = self.run_sources("--min-nodes", "57", responses=[self.responses[0], "invalid", "invalid"])
        self.assertEqual(rc, convert_sub.EXIT_EMPTY)
        self.assertEqual(self.read_output(), "ORIGINAL")
        self.assertIn("below minimum 57", logs)

    def test_all_sources_fail_closed(self):
        rc, logs, _, fetch = self.run_sources(responses=["invalid"] * 3)
        self.assertEqual(rc, convert_sub.EXIT_EMPTY)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(self.read_output(), "ORIGINAL")
        self.assertIn("failed_sources=3", logs)

    def test_unset_source_not_silently_successful(self):
        del self.settings["SUBSCRIPTION_URL_2"]
        rc, logs, _, fetch = self.run_sources()
        self.assertEqual(rc, convert_sub.EXIT_PARTIAL)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("failed_sources=1", logs)

    def test_minimum_applies_after_dedup_and_defaults_to_one(self):
        one = yaml.safe_dump({"proxies": [anytls_node(1)] * 5})
        self.settings["MIN_CANDIDATE_NODES"] = "2"
        rc, _, _, _ = self.run_sources(responses=[one] * 3)
        self.assertEqual(rc, convert_sub.EXIT_EMPTY)
        self.assertEqual(self.read_output(), "ORIGINAL")
        rc, logs, _, _ = self.run_sources("--min-nodes", "1", responses=[one] * 3)
        self.assertEqual(rc, 0)
        self.assertEqual(len(yaml.safe_load(self.read_output())["proxies"]), 1)
        self.assertIn("duplicates=14", logs)
        del self.settings["MIN_CANDIDATE_NODES"]
        rc, _, _, _ = self.run_sources(responses=[one] * 3)
        self.assertEqual(rc, 0)

    def test_invalid_minimum_rejected_before_fetch(self):
        for minimum in ("0", "-1", "1.5", "SYNTH_SECRET", ""):
            with self.subTest(minimum=minimum):
                self.settings["MIN_CANDIDATE_NODES"] = minimum
                rc, logs, _, fetch = self.run_sources()
                self.assertEqual(rc, convert_sub.EXIT_USAGE)
                fetch.assert_not_called()
                self.assertEqual(self.read_output(), "ORIGINAL")
                self.assertNotIn("SYNTH_SECRET", logs)

    def test_output_replace_failure_is_atomic_and_redacted(self):
        with patch.object(convert_sub.os, "replace", side_effect=OSError("SYNTH_SECRET")):
            rc, logs, _, _ = self.run_sources()
        self.assertEqual(rc, convert_sub.EXIT_BAD_INPUT)
        self.assertEqual(self.read_output(), "ORIGINAL")
        self.assertNotIn("SYNTH_SECRET", logs)
        self.assertFalse(any(p.startswith(".convert_sub.") for p in os.listdir(self.tmp.name)))

    def test_list_is_sanitized_and_still_reports_partial_failure(self):
        node = {**anytls_node(1), "name": "SYNTH_NAME_SECRET", "server": "SYNTH_SERVER_SECRET"}
        rc, logs, stdout, _ = self.run_sources("--list", "-q", responses=[yaml.safe_dump({"proxies": [node]}), "bad", "bad"])
        self.assertEqual(rc, convert_sub.EXIT_PARTIAL)
        self.assertEqual(stdout, "node#1 [anytls]\n")
        self.assertNotIn("SYNTH_", logs + stdout)
        self.assertEqual(self.read_output(), "ORIGINAL")

    def test_existing_merge_nodes_cannot_hide_threshold_failure(self):
        target = os.path.join(self.tmp.name, "existing.yaml")
        text = yaml.safe_dump({"proxies": synthetic_56(), "mode": "rule"})
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
        one = yaml.safe_dump({"proxies": [anytls_node(100)]})
        rc, _, _, _ = self.run_sources("--merge", target, "--min-nodes", "2", responses=[one] * 3)
        self.assertEqual(rc, convert_sub.EXIT_EMPTY)
        self.assertEqual(self.read_output(), "ORIGINAL")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), text)


class SafetyRegressionTests(unittest.TestCase):
    def test_bare_credentials_in_names_types_and_fragments_never_logged(self):
        warns = []
        nodes = [{**anytls_node(1), "type": "SYNTH_UUID", "name": "SYNTH_PASSWORD"}]
        out, _ = convert_sub.convert_content(yaml.safe_dump({"proxies": nodes}), warn=warns.append)
        self.assertEqual(out, nodes)
        convert_sub.convert_content("SYNTH_SCHEME://payload#SYNTH_FRAGMENT", warn=warns.append)
        self.assertNotIn("SYNTH_", "\n".join(warns))

    def test_recursive_yaml_node_is_skipped(self):
        warns = []
        out, stats = convert_sub.convert_content("proxies: [&p {name: n, type: anytls, extra: *p}]", warn=warns.append)
        self.assertEqual(out, [])
        self.assertEqual(stats.skipped_malformed, 1)

    def test_source_env_quotes_delimiters_and_invalid_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "synthetic.env")
            with open(path, "w", encoding="utf-8-sig") as handle:
                handle.write('SUBSCRIPTION_URL_1="https://fixture.example/path?a=b=c#fragment"\nIGNORED=value\n')
            self.assertEqual(convert_sub.read_sources_env(path),
                             {"SUBSCRIPTION_URL_1": "https://fixture.example/path?a=b=c#fragment"})
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("SUBSCRIPTION_URL_1=SYNTH_SECRET\n")
            with self.assertRaises(convert_sub.ConvertError) as ctx:
                convert_sub.read_sources_env(path)
            self.assertNotIn("SYNTH_SECRET", str(ctx.exception))

    def test_local_url_schemes_rejected_without_reading_files(self):
        for url in ("file:///SYNTH_SECRET", "ftp://fixture.example/SYNTH_SECRET"):
            with self.assertRaises(convert_sub.ConvertError) as ctx:
                convert_sub.fetch_subscription(url, 1, "FixtureUA")
            self.assertNotIn("SYNTH_SECRET", str(ctx.exception))

    def test_single_source_threshold_and_malformed_merge_keep_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("ORIGINAL")
            for args in (["--min-nodes", "2"], ["--merge", path]):
                with redirect_stderr(io.StringIO()):
                    rc = convert_sub.main(["--uri", "anytls://synthetic@fixture.example:443", "-o", path] + args)
                self.assertNotEqual(rc, 0)
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "ORIGINAL")

    def test_invalid_empty_merge_proxy_shapes_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            for text in ("proxies: {}", "proxies: null", "proxies: ''", "proxies: 0", "proxies: [invalid]"):
                with self.subTest(text=text):
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(text)
                    with redirect_stderr(io.StringIO()):
                        rc = convert_sub.main(["--uri", "anytls://synthetic@fixture.example:443",
                                               "--merge", path, "-o", path])
                    self.assertEqual(rc, convert_sub.EXIT_BAD_INPUT)
                    with open(path, encoding="utf-8") as handle:
                        self.assertEqual(handle.read(), text)

    def test_powershell_entry_only_delegates_to_candidate_converter(self):
        path = os.path.join(HERE, os.pardir, "scripts", "update-subscription.ps1")
        with open(path, encoding="utf-8-sig") as handle:
            script = handle.read()
        for forbidden in ("Remove-Item", "Invoke-WebRequest", "Invoke-RestMethod", "Ensure-Admin", "start.ps1", "restart.ps1", "common.ps1"):
            self.assertNotIn(forbidden, script)
        self.assertIn("--sources-env-file", script)
        self.assertIn("exit $result", script)


class PowerShellIsolationTests(unittest.TestCase):
    def test_wrapper_exit_codes_with_temporary_stub_converter(self):
        shell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not shell:
            self.skipTest("PowerShell unavailable; wrapper runtime not verified")
        source = os.path.join(HERE, os.pardir, "scripts", "update-subscription.ps1")
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = os.path.join(tmp, "update-subscription.ps1")
            shutil.copyfile(source, wrapper)
            with open(os.path.join(tmp, "convert_sub.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "import pathlib, sys\n"
                    "assert '--sources-env-file' in sys.argv\n"
                    "code = int(sys.argv[sys.argv.index('--min-nodes') + 1])\n"
                    "print('warning: synthetic fixture', file=sys.stderr)\n"
                    "if code in (1, 5):\n"
                    "    pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text('CANDIDATE')\n"
                    "sys.exit(0 if code == 1 else code)\n"
                )
            env_path = os.path.join(tmp, "synthetic.env")
            with open(env_path, "w", encoding="utf-8") as handle:
                handle.write("# synthetic fixture; no URLs\n")
            output = os.path.join(tmp, "candidate pool.yaml")
            for stub_code, expected in ((1, 0), (5, 5), (3, 3)):
                with self.subTest(expected=expected):
                    with open(output, "w", encoding="utf-8") as handle:
                        handle.write("ORIGINAL")
                    result = subprocess.run(
                        [shell, "-NoProfile", "-NonInteractive", "-File", wrapper,
                         "-EnvFile", env_path, "-Output", output, "-MinNodes", str(stub_code)],
                        capture_output=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr.decode(errors="replace"))
                    with open(output, encoding="utf-8") as handle:
                        self.assertEqual(handle.read(), "ORIGINAL" if expected == 3 else "CANDIDATE")


if __name__ == "__main__":
    unittest.main()
