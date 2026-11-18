"""node_provider --collect: two extraction channels, merge, dry-run, writes."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import yaml  # noqa: E402

from node_provider import app_extract, collect, schema  # noqa: E402
from node_provider.logutil import Logger  # noqa: E402
from node_provider.runner import Settings, run_collect  # noqa: E402
from node_provider.subscription import SubscriptionError  # noqa: E402

ENV = {
    "SUBSCRIPTION_URL_1": "https://one.invalid/sub",
    "SUBSCRIPTION_URL_2": "https://two.invalid/sub",
    "SUBSCRIPTION_URL_3": "https://three.invalid/sub",
}
YAML_ONE = """
proxies:
  - name: us-01
    type: anytls
    server: 1.1.1.1
    port: 443
    password: pw
"""
URI_ONE = "trojan://pw@2.2.2.2:443#sg-01\n"


def fetcher_map(mapping):
    def fetch(url, ua, timeout):
        if url not in mapping:
            raise SubscriptionError("boom", "http")
        return mapping[url]
    return fetch


class FakeReader:
    def __init__(self, buffers):
        self.buffers = list(buffers)
        self.verified = False
        self.closed = False

    def verify(self):
        self.verified = True

    def iter_buffers(self):
        yield from self.buffers

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, old_rows=None):
        self.old_rows = list(old_rows or [])
        self.calls = []
        self.tables = [
            {"name": schema.NODES_TABLE_NAME, "table_id": "tbl-n"},
            {"name": schema.VALID_TABLE_NAME, "table_id": "tbl-v"},
        ]

    def list_tables(self):
        return list(self.tables)

    def list_fields(self, table_id):
        spec = schema.NODES_TABLE if table_id == "tbl-n" else schema.VALID_TABLE
        return [{"field_id": f"fld-{name}", "field_name": name, "type": ftype,
                 "property": prop} for name, ftype, prop in spec["fields"]]

    def list_records(self, table_id):
        self.calls.append(("list_records", table_id))
        return list(self.old_rows) if table_id == "tbl-n" else []

    def batch_create(self, table_id, records):
        self.calls.append(("batch_create", table_id, records))
        return len(records)

    def batch_delete(self, table_id, record_ids):
        self.calls.append(("batch_delete", table_id, record_ids))
        return len(record_ids)

    def create_table(self, *args):  # pragma: no cover - not expected
        raise AssertionError("unexpected create_table")

    def create_field(self, *args):  # pragma: no cover - not expected
        raise AssertionError("unexpected create_field")

    def update_field(self, *args):  # pragma: no cover - not expected
        raise AssertionError("unexpected update_field")


class ExplodingClient:
    def __getattr__(self, name):
        raise AssertionError(f"client.{name} must not be called in dry-run")


def settings(**kwargs):
    base = dict(dry_run=True)
    base.update(kwargs)
    return Settings(**base)


class SubscriptionChannelTests(unittest.TestCase):
    def test_subscription_records_have_source_number_and_defaults(self):
        env = {"SUBSCRIPTION_URL_1": ENV["SUBSCRIPTION_URL_1"],
               "SUBSCRIPTION_URL_3": ENV["SUBSCRIPTION_URL_3"]}
        result = collect.collect(
            env, fetcher=fetcher_map({ENV["SUBSCRIPTION_URL_1"]: YAML_ONE,
                                      ENV["SUBSCRIPTION_URL_3"]: URI_ONE}),
        )
        self.assertEqual(result.collected, 2)
        self.assertEqual(result.failed, [])
        by_name = {node.config["name"]: node for node in result.nodes}
        self.assertEqual(by_name["us-01"].source, "飞兔")
        self.assertEqual(by_name["us-01"].source_number, 1)
        self.assertEqual(by_name["sg-01"].source, "CoffeeCloud")
        self.assertEqual(by_name["sg-01"].source_number, 3)

        records = collect.build_records(result.nodes, 1_700_000_000_000)
        fields = records[0]["fields"]
        # Date measurement is left unset; every other column is present.
        self.assertEqual(set(schema.field_names("nodes")) - set(fields),
                         {"地区检测时间"})
        self.assertEqual(fields["同步时间"], 1_700_000_000_000)
        self.assertEqual(fields["延迟ms"], -1)
        self.assertEqual(fields["存活"], False)
        self.assertEqual(fields["地区组"], "未知")
        self.assertEqual(fields["测速目标"], "")

    def test_empty_body_is_failed_not_raised(self):
        result = collect.collect(
            ENV, fetcher=fetcher_map({ENV["SUBSCRIPTION_URL_1"]: YAML_ONE,
                                      ENV["SUBSCRIPTION_URL_2"]: "   \n"}),
        )
        self.assertEqual(result.collected, 1)
        self.assertIn("subscription:星辰", result.failed)
        self.assertEqual(result.nodes[0].source, "飞兔")

    def test_http_403_is_failed_not_raised(self):
        def fetch(url, ua, timeout):
            if url == ENV["SUBSCRIPTION_URL_1"]:
                return YAML_ONE
            raise SubscriptionError("HTTP error 403 (URL redacted)", "http")
        result = collect.collect(ENV, fetcher=fetch)
        self.assertEqual(result.collected, 1)
        self.assertEqual(len(result.failed), 2)

    def test_all_subscription_failures_yield_empty_result(self):
        result = collect.collect(ENV, fetcher=fetcher_map({}))
        self.assertEqual(result.collected, 0)
        self.assertEqual(len(result.failed), 3)

    def test_transient_network_error_retried_then_succeeds(self):
        calls = []

        def fetch(url, ua, timeout):
            calls.append(url)
            if url == ENV["SUBSCRIPTION_URL_1"]:
                if len([c for c in calls if c == url]) <= 2:
                    raise SubscriptionError("network error (URL redacted)", "network")
                return YAML_ONE
            raise SubscriptionError("HTTP error 403 (URL redacted)", "http")

        with patch.object(collect.time, "sleep") as sleep:
            result = collect.collect(ENV, fetcher=fetch)
        self.assertEqual(result.collected, 1)
        self.assertEqual(len([c for c in calls if c == ENV["SUBSCRIPTION_URL_1"]]), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("subscription:飞兔", result.per_channel)

    def test_persistent_network_error_fails_after_bounded_retries(self):
        calls = []

        def fetch(url, ua, timeout):
            calls.append(url)
            raise SubscriptionError("network error (URL redacted)", "network")

        with patch.object(collect.time, "sleep"):
            result = collect.collect(ENV, fetcher=fetch)
        self.assertEqual(result.collected, 0)
        self.assertEqual(len(calls), 3 * (collect.SUBSCRIPTION_RETRIES + 1))
        self.assertEqual(len(result.failed), 3)

    def test_http_error_is_not_retried(self):
        calls = []

        def fetch(url, ua, timeout):
            calls.append(url)
            if url == ENV["SUBSCRIPTION_URL_1"]:
                return YAML_ONE
            raise SubscriptionError("HTTP error 403 (URL redacted)", "http")

        with patch.object(collect.time, "sleep") as sleep:
            result = collect.collect(ENV, fetcher=fetch)
        self.assertEqual(result.collected, 1)
        self.assertEqual(len(calls), 3)
        sleep.assert_not_called()


class AppChannelTests(unittest.TestCase):
    def test_source1_reader_recovers_complete_nodes_and_labels_source(self):
        payload = json.dumps({"proxies": [
            {"name": "app-us", "type": "trojan", "server": "9.9.9.9",
             "port": 443, "password": "pw"},
            {"name": "incomplete", "type": "trojan", "server": "8.8.8.8", "port": 443},
            {"name": "loopback", "type": "trojan", "server": "127.0.0.1",
             "port": 443, "password": "pw"},
        ]}).encode("utf-8")
        reader = FakeReader([b"noise", b"prefix " + payload + b" trailing"])
        result = collect.collect({}, from_app=["source1"], source1_reader=reader)
        self.assertEqual(result.collected, 1)
        self.assertEqual(result.nodes[0].source, "飞兔")
        self.assertEqual(result.nodes[0].source_number, 1)
        self.assertEqual(result.nodes[0].config["name"], "app-us")
        self.assertTrue(reader.verified)
        self.assertFalse(reader.closed)  # caller-owned reader is not closed

    def test_source1_encrypted_frame_path(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = os.urandom(32)
        magic = b"ATLS"
        nonce = os.urandom(16)
        payload = json.dumps({"proxies": [
            {"name": "enc-us", "type": "trojan", "server": "6.6.6.6",
             "port": 443, "password": "pw"},
        ]}).encode("utf-8")
        ciphertext = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor().update(payload)
        frame = magic + nonce + ciphertext + b"\0" * 64
        nodes = app_extract.extract_source1(
            reader=FakeReader([frame]), key=key, magic=magic,
        )
        self.assertEqual([node["name"] for node in nodes], ["enc-us"])

    def test_source1_no_nodes_is_channel_failure(self):
        reader = FakeReader([b"nothing useful here"])
        result = collect.collect({}, from_app=["source1"], source1_reader=reader)
        self.assertEqual(result.collected, 0)
        self.assertIn("app:source1", result.failed)

    def _make_vault(self, root, nodes, hostname="test-host"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        base = Path(root) / "NinjaDesktop"
        (base / "subscription/default").mkdir(parents=True)
        key = os.urandom(32)
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(app_extract.derive_vault_kek(hostname)).encrypt(
            nonce, key, None)
        (base / "vault.key.enc").write_bytes(wrapped)
        plaintext = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
        nonce2 = os.urandom(12)
        encrypted = b"NJVT\x01" + nonce2 + AESGCM(key).encrypt(nonce2, plaintext, None)
        (base / "subscription/default/config.enc").write_bytes(encrypted)
        return base

    def test_source3_vault_decrypts_and_labels_source(self):
        nodes = [{"name": "ninja-us", "type": "ninja", "server": "7.7.7.7",
                  "port": 443, "password": "pw"}]
        with tempfile.TemporaryDirectory() as root:
            base = self._make_vault(root, nodes)
            extracted = app_extract.extract_source3(base, hostname="test-host")
        self.assertEqual(extracted[0]["name"], "ninja-us")
        self.assertEqual(extracted[0]["type"], "ninja")

    def test_source3_bad_vault_is_classified(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "NinjaDesktop"
            (base / "subscription/default").mkdir(parents=True)
            (base / "vault.key.enc").write_bytes(b"garbage")
            (base / "subscription/default/config.enc").write_bytes(b"garbage")
            with self.assertRaises(app_extract.AppExtractError) as caught:
                app_extract.extract_source3(base, hostname="test-host")
        self.assertEqual(caught.exception.kind, "vault_decrypt_failed")

    def test_source3_failure_does_not_block_source1(self):
        payload = json.dumps({"proxies": [
            {"name": "app-us", "type": "trojan", "server": "9.9.9.9",
             "port": 443, "password": "pw"},
        ]}).encode("utf-8")
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "NinjaDesktop"  # missing files
            result = collect.collect(
                {}, from_app=["source1", "source3"],
                source1_reader=FakeReader([payload]), vault_dir=base,
            )
        self.assertEqual(result.collected, 1)
        self.assertIn("app:source3", result.failed)
        self.assertNotIn("app:source1", result.failed)


class MergeTests(unittest.TestCase):
    def test_dual_channel_semantic_duplicate_kept_once(self):
        app_node = {"name": "renamed", "type": "anytls", "server": "1.1.1.1",
                    "port": 443, "password": "pw"}
        payload = json.dumps({"proxies": [app_node]}).encode("utf-8")
        result = collect.collect(
            ENV,
            fetcher=fetcher_map({ENV["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            from_app=["source1"], source1_reader=FakeReader([payload]),
        )
        self.assertEqual(result.collected, 1)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.nodes[0].source, "飞兔")
        self.assertEqual(result.nodes[0].source_number, 1)

    def test_same_name_different_node_gets_suffix(self):
        app_node = {"name": "us-01", "type": "anytls", "server": "5.5.5.5",
                    "port": 443, "password": "different"}
        payload = json.dumps({"proxies": [app_node]}).encode("utf-8")
        result = collect.collect(
            ENV,
            fetcher=fetcher_map({ENV["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            from_app=["source1"], source1_reader=FakeReader([payload]),
        )
        self.assertEqual(sorted(node.config["name"] for node in result.nodes),
                         ["us-01", "us-01 [2]"])


class RunCollectTests(unittest.TestCase):
    def test_dry_run_never_touches_client_or_disk(self):
        code = run_collect(
            settings(), ExplodingClient(), Logger(),
            env_values=ENV,
            fetcher=fetcher_map({ENV["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            now_ms=1_700_000_000_000,
        )
        self.assertEqual(code, 0)

    def test_no_nodes_is_exit_3(self):
        logger = Logger()
        code = run_collect(settings(), None, logger, env_values=ENV,
                           fetcher=fetcher_map({}))
        self.assertEqual(code, 3)
        self.assertTrue(any("no nodes collected" in line for line in logger.lines))

    def test_write_replaces_total_table_only(self):
        old = [{"record_id": "old1", "fields": {"节点名": "gone"}}]
        client = FakeClient(old_rows=old)
        code = run_collect(
            settings(dry_run=False), client, Logger(),
            env_values=ENV,
            fetcher=fetcher_map({ENV["SUBSCRIPTION_URL_1"]: YAML_ONE}),
            now_ms=1_700_000_000_000,
        )
        self.assertEqual(code, 0)
        creates = [call for call in client.calls if call[0] == "batch_create"]
        self.assertEqual([call[1] for call in creates], ["tbl-n"])
        fields = creates[0][2][0]["fields"]
        self.assertEqual(fields["来源"], "飞兔")
        self.assertEqual(fields["源编号"], 1)
        deletes = [call for call in client.calls if call[0] == "batch_delete"]
        self.assertEqual(deletes[0][2], ["old1"])

    def test_secrets_and_node_passwords_never_logged(self):
        logger = Logger()
        run_collect(
            settings(), None, logger,
            env_values=ENV,
            fetcher=fetcher_map({ENV["SUBSCRIPTION_URL_1"]: YAML_ONE}),
        )
        joined = "\n".join(logger.lines)
        self.assertNotIn("https://one.invalid", joined)
        self.assertNotIn("password", joined)


if __name__ == "__main__":
    unittest.main()
