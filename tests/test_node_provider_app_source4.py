"""node_provider source4 (西瓜加速) cache decryption, fully synthetic fixtures.

No test touches a real DPAPI key, the real ShuttleClient directory or the
network.  Every credential in these fixtures is made up.
"""

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from node_provider import app_extract  # noqa: E402

MASTER_KEY = bytes(range(32))
CONTENT_KEY = bytes(range(32, 64))
DPAPI_BLOB = b"synthetic-dpapi-blob"
APP_ID = "com.shuttle.client." + "a" * 32
API_BASE = "https://api.example.invalid"
SECRET = "fixture-session-secret-do-not-leak"

NODES = [
    {"name": "xigua-sg-01", "type": "trojan", "server": "203.0.113.10",
     "port": 443, "password": "fixture-password-1", "sni": "example.invalid"},
    {"name": "xigua-us-01", "type": "vless", "server": "203.0.113.11",
     "port": 8443, "uuid": "11111111-2222-3333-4444-555555555555"},
]


def b64url_nopad(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def fake_dpapi(blob):
    assert blob == DPAPI_BLOB
    return MASTER_KEY


def secret_digest(kind="cookie", secret=SECRET):
    digest = hashlib.sha256((kind + "\x00" + secret).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_aad(config_id=APP_ID, api_base=API_BASE, revision="7",
              core="mihomo", kind="cookie", secret=SECRET):
    return "\n".join((app_extract.SHUTTLE_AAD_PREFIX, config_id, api_base,
                      revision, core, secret_digest(kind, secret))).encode("utf-8")


def build_profile(root, *, nodes=None, key_encoding="b64url", revision=7,
                  include_revision=True, session_config_id=APP_ID,
                  gateways=None, write_session=True, write_gateway=True,
                  write_local_state=True, write_cache=True,
                  content_key=CONTENT_KEY, kind="cookie", secret=SECRET):
    """Create one synthetic ShuttleClient profile directory."""
    base = Path(root) / "profiles" / ("p" + os.urandom(4).hex())
    base.mkdir(parents=True)

    if write_local_state:
        (base / app_extract.SHUTTLE_LOCAL_STATE_NAME).write_text(json.dumps({
            "os_crypt": {
                "encrypted_key": base64.b64encode(
                    app_extract.SHUTTLE_DPAPI_PREFIX + DPAPI_BLOB).decode("ascii"),
            },
        }), encoding="utf-8")

    key_text = content_key.hex() if key_encoding == "hex" else b64url_nopad(content_key)
    nonce = os.urandom(12)
    wrapped = app_extract.SHUTTLE_OS_CRYPT_PREFIX + nonce + AESGCM(MASTER_KEY).encrypt(
        nonce, key_text.encode("ascii"), None)

    if write_session:
        session_nonce = os.urandom(12)
        session_plain = json.dumps({
            "configId": session_config_id if session_config_id else APP_ID,
            "kind": kind, "secret": secret, "email": "fixture@example.invalid",
        }).encode("utf-8")
        encrypted = app_extract.SHUTTLE_OS_CRYPT_PREFIX + session_nonce + AESGCM(
            MASTER_KEY).encrypt(session_nonce, session_plain, None)
        (base / app_extract.SHUTTLE_SESSION_NAME).write_text(json.dumps({
            "version": 1,
            "encrypted": base64.b64encode(encrypted).decode("ascii"),
        }), encoding="utf-8")

    if write_gateway:
        if gateways is None:
            gateways = {APP_ID: {"gateway": API_BASE}}
        (base / app_extract.SHUTTLE_GATEWAY_NAME).write_text(json.dumps({
            "gateways": gateways,
        }), encoding="utf-8")

    if write_cache:
        config_id = session_config_id or APP_ID
        revision_text = str(revision) if include_revision else "unknown"
        aad = build_aad(config_id=config_id, revision=revision_text,
                        kind=kind, secret=secret)
        iv = os.urandom(12)
        plaintext = json.dumps({
            "version": 3,
            "config": {"proxies": NODES if nodes is None else nodes},
        }).encode("utf-8")
        ciphertext = AESGCM(content_key).encrypt(iv, plaintext, aad)
        cache = {
            "version": 3,
            "algorithm": "A256GCM-SAFE-STORAGE-WRAPPED-KEY",
            "core": "mihomo",
            "wrappedKey": base64.b64encode(wrapped).decode("ascii"),
            "iv": b64url_nopad(iv),
            "ciphertext": b64url_nopad(ciphertext),
        }
        if include_revision:
            cache["coreConfigRevision"] = revision
        (base / app_extract.SHUTTLE_CACHE_NAME).write_text(
            json.dumps(cache), encoding="utf-8")

    return base


def extract(base):
    return app_extract.extract_source4(base, dpapi_unprotect=fake_dpapi)


class Source4HappyPathTests(unittest.TestCase):
    def test_b64url_wrapped_key_end_to_end(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, key_encoding="b64url")
            nodes = extract(base)
        self.assertEqual([node["name"] for node in nodes],
                         ["xigua-sg-01", "xigua-us-01"])
        self.assertEqual(sorted({node["type"] for node in nodes}),
                         ["trojan", "vless"])

    def test_hex_wrapped_key_end_to_end(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, key_encoding="hex")
            nodes = extract(base)
        self.assertEqual(len(nodes), 2)

    def test_missing_revision_falls_back_to_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, include_revision=False)
            nodes = extract(base)
        self.assertEqual(len(nodes), 2)

    def test_incomplete_and_loopback_nodes_are_dropped(self):
        mixed = [
            dict(NODES[0]),
            {"name": "no-cred", "type": "trojan", "server": "203.0.113.12", "port": 443},
            {"name": "loop", "type": "trojan", "server": "127.0.0.1",
             "port": 443, "password": "pw"},
        ]
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, nodes=mixed)
            nodes = extract(base)
        self.assertEqual([node["name"] for node in nodes], ["xigua-sg-01"])

    def test_extract_app_dispatches_source4(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root)
            nodes = app_extract.extract_app(
                "source4", profile_dir=base, dpapi_unprotect=fake_dpapi)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(app_extract.APP_SOURCE_NUMBER["source4"], 3)
        self.assertIn("source4", app_extract.APP_CHOICES)


