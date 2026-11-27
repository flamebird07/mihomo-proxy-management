"""feishu_pool: master-table pool building, atomic 0600 output, CLI contract.

All tests are offline: the Feishu client is faked, node configs use
documentation-reserved addresses (192.0.2.0/24, 198.51.100.0/24) and obvious
fake credentials, and the process environment is isolated per test.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import feishu_pool  # noqa: E402
import yaml  # noqa: E402
from node_provider.feishu import FeishuError  # noqa: E402

NOW_MS = 1_700_000_000_000
NODES_TABLE_NAME = feishu_pool.NODES_TABLE_NAME

FAKE_SECRET = "fake-app-secret-value"
FAKE_TOKEN_CLI = "fake-token-cli"
FAKE_TOKEN_ENV = "fake-token-env"
FAKE_TOKEN_FILE = "fake-token-file"
FAKE_PASSWORD = "fake-node-password"


def cfg(name="节点甲", server="192.0.2.1"):
    return {"name": name, "type": "trojan", "server": server, "port": 443,
            "password": FAKE_PASSWORD}


def row(config, **field_overrides):
    fields = {
        "节点名": config.get("name", ""),
        "节点配置JSON": json.dumps(config, ensure_ascii=False),
        "地区组": "美国",
        "GPT可用": True,
        "同步时间": NOW_MS - 60_000,
    }
    fields.update(field_overrides)
    return {"fields": fields}


def env_text(include_token=True):
    lines = ["FEISHU_APP_ID=fake-app-id", "FEISHU_APP_SECRET=" + FAKE_SECRET]
    if include_token:
        lines.append("FEISHU_BITABLE_APP_TOKEN=" + FAKE_TOKEN_FILE)
    return "\n".join(lines) + "\n"


class FakePoolClient:
    def __init__(self, rows=None, *, tables=None, table_error=False,
                 record_error=False):
        self.rows = list(rows or [])
        self.tables = tables if tables is not None else [
            {"name": NODES_TABLE_NAME, "table_id": "tbl_nodes"}]
        self.table_error = table_error
        self.record_error = record_error

    def list_tables(self):
        if self.table_error:
            raise FeishuError("network_error")
        return self.tables

    def list_records(self, table_id):
        if self.record_error:
            raise FeishuError("http_500")
        return self.rows


def isolated_env():
    """A copy of the process environment without the Bitable app_token."""
    env = {key: value for key, value in os.environ.items()
           if key != "FEISHU_BITABLE_APP_TOKEN"}
    return mock.patch.dict(os.environ, env, clear=True)


class BuildPoolTests(unittest.TestCase):
    """build_pool: every row is a candidate, junk rows are dropped."""

    def test_gpt_unavailable_rows_are_included(self):
        result = feishu_pool.build_pool([row(cfg("节点甲"), GPT可用=False)],
                                        now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in result.proxies], ["节点甲"])
        self.assertEqual(result.gpt_skipped, 0)

    def test_stale_and_missing_sync_time_are_included(self):
        old = row(cfg("节点甲"), 同步时间=NOW_MS - 30 * 24 * 3600 * 1000)
        unreadable = row(cfg("节点乙", "198.51.100.7"), 同步时间=None)
        absent = row(cfg("节点丙", "192.0.2.3"))
        absent["fields"].pop("同步时间")
        result = feishu_pool.build_pool([old, unreadable, absent], now_ms=NOW_MS)
        self.assertEqual(result.selected, 3)
        self.assertEqual(result.stale_skipped, 0)

    def test_ad_node_names_are_skipped(self):
        rows = [row(cfg("★V-官网")), row(cfg("★!!!网不通 重新更新订阅"))]
        result = feishu_pool.build_pool(rows, now_ms=NOW_MS)
        self.assertEqual(result.selected, 0)
        self.assertEqual(result.ad_skipped, 2)

    def test_invalid_rows_are_skipped(self):
        bad_json = row(cfg("节点甲"), 节点配置JSON="{oops")
        no_server = row({"name": "节点乙", "type": "trojan", "port": 443})
        no_port = row({"name": "节点丙", "type": "trojan", "server": "192.0.2.3"})
        result = feishu_pool.build_pool([bad_json, no_server, no_port],
                                        now_ms=NOW_MS)
        self.assertEqual(result.selected, 0)
        self.assertEqual(result.invalid, 3)

    def test_semantic_duplicates_collapse(self):
        # Same server/port/type/password, different display names.
        rows = [row(cfg("节点甲")), row(cfg("节点甲-副本"))]
        result = feishu_pool.build_pool(rows, now_ms=NOW_MS)
        self.assertEqual(result.selected, 1)
        self.assertEqual(result.duplicates, 1)

    def test_same_name_different_server_gets_suffix(self):
        rows = [row(cfg("同名", "192.0.2.1")), row(cfg("同名", "198.51.100.7"))]
        result = feishu_pool.build_pool(rows, now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in result.proxies],
                         ["同名", "同名 [2]"])

    def test_empty_rows(self):
        result = feishu_pool.build_pool([], now_ms=NOW_MS)
        self.assertEqual(result.selected, 0)


class FindNodesTableTests(unittest.TestCase):
    def test_found_by_name(self):
        tables = [{"name": "别的表", "table_id": "t0"},
                  {"name": NODES_TABLE_NAME, "table_id": "tbl_nodes"}]
        self.assertEqual(feishu_pool.find_nodes_table(tables), "tbl_nodes")

    def test_missing_returns_none(self):
        tables = [{"name": "别的表", "table_id": "t0"}]
        self.assertIsNone(feishu_pool.find_nodes_table(tables))

    def test_same_name_without_table_id_does_not_count(self):
        tables = [{"name": NODES_TABLE_NAME}]
        self.assertIsNone(feishu_pool.find_nodes_table(tables))

    def test_entry_without_table_id_is_skipped(self):
        tables = [{"name": NODES_TABLE_NAME},
                  {"name": NODES_TABLE_NAME, "table_id": "tbl_nodes"}]
        self.assertEqual(feishu_pool.find_nodes_table(tables), "tbl_nodes")


class WritePoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feishu-pool-write-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_writes_utf8_content(self):
        path = os.path.join(self.tmp, "pool.yaml")
        feishu_pool.write_pool(path, "proxies:\n- name: 节点甲\n")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), "proxies:\n- name: 节点甲\n".encode("utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_permissions_are_0600(self):
        path = os.path.join(self.tmp, "pool.yaml")
        feishu_pool.write_pool(path, "proxies: []\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_no_temp_files_remain(self):
        path = os.path.join(self.tmp, "pool.yaml")
        feishu_pool.write_pool(path, "proxies: []\n")
        self.assertEqual(os.listdir(self.tmp), ["pool.yaml"])

    def test_overwrite_replaces_content(self):
        path = os.path.join(self.tmp, "pool.yaml")
        feishu_pool.write_pool(path, "old: 1\n")
        feishu_pool.write_pool(path, "new: 2\n")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "new: 2\n")
        self.assertEqual(os.listdir(self.tmp), ["pool.yaml"])

    def test_missing_parent_raises_and_leaves_nothing(self):
        missing = os.path.join(self.tmp, "nope")
        path = os.path.join(missing, "pool.yaml")
        with self.assertRaises(OSError):
            feishu_pool.write_pool(path, "proxies: []\n")
        self.assertFalse(os.path.exists(missing))
        leftovers = []
        for root, _dirs, files in os.walk(self.tmp):
            leftovers.extend(name for name in files
                             if name.startswith(".feishu_pool."))
        self.assertEqual(leftovers, [])


class CliTestCase(unittest.TestCase):
    """CLI runs offline with a fake client and an isolated process env."""

    def setUp(self):
        patcher = isolated_env()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="feishu-pool-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_env(self, text):
        path = os.path.join(self.tmp, "creds.env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_main(self, argv, client=None):
        client = client if client is not None else FakePoolClient(
            [row(cfg("节点甲")), row(cfg("节点乙", "198.51.100.7"))])
        recorded = []

        def factory(app_id, app_secret, app_token, log):
            recorded.append({"app_id": app_id, "app_secret": app_secret,
                             "app_token": app_token})
            return client

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = feishu_pool.main(list(argv), client_factory=factory,
                                    now_ms=NOW_MS)
        return code, buffer.getvalue(), recorded


class CliCredentialTests(CliTestCase):
    def test_unreadable_env_file_exits_2(self):
        code, stderr, recorded = self.run_main(
            ["--env-file", os.path.join(self.tmp, "absent.env"), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertTrue(any("credentials unavailable class=EnvFileError" in line
                            for line in stderr.splitlines()))
        self.assertEqual(recorded, [])

    def test_duplicate_env_key_exits_2(self):
        env = self.write_env(env_text() + "FEISHU_APP_ID=again\n")
        code, stderr, recorded = self.run_main(["--env-file", env, "--dry-run"])
        self.assertEqual(code, 2)
        self.assertTrue(any("credentials unavailable class=EnvFileError" in line
                            for line in stderr.splitlines()))
        self.assertEqual(recorded, [])

    def test_missing_app_secret_exits_2(self):
        env = self.write_env("FEISHU_APP_ID=fake-app-id\n"
                             "FEISHU_BITABLE_APP_TOKEN=" + FAKE_TOKEN_FILE + "\n")
        code, stderr, recorded = self.run_main(["--env-file", env, "--dry-run"])
        self.assertEqual(code, 2)
        self.assertTrue(any("credentials unavailable class=missing_keys" in line
                            for line in stderr.splitlines()))
        self.assertEqual(recorded, [])

    def test_token_missing_everywhere_exits_2(self):
        env = self.write_env(env_text(include_token=False))
        code, stderr, recorded = self.run_main(["--env-file", env, "--dry-run"])
        self.assertEqual(code, 2)
        self.assertTrue(any(
            "app_token_missing: FEISHU_BITABLE_APP_TOKEN" in line
            for line in stderr.splitlines()))
        self.assertEqual(recorded, [])  # the client is never constructed

    def test_token_priority_cli_flag(self):
        env = self.write_env(env_text())
        code, _, recorded = self.run_main(
            ["--env-file", env, "--dry-run", "--app-token", FAKE_TOKEN_CLI])
        self.assertEqual(code, 0)
        self.assertEqual(recorded[0]["app_token"], FAKE_TOKEN_CLI)

    def test_token_priority_process_env(self):
        env = self.write_env(env_text())
        with mock.patch.dict(os.environ,
                             {"FEISHU_BITABLE_APP_TOKEN": FAKE_TOKEN_ENV}):
            code, _, recorded = self.run_main(["--env-file", env, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(recorded[0]["app_token"], FAKE_TOKEN_ENV)

    def test_token_priority_env_file(self):
        env = self.write_env(env_text())
        code, _, recorded = self.run_main(["--env-file", env, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(recorded[0]["app_token"], FAKE_TOKEN_FILE)


class CliReadTests(CliTestCase):
    def test_missing_master_table_exits_3(self):
        env = self.write_env(env_text())
        client = FakePoolClient(tables=[{"name": "别的表", "table_id": "t1"}])
        code, stderr, _ = self.run_main(["--env-file", env, "--dry-run"], client)
        self.assertEqual(code, 3)
        self.assertTrue(any("table missing" in line
                            for line in stderr.splitlines()))

    def test_table_list_failure_exits_3(self):
        env = self.write_env(env_text())
        client = FakePoolClient(table_error=True)
        code, stderr, _ = self.run_main(["--env-file", env, "--dry-run"], client)
        self.assertEqual(code, 3)
        self.assertTrue(any("table list failed class=network_error" in line
                            for line in stderr.splitlines()))

    def test_record_read_failure_exits_3(self):
        env = self.write_env(env_text())
        client = FakePoolClient(record_error=True)
        code, stderr, _ = self.run_main(["--env-file", env, "--dry-run"], client)
        self.assertEqual(code, 3)
        self.assertTrue(any("record read failed class=http_500" in line
                            for line in stderr.splitlines()))


class CliSuccessTests(CliTestCase):
    def test_success_writes_pool(self):
        env = self.write_env(env_text())
        output = os.path.join(self.tmp, "pool.yaml")
        client = FakePoolClient([row(cfg("节点甲")),
                                 row(cfg("节点乙", "198.51.100.7"))])
        code, stderr, _ = self.run_main(["--env-file", env, "-o", output], client)
        self.assertEqual(code, 0)
        with open(output, encoding="utf-8") as handle:
            proxies = yaml.safe_load(handle)["proxies"]
        self.assertEqual(len(proxies), 2)
        for proxy in proxies:
            for key in ("name", "type", "server", "port"):
                self.assertIn(key, proxy)
        self.assertTrue(any("pool written nodes=2" in line
                            for line in stderr.splitlines()))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_output_permissions_are_0600(self):
        env = self.write_env(env_text())
        output = os.path.join(self.tmp, "pool.yaml")
        code, _, _ = self.run_main(["--env-file", env, "-o", output])
        self.assertEqual(code, 0)
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)


class CliDryRunTests(CliTestCase):
    def test_dry_run_writes_nothing(self):
        env = self.write_env(env_text())
        output = os.path.join(self.tmp, "pool.yaml")
        code, stderr, _ = self.run_main(["--env-file", env, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(output))
        self.assertEqual(os.listdir(self.tmp), ["creds.env"])
        self.assertTrue(any("dry-run" in line for line in stderr.splitlines()))

    def test_dry_run_below_threshold_exits_4(self):
        env = self.write_env(env_text())
        client = FakePoolClient([row(cfg("节点甲"))])
        code, _, _ = self.run_main(["--env-file", env, "--dry-run",
                                    "--min-nodes", "2"], client)
        self.assertEqual(code, 4)
        self.assertEqual(os.listdir(self.tmp), ["creds.env"])


class CliThresholdTests(CliTestCase):
    def test_below_threshold_keeps_previous_file(self):
        env = self.write_env(env_text())
        output = os.path.join(self.tmp, "pool.yaml")
        with open(output, "w", encoding="utf-8") as handle:
            handle.write("proxies: []\n# previous\n")
        client = FakePoolClient([row(cfg("节点甲"))])
        code, stderr, _ = self.run_main(["--env-file", env, "-o", output,
                                         "--min-nodes", "2"], client)
        self.assertEqual(code, 4)
        with open(output, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "proxies: []\n# previous\n")
        self.assertTrue(any("publish blocked" in line
                            for line in stderr.splitlines()))

    def test_min_nodes_zero_with_empty_table_publishes_empty_pool(self):
        env = self.write_env(env_text())
        output = os.path.join(self.tmp, "pool.yaml")
        code, _, _ = self.run_main(["--env-file", env, "-o", output,
                                    "--min-nodes", "0"], FakePoolClient([]))
        self.assertEqual(code, 0)
        with open(output, encoding="utf-8") as handle:
            self.assertEqual(yaml.safe_load(handle)["proxies"], [])


class CliOutputPathTests(CliTestCase):
    def test_output_parent_must_exist(self):
        env = self.write_env(env_text())
        with self.assertRaises(SystemExit) as caught:
            feishu_pool.main(["--env-file", env, "-o",
                              os.path.join(self.tmp, "missing", "pool.yaml")])
        self.assertEqual(caught.exception.code, 2)

    def test_negative_min_nodes_rejected(self):
        env = self.write_env(env_text())
        with self.assertRaises(SystemExit) as caught:
            feishu_pool.main(["--env-file", env, "--dry-run",
                              "--min-nodes", "-1"])
        self.assertEqual(caught.exception.code, 2)

    def test_output_pointing_at_a_directory_exits_5(self):
        env = self.write_env(env_text())
        target = os.path.join(self.tmp, "adir")
        os.mkdir(target)
        code, stderr, _ = self.run_main(["--env-file", env, "-o", target])
        self.assertEqual(code, 5)
        self.assertTrue(any("output write failed" in line
                            for line in stderr.splitlines()))
        self.assertEqual(os.listdir(target), [])
        self.assertEqual([name for name in os.listdir(self.tmp)
                          if name.startswith(".feishu_pool.")], [])


class NoLeakTests(CliTestCase):
    def test_stderr_never_carries_node_or_credential_values(self):
        env = self.write_env(env_text())
        output = os.path.join(self.tmp, "pool.yaml")
        rows = [row(cfg("节点甲")), row(cfg("节点乙", "198.51.100.7")),
                row(cfg("★V-官网"))]
        code, stderr, _ = self.run_main(["--env-file", env, "-o", output],
                                        FakePoolClient(rows))
        self.assertEqual(code, 0)
        forbidden = ["节点甲", "节点乙", "★V-官网", "192.0.2.1", "198.51.100.7",
                     FAKE_PASSWORD, FAKE_SECRET, FAKE_TOKEN_FILE]
        for line in stderr.splitlines():
            for value in forbidden:
                self.assertNotIn(value, line)
        with open(output, encoding="utf-8") as handle:
            document = handle.read()
        # The YAML is the product: node names and servers belong there.
        self.assertIn("节点甲", document)
        self.assertIn("192.0.2.1", document)


if __name__ == "__main__":
    unittest.main()
