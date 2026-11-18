"""node_provider: subscription parsing in three formats + semantic de-duplication.

Moved from node_ops in S3; node extraction now belongs to the provider.
"""

import base64
import json
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_provider import subscription as sub  # noqa: E402
from node_provider.envfile import EnvFileError, read_env_file  # noqa: E402


CLASH_YAML = """
proxies:
  - name: us-01
    type: anytls
    server: 1.1.1.1
    port: 443
    password: pw1
    sni: a.example
    alpn: [h2]
  - name: hk-01
    type: trojan
    server: 2.2.2.2
    port: 8443
    password: pw2
"""

SINGBOX_JSON = json.dumps({
    "outbounds": [
        {"type": "selector", "tag": "select"},
        {"type": "vless", "tag": "sg-01", "server": "3.3.3.3", "server_port": 443,
         "uuid": "uuid-1", "tls": {"enabled": True, "server_name": "s.example"}},
        {"type": "trojan", "tag": "jp-01", "server": "4.4.4.4", "server_port": 443,
         "password": "pw3"},
    ]
})

URI_LIST = (
    "anytls://pw@5.5.5.5:443?sni=x.example#us-uri\n"
    "ss://YWVzLTI1Ni1nY206cHc=@6.6.6.6:8388#sg-uri\n"
)


def setUpModule():
    guard = patch.object(sub.urllib.request, "urlopen",
                         side_effect=AssertionError("network disabled in tests"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)


class FormatParsingTests(unittest.TestCase):
    def test_clash_yaml(self):
        nodes = sub.convert_content(CLASH_YAML)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["type"], "anytls")
        self.assertEqual(nodes[0]["name"], "us-01")
        self.assertEqual(nodes[1]["name"], "hk-01")
        self.assertEqual(nodes[1]["type"], "trojan")

    def test_singbox_json_skips_non_node_outbound(self):
        nodes = sub.convert_content(SINGBOX_JSON)
        self.assertEqual([node["name"] for node in nodes], ["sg-01", "jp-01"])
        self.assertEqual(nodes[0]["type"], "vless")
        self.assertTrue(nodes[0]["tls"])
        self.assertEqual(nodes[0]["servername"], "s.example")

    def test_base64_uri_list(self):
        encoded = base64.b64encode(URI_LIST.encode("utf-8")).decode("ascii")
        nodes = sub.convert_content(encoded)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["type"], "anytls")
        self.assertEqual(nodes[0]["name"], "us-uri")
        self.assertEqual(nodes[1]["type"], "ss")
        self.assertEqual(nodes[1]["cipher"], "aes-256-gcm")

    def test_plain_uri_list(self):
        nodes = sub.convert_content(URI_LIST)
        self.assertEqual({node["type"] for node in nodes}, {"anytls", "ss"})

    def test_empty_body_is_classified_empty(self):
        with self.assertRaises(sub.SubscriptionError) as caught:
            sub.convert_content("   \n")
        self.assertEqual(caught.exception.kind, "empty")


class DedupeTests(unittest.TestCase):
    def test_semantic_duplicate_ignores_name(self):
        a = {"name": "a", "type": "trojan", "server": "1.1.1.1", "port": 443, "password": "p"}
        b = {"name": "b", "type": "trojan", "server": "1.1.1.1", "port": 443, "password": "p"}
        c = {"name": "c", "type": "trojan", "server": "1.1.1.1", "port": 443, "password": "q"}
        result = sub.dedupe_nodes([a, b, c])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "a")
        self.assertEqual(result[1]["name"], "c")

    def test_duplicate_names_are_made_unique(self):
        a = {"name": "same", "type": "ss", "server": "1.1.1.1", "port": 1, "password": "a"}
        b = {"name": "same", "type": "ss", "server": "2.2.2.2", "port": 2, "password": "b"}
        result = sub.dedupe_nodes([a, b])
        self.assertEqual([node["name"] for node in result], ["same", "same [2]"])

    def test_malformed_yaml_containing_urls_fails_closed(self):
        content = "proxies:\n  - name: x\n    bad: [unclosed\n  url: https://leak.example/token"
        with self.assertRaises(sub.SubscriptionError):
            sub.convert_content(content)


class EnvFileTests(unittest.TestCase):
    def _write(self, text):
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".env")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_bom_quotes_and_no_expansion(self):
        path = self._write(
            "\ufeffSUBSCRIPTION_URL_1=\"https://example.invalid/a?token=$HOME\"\n"
            "SUBSCRIPTION_UA_1='custom-ua'\n"
            "# comment\n\n"
        )
        values = read_env_file(path)
        self.assertEqual(values["SUBSCRIPTION_URL_1"], "https://example.invalid/a?token=$HOME")
        self.assertEqual(values["SUBSCRIPTION_UA_1"], "custom-ua")

    def test_duplicate_key_rejected(self):
        path = self._write("A=1\nA=2\n")
        with self.assertRaises(EnvFileError):
            read_env_file(path)

    def test_unclosed_quote_rejected(self):
        path = self._write("A=\"unclosed\n")
        with self.assertRaises(EnvFileError):
            read_env_file(path)

    def test_source_settings(self):
        settings = sub.source_settings({
            "SUBSCRIPTION_URL_1": "https://a.invalid/x",
            "SUBSCRIPTION_UA_2": "ua2",
            "SUBSCRIPTION_URL_3": "",
        })
        self.assertIn("1", settings)
        self.assertNotIn("2", settings)
        self.assertNotIn("3", settings)
        self.assertEqual(settings["1"]["ua"], sub.DEFAULT_USER_AGENT)


class FetchProxyTests(unittest.TestCase):
    URL = "https://a.invalid/sub"

    def _fake_opener(self):
        opener = patch.object(sub.urllib.request, "build_opener").start()
        self.addCleanup(patch.stopall)
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = b"proxies: []"
        return opener

    def test_default_fetch_goes_through_local_proxy(self):
        opener = self._fake_opener()
        with patch.object(sub.urllib.request, "urlopen") as urlopen:
            body = sub.fetch_subscription(self.URL, "ua", 5)
        self.assertEqual(body, "proxies: []")
        urlopen.assert_not_called()
        handler = opener.call_args[0][0]
        self.assertIsInstance(handler, sub.urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies.get("https"), sub.DEFAULT_FETCH_PROXY)
        self.assertEqual(handler.proxies.get("http"), sub.DEFAULT_FETCH_PROXY)

    def test_custom_proxy_is_used(self):
        opener = self._fake_opener()
        sub.fetch_subscription(self.URL, "ua", 5, fetch_proxy="http://127.0.0.1:9999")
        handler = opener.call_args[0][0]
        self.assertEqual(handler.proxies.get("https"), "http://127.0.0.1:9999")

    def test_empty_proxy_means_direct(self):
        with patch.object(sub.urllib.request, "build_opener") as build_opener, \
                patch.object(sub.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"x"
            body = sub.fetch_subscription(self.URL, "ua", 5, fetch_proxy="")
        self.assertEqual(body, "x")
        build_opener.assert_not_called()
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