class Source4DiscoveryTests(unittest.TestCase):
    def test_newest_profile_is_selected(self):
        with tempfile.TemporaryDirectory() as root:
            old = build_profile(root)  # default nodes
            new = build_profile(root, nodes=[NODES[1]])
            old_cache = old / app_extract.SHUTTLE_CACHE_NAME
            new_cache = new / app_extract.SHUTTLE_CACHE_NAME
            os.utime(old_cache, (1_000_000, 1_000_000))
            os.utime(new_cache, (2_000_000, 2_000_000))
            nodes = app_extract.extract_source4(
                profiles_root=Path(root) / "profiles", dpapi_unprotect=fake_dpapi)
        self.assertEqual([node["name"] for node in nodes], ["xigua-us-01"])

    def test_empty_profiles_root_is_profile_missing(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(app_extract.AppExtractError) as caught:
                app_extract.extract_source4(
                    profiles_root=Path(root), dpapi_unprotect=fake_dpapi)
        self.assertEqual(caught.exception.kind, "profile_missing")

    def test_missing_profile_dir_is_profile_missing(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(app_extract.AppExtractError) as caught:
                extract(Path(root) / "does-not-exist")
        self.assertEqual(caught.exception.kind, "profile_missing")

    def test_profile_without_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            empty = Path(root) / "profiles" / "empty"
            empty.mkdir(parents=True)
            with self.assertRaises(app_extract.AppExtractError) as caught:
                app_extract.extract_source4(
                    profiles_root=Path(root) / "profiles", dpapi_unprotect=fake_dpapi)
        self.assertEqual(caught.exception.kind, "profile_missing")


class Source4ErrorKindTests(unittest.TestCase):
    def assert_kind(self, kind, base):
        with self.assertRaises(app_extract.AppExtractError) as caught:
            extract(base)
        self.assertEqual(caught.exception.kind, kind)
        return caught.exception

    def test_cache_missing(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, write_cache=False)
            self.assert_kind("cache_missing", base)

    def test_local_state_missing_is_master_key_failed(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, write_local_state=False)
            self.assert_kind("master_key_failed", base)

    def test_bad_dpapi_is_master_key_failed(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root)

            def broken(_blob):
                raise ValueError("dpapi failure with secret-free message")

            with self.assertRaises(app_extract.AppExtractError) as caught:
                app_extract.extract_source4(base, dpapi_unprotect=broken)
        self.assertEqual(caught.exception.kind, "master_key_failed")

    def test_session_missing(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, write_session=False)
            self.assert_kind("session_missing", base)

    def test_gateway_missing(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, write_gateway=False)
            self.assert_kind("gateway_missing", base)

    def test_ambiguous_gateways_is_classified(self):
        with tempfile.TemporaryDirectory() as root:
            gateways = {
                APP_ID: {"gateway": API_BASE},
                "com.shuttle.client." + "b" * 32: {"gateway": API_BASE},
            }
            base = build_profile(root, gateways=gateways)
            # Break the session so it has no configId match.
            session_path = base / app_extract.SHUTTLE_SESSION_NAME
            nonce = os.urandom(12)
            plain = json.dumps({"configId": "unknown-id", "kind": "cookie",
                                "secret": SECRET}).encode("utf-8")
            encrypted = app_extract.SHUTTLE_OS_CRYPT_PREFIX + nonce + AESGCM(
                MASTER_KEY).encrypt(nonce, plain, None)
            session_path.write_text(json.dumps({
                "version": 1,
                "encrypted": base64.b64encode(encrypted).decode("ascii"),
            }), encoding="utf-8")
            self.assert_kind("gateway_missing", base)

    def test_bad_tag_is_decrypt_failed(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root)
            cache_path = base / app_extract.SHUTTLE_CACHE_NAME
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            raw = bytearray(base64.urlsafe_b64decode(
                cache["ciphertext"] + "=" * (-len(cache["ciphertext"]) % 4)))
            raw[-1] ^= 0xFF
            cache["ciphertext"] = b64url_nopad(bytes(raw))
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            self.assert_kind("decrypt_failed", base)

    def test_bad_aad_is_decrypt_failed(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root)
            cache_path = base / app_extract.SHUTTLE_CACHE_NAME
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["coreConfigRevision"] = cache["coreConfigRevision"] + 1
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            self.assert_kind("decrypt_failed", base)

    def test_bad_wrapped_key_encoding_is_decrypt_failed(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root)
            cache_path = base / app_extract.SHUTTLE_CACHE_NAME
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["wrappedKey"] = "!!!not-base64!!!"
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            self.assert_kind("decrypt_failed", base)

    def test_empty_proxies_is_cache_empty(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, nodes=[])
            self.assert_kind("cache_empty", base)

    def test_no_complete_node(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, nodes=[
                {"name": "incomplete", "type": "trojan",
                 "server": "203.0.113.20", "port": 443},
            ])
            self.assert_kind("no_complete_node", base)


class Source4SecretHygieneTests(unittest.TestCase):
    def test_secret_and_credentials_never_in_error_text(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root, secret=SECRET)
            cache_path = base / app_extract.SHUTTLE_CACHE_NAME
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            raw = bytearray(base64.urlsafe_b64decode(
                cache["ciphertext"] + "=" * (-len(cache["ciphertext"]) % 4)))
            raw[0] ^= 0xFF
            cache["ciphertext"] = b64url_nopad(bytes(raw))
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            with self.assertRaises(app_extract.AppExtractError) as caught:
                extract(base)
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        for node in NODES:
            for key in ("password", "uuid"):
                if key in node:
                    self.assertNotIn(node[key], message)

    def test_bad_dpapi_message_never_contains_secret(self):
        with tempfile.TemporaryDirectory() as root:
            base = build_profile(root)

            def broken(_blob):
                raise RuntimeError(SECRET)

            with self.assertRaises(app_extract.AppExtractError) as caught:
                app_extract.extract_source4(base, dpapi_unprotect=broken)
        self.assertEqual(caught.exception.kind, "master_key_failed")
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
