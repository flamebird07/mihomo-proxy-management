"""node_provider: orchestration exit codes, keep-pool, dry-run, secrets."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_provider import builder  # noqa: E402
from node_provider import __main__ as cli  # noqa: E402
from node_provider import runner  # noqa: E402
from node_provider.feishu import FeishuError  # noqa: E402
from node_provider.logutil import Logger  # noqa: E402

NOW_MS = 1_700_000_000_000


def cfg(name="us-01", server="1.1.1.1"):
    return {"name": name, "type": "trojan", "server": server, "port": 443,
            "password": "NODE-SECRET-PW"}


def row(config):
    return {"fields": {
        "节点名": config["name"],
        "节点配置JSON": json.dumps(config, ensure_ascii=False),
        "地区组": "美国",
        "GPT可用": True,
        "同步时间": NOW_MS - 60_000,
    }}


class FakeClient:
    def __init__(self, rows=None, *, table_error=False, record_error=False,
                 table_name=builder.schema.VALID_TABLE_NAME, tables=None):
        self.rows = list(rows or [])
        self.table_error = table_error
        self.record_error = record_error
        self.table_name = table_name
        self.tables = tables

    def list_tables(self):
        if self.table_error:
            raise FeishuError("network_error")
        if self.tables is not None:
            return self.tables
        return [{"name": self.table_name, "table_id": "tbl_valid"}]

    def list_records(self, table_id):
        if self.record_error:
            raise FeishuError("http_500")
        return self.rows


def settings(**kwargs):
    base = dict(output=None, min_nodes=1, dry_run=False)
    base.update(kwargs)
    return runner.Settings(**base)


def run(settings_obj, rows=None, **client_kwargs):
    logger = Logger()
    code = runner.run(settings_obj, FakeClient(rows, **client_kwargs), logger,
                      now_ms=NOW_MS)
    return code, logger


class RunTests(unittest.TestCase):
    def test_dry_run_zero_write(self):
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "provider.yaml")
            code, logger = run(settings(output=output, dry_run=True),
                               [row(cfg("a")), row(cfg("b", "2.2.2.2"))])
            self.assertEqual(code, runner.EXIT_OK)
            self.assertFalse(os.path.exists(output))
            self.assertEqual(os.listdir(root), [])
            self.assertTrue(any("dry-run" in line for line in logger.lines))

    def test_dry_run_below_threshold(self):
        with tempfile.TemporaryDirectory() as root:
            code, _ = run(settings(output=os.path.join(root, "p.yaml"),
                                   dry_run=True, min_nodes=2),
                          [row(cfg("a"))])
            self.assertEqual(code, runner.EXIT_THRESHOLD)

    def test_min_nodes_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "provider.yaml")
            with open(output, "w", encoding="utf-8") as handle:
                handle.write("proxies: []\n# previous\n")
            code, logger = run(settings(output=output, min_nodes=2),
                               [row(cfg("a"))])
            self.assertEqual(code, runner.EXIT_THRESHOLD)
            with open(output, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "proxies: []\n# previous\n")
            self.assertTrue(any("publish blocked" in line for line in logger.lines))

    def test_success_writes_provider(self):
        import yaml
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "provider.yaml")
            code, _ = run(settings(output=output),
                          [row(cfg("a")), row(cfg("b", "2.2.2.2"))])
            self.assertEqual(code, runner.EXIT_OK)
            with open(output, encoding="utf-8") as handle:
                document = yaml.safe_load(handle)
            self.assertEqual([p["name"] for p in document["proxies"]], ["a", "b"])

    def test_partial_invalid_writes_and_exits_5(self):
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "provider.yaml")
            bad = {"fields": {"节点配置JSON": "{oops", "GPT可用": True,
                              "同步时间": NOW_MS}}
            code, _ = run(settings(output=output), [row(cfg("a")), bad])
            self.assertEqual(code, runner.EXIT_PARTIAL)
            self.assertTrue(os.path.exists(output))

    def test_all_valid_relaxes_gpt_filter(self):
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "provider.yaml")
            no_gpt = row(cfg("b", "2.2.2.2"))
            no_gpt["fields"]["GPT可用"] = False
            strict, _ = run(settings(output=output), [row(cfg("a")), no_gpt])
            self.assertEqual(strict, runner.EXIT_OK)
            with open(output, encoding="utf-8") as handle:
                import yaml
                self.assertEqual(len(yaml.safe_load(handle)["proxies"]), 1)

            relaxed, _ = run(settings(output=output, all_valid=True),
                             [row(cfg("a")), no_gpt])
            self.assertEqual(relaxed, runner.EXIT_OK)
            with open(output, encoding="utf-8") as handle:
                import yaml
                self.assertEqual(len(yaml.safe_load(handle)["proxies"]), 2)

    def test_table_list_failure_exit_3(self):
        code, logger = run(settings(output="/unused/p.yaml"), table_error=True)
        self.assertEqual(code, runner.EXIT_READ)
        self.assertTrue(any("table list failed" in line for line in logger.lines))

    def test_record_read_failure_exit_3(self):
        code, _ = run(settings(output="/unused/p.yaml"), record_error=True)
        self.assertEqual(code, runner.EXIT_READ)

    def test_missing_table_exit_3(self):
        code, logger = run(settings(output="/unused/p.yaml", table_name="不存在"),
                           tables=[{"name": "别的表", "table_id": "t1"}])
        self.assertEqual(code, runner.EXIT_READ)
        self.assertTrue(any("table missing" in line for line in logger.lines))

    def test_node_credentials_never_logged(self):
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "provider.yaml")
            code, logger = run(settings(output=output), [row(cfg("a"))])
            self.assertEqual(code, runner.EXIT_OK)
            joined = "\n".join(logger.lines)
            self.assertNotIn("NODE-SECRET-PW", joined)

    def test_below_threshold_zero_rows_keeps_no_file(self):
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "provider.yaml")
            code, _ = run(settings(output=output), [])
            self.assertEqual(code, runner.EXIT_THRESHOLD)
            self.assertFalse(os.path.exists(output))


class CredentialAndCliTests(unittest.TestCase):
    def test_credentials_missing_secret_not_logged(self):
        with tempfile.TemporaryDirectory() as root:
            env = os.path.join(root, "creds.env")
            with open(env, "w", encoding="utf-8") as handle:
                handle.write("FEISHU_APP_SECRET=SUPER-SECRET-VALUE\n")
            logger = Logger()
            self.assertIsNone(cli._credentials(env, logger))
            self.assertNotIn("SUPER-SECRET-VALUE", "\n".join(logger.lines))

    def test_credentials_unreadable(self):
        logger = Logger()
        self.assertIsNone(cli._credentials("no-such-file.env", logger))
        self.assertTrue(any("credentials unavailable" in line
                            for line in logger.lines))

    def test_logger_scrubs_registered_secret(self):
        logger = Logger()
        logger.add_secret("TOP-SECRET-XYZ")
        logger.info("token is TOP-SECRET-XYZ")
        self.assertNotIn("TOP-SECRET-XYZ", "\n".join(logger.lines))
        self.assertIn("<redacted>", "\n".join(logger.lines))

    def test_main_requires_output_without_dry_run(self):
        with tempfile.TemporaryDirectory() as root:
            env = os.path.join(root, "creds.env")
            with open(env, "w", encoding="utf-8") as handle:
                handle.write("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret-value\nFEISHU_BITABLE_APP_TOKEN=fake-app-token-cli\n")
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--env-file", env])
            self.assertEqual(caught.exception.code, 2)

    def test_main_output_directory_must_exist(self):
        with tempfile.TemporaryDirectory() as root:
            env = os.path.join(root, "creds.env")
            with open(env, "w", encoding="utf-8") as handle:
                handle.write("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret-value\nFEISHU_BITABLE_APP_TOKEN=fake-app-token-cli\n")
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--env-file", env, "-o",
                          os.path.join(root, "missing", "p.yaml")])
            self.assertEqual(caught.exception.code, 2)

    def test_main_end_to_end_with_injected_client(self):
        import yaml
        with tempfile.TemporaryDirectory() as root:
            env = os.path.join(root, "creds.env")
            with open(env, "w", encoding="utf-8") as handle:
                handle.write("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret-value\nFEISHU_BITABLE_APP_TOKEN=fake-app-token-cli\n")
            output = os.path.join(root, "provider.yaml")
            created = []

            def factory(app_id, app_secret, app_token):
                created.append((app_id, app_secret, app_token))
                return FakeClient([row(cfg("a"))])

            code = cli.main(["--env-file", env, "-o", output],
                            client_factory=factory, now_ms=NOW_MS)
            self.assertEqual(code, runner.EXIT_OK)
            self.assertEqual(created[0][0], "id")
            with open(output, encoding="utf-8") as handle:
                self.assertEqual(len(yaml.safe_load(handle)["proxies"]), 1)

    def test_main_dry_run_needs_no_output(self):
        with tempfile.TemporaryDirectory() as root:
            env = os.path.join(root, "creds.env")
            with open(env, "w", encoding="utf-8") as handle:
                handle.write("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret-value\nFEISHU_BITABLE_APP_TOKEN=fake-app-token-cli\n")
            code = cli.main(["--env-file", env, "--dry-run"],
                            client_factory=lambda *a: FakeClient([row(cfg("a"))]),
                            now_ms=NOW_MS)
            self.assertEqual(code, runner.EXIT_OK)


class CollectCliTests(unittest.TestCase):
    def _env(self, root):
        env = os.path.join(root, "creds.env")
        with open(env, "w", encoding="utf-8") as handle:
            handle.write("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret-value\nFEISHU_BITABLE_APP_TOKEN=fake-app-token-cli\n")
        return env

    def test_collect_requires_a_channel(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["--collect", "--dry-run"])
        self.assertEqual(caught.exception.code, 2)

    def test_collect_rejects_output(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["--collect", "--sources-env", "x.env", "-o", "p.yaml"])
        self.assertEqual(caught.exception.code, 2)

    def test_from_app_requires_collect(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["--from-app", "source1", "--dry-run"])
        self.assertEqual(caught.exception.code, 2)

    def test_unknown_app_channel_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["--collect", "--from-app", "source9", "--dry-run"])
        self.assertEqual(caught.exception.code, 2)

    def test_collect_dry_run_allows_missing_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            lock = os.path.join(root, "p.lock")
            with patch.object(cli, "run_collect", return_value=0) as runner_call:
                code = cli.main([
                    "--collect", "--from-app", "source1", "--dry-run",
                    "--env-file", os.path.join(root, "missing.env"),
                    "--lock-file", lock,
                ])
        self.assertEqual(code, 0)
        self.assertIsNone(runner_call.call_args.args[1])
        self.assertFalse(os.path.exists(lock))

    def test_collect_lock_busy(self):
        from node_provider.lock import FileLock
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "p.lock")
            held = FileLock(lock_path)
            held.acquire()
            try:
                code = cli.main([
                    "--collect", "--sources-env", os.path.join(root, "s.env"),
                    "--dry-run", "--env-file", self._env(root),
                    "--lock-file", lock_path,
                ])
            finally:
                held.release()
        self.assertEqual(code, runner.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
