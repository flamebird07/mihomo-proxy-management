"""node_provider / node_ops: the Bitable app_token has no built-in default.

Covered contracts:
  * envfile.resolve_app_token priority: --app-token > process env
    FEISHU_BITABLE_APP_TOKEN > env-file key; blank candidates fall through;
    missing everywhere -> ""
  * DEFAULT_APP_TOKEN is None in both runners and neither argparse parser
    carries a table address: the address belongs to the deploying machine
  * credentials present but the table address configured nowhere -> exit 2
    with the key name only (app_token_missing) and no client is built
  * the resolved token is the one the client receives; a token value never
    reaches stdout / stderr / the log file
  * a credential-less ``--collect --dry-run`` is still allowed (unchanged)

Every token value here is a runtime-generated fake. No network: Feishu clients
are stubs.
"""

import contextlib
import io
import json
import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_provider import __main__ as provider_cli  # noqa: E402
from node_provider import builder  # noqa: E402
from node_provider import envfile as provider_envfile  # noqa: E402
from node_provider import runner as provider_runner  # noqa: E402
from node_ops import __main__ as ops_cli  # noqa: E402
from node_ops import envfile as ops_envfile  # noqa: E402
from node_ops import runner as ops_runner  # noqa: E402

ENV_KEY = "FEISHU_BITABLE_APP_TOKEN"
NOW_MS = 1_700_000_000_000
PACKAGES = (("node_provider", provider_envfile), ("node_ops", ops_envfile))


def fake_token():
    return "fake-app-token-" + secrets.token_hex(8)


def clean_environ(**extra):
    """The process environment without the token key (plus optional extras)."""
    environ = {key: value for key, value in os.environ.items() if key != ENV_KEY}
    environ.update(extra)
    return environ


def write_env(path, *, creds=True, token=None):
    lines = ["FEISHU_APP_ID=fake-cli-id", "FEISHU_APP_SECRET=fake-app-secret-value"] if creds else []
    if token is not None:
        lines.append(f"{ENV_KEY}={token}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


class ResolverTests(unittest.TestCase):
    def test_priority_cli_then_process_env_then_file(self):
        for name, module in PACKAGES:
            with self.subTest(package=name):
                cli, process, filed = fake_token(), fake_token(), fake_token()
                self.assertEqual(
                    module.resolve_app_token(cli, {ENV_KEY: filed}, {ENV_KEY: process}), cli)
                self.assertEqual(
                    module.resolve_app_token(None, {ENV_KEY: filed}, {ENV_KEY: process}), process)
                self.assertEqual(module.resolve_app_token(None, {ENV_KEY: filed}, {}), filed)

    def test_blank_candidates_fall_through_and_result_is_stripped(self):
        for name, module in PACKAGES:
            with self.subTest(package=name):
                filed = fake_token()
                self.assertEqual(
                    module.resolve_app_token("   ", {" " + ENV_KEY: "", ENV_KEY: f"  {filed}  "},
                                             {ENV_KEY: "\t"}),
                    filed)
                cli = fake_token()
                self.assertEqual(module.resolve_app_token(f" {cli} ", {}, {}), cli)

    def test_missing_everywhere_returns_empty(self):
        for name, module in PACKAGES:
            with self.subTest(package=name):
                self.assertEqual(module.resolve_app_token(None, None, None), "")
                self.assertEqual(module.resolve_app_token("", {}, {}), "")

    def test_only_the_exact_key_is_read(self):
        for name, module in PACKAGES:
            with self.subTest(package=name):
                values = {"FEISHU_APP_TOKEN": fake_token(), "APP_TOKEN": fake_token()}
                environ = {"FEISHU_APP_TOKEN": fake_token()}
                self.assertEqual(module.resolve_app_token(None, values, environ), "")
                self.assertEqual(module.APP_TOKEN_ENV_KEY, ENV_KEY)


class NoBuiltinTableAddressTests(unittest.TestCase):
    def test_runner_default_attribute_is_none(self):
        for module in (provider_runner, ops_runner):
            self.assertIsNone(module.DEFAULT_APP_TOKEN)

    def test_ops_settings_default_token_is_empty(self):
        self.assertEqual(ops_runner.Settings().app_token, "")

    def test_parsers_carry_no_table_address(self):
        for build in (provider_cli.build_parser, ops_cli.build_parser):
            with self.subTest(parser=build.__module__):
                text = build().format_help()
                self.assertIn(ENV_KEY, text)
                self.assertNotIn("default: project app", text)
                self.assertIsNone(build().parse_args([]).app_token)


# ---- node_provider ---------------------------------------------------------


def node_config(name="us-01", server="1.1.1.1"):
    return {"name": name, "type": "trojan", "server": server, "port": 443,
            "password": "NODE-SECRET-PW"}


def valid_row(config):
    return {"fields": {
        "节点名": config["name"],
        "节点配置JSON": json.dumps(config, ensure_ascii=False),
        "地区组": "美国",
        "GPT可用": True,
        "同步时间": NOW_MS - 60_000,
    }}


class FakeClient:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def list_tables(self):
        self.calls.append("list_tables")
        return [{"name": builder.schema.VALID_TABLE_NAME, "table_id": "tbl_valid"}]

    def list_records(self, table_id):
        self.calls.append("list_records")
        return self.rows


class ClientRecorder:
    """Stands in for the client factory / FeishuClient; records what it was built with."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.built = []
        self.clients = []

    def __call__(self, app_id, app_secret, app_token, log=None):
        self.built.append((app_id, app_secret, app_token))
        client = FakeClient(self.rows)
        self.clients.append(client)
        return client


class ProviderCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.output = os.path.join(self.root, "provider.yaml")
        self.recorder = ClientRecorder([valid_row(node_config("a"))])
        patcher = patch.dict(os.environ, clean_environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, name="creds.env", **kwargs):
        return write_env(os.path.join(self.root, name), **kwargs)

    def test_env_file_token_reaches_the_client(self):
        token = fake_token()
        code = provider_cli.main(["--env-file", self.env(token=token), "-o", self.output],
                                 client_factory=self.recorder, now_ms=NOW_MS)
        self.assertEqual(code, provider_runner.EXIT_OK)
        self.assertEqual([item[2] for item in self.recorder.built], [token])
        self.assertTrue(os.path.exists(self.output))

    def test_process_env_token_beats_env_file(self):
        process, filed = fake_token(), fake_token()
        with patch.dict(os.environ, {ENV_KEY: process}, clear=False):
            code = provider_cli.main(["--env-file", self.env(token=filed), "-o", self.output],
                                     client_factory=self.recorder, now_ms=NOW_MS)
        self.assertEqual(code, provider_runner.EXIT_OK)
        self.assertEqual([item[2] for item in self.recorder.built], [process])

    def test_cli_flag_beats_process_env_and_file(self):
        cli, process, filed = fake_token(), fake_token(), fake_token()
        with patch.dict(os.environ, {ENV_KEY: process}, clear=False):
            code = provider_cli.main(["--app-token", cli, "--env-file", self.env(token=filed),
                                      "-o", self.output],
                                     client_factory=self.recorder, now_ms=NOW_MS)
        self.assertEqual(code, provider_runner.EXIT_OK)
        self.assertEqual([item[2] for item in self.recorder.built], [cli])

    def test_missing_token_exits_2_without_building_a_client(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = provider_cli.main(["--env-file", self.env(), "-o", self.output],
                                     client_factory=self.recorder, now_ms=NOW_MS)
        self.assertEqual(code, provider_runner.EXIT_USAGE)
        self.assertEqual(self.recorder.built, [])
        self.assertIn("app_token_missing: FEISHU_BITABLE_APP_TOKEN", err.getvalue())
        self.assertFalse(os.path.exists(self.output))

    def test_dry_run_needs_the_token_too(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = provider_cli.main(["--env-file", self.env(), "--dry-run"],
                                     client_factory=self.recorder, now_ms=NOW_MS)
        self.assertEqual(code, provider_runner.EXIT_USAGE)
        self.assertEqual(self.recorder.built, [])
        self.assertIn("app_token_missing", err.getvalue())

    def test_token_value_never_reaches_output(self):
        secret = fake_token()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            provider_cli.main(["--env-file", self.env(token=secret), "-o", self.output],
                              client_factory=self.recorder, now_ms=NOW_MS)
            provider_cli.main(["--env-file", self.env("creds-only.env"), "--dry-run"],
                              client_factory=self.recorder, now_ms=NOW_MS)
        self.assertEqual(len(self.recorder.built), 1)
        for stream in (out, err):
            self.assertNotIn(secret, stream.getvalue())

    def test_collect_dry_run_without_credentials_is_still_allowed(self):
        lock = os.path.join(self.root, "p.lock")
        with patch.object(provider_cli, "run_collect", return_value=0) as run_collect:
            code = provider_cli.main([
                "--collect", "--from-app", "source1", "--dry-run",
                "--env-file", os.path.join(self.root, "missing.env"),
                "--lock-file", lock,
            ])
        self.assertEqual(code, 0)
        self.assertIsNone(run_collect.call_args.args[1])
        self.assertEqual(self.recorder.built, [])
        self.assertFalse(os.path.exists(lock))


# ---- node_ops --------------------------------------------------------------


class OpsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.lock = os.path.join(self.root, "ops.lock")
        self.log = os.path.join(self.root, "ops.log")
        self.recorder = ClientRecorder()
        patcher = patch.dict(os.environ, clean_environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **kwargs):
        return write_env(os.path.join(self.root, "creds.env"), **kwargs)

    def run_ops(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(ops_cli, "FeishuClient", self.recorder), \
                patch.object(ops_cli, "run", return_value=ops_runner.RunSummary()) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ops_cli.main(["--lock-file", self.lock, "--log-file", self.log] + argv)
        return code, out.getvalue(), err.getvalue(), run

    def test_env_file_token_reaches_the_client(self):
        token = fake_token()
        code, _, _, run = self.run_ops(["--env-file", self.env(token=token), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual([item[2] for item in self.recorder.built], [token])
        self.assertEqual(run.call_args.kwargs["client"], self.recorder.clients[0])
        self.assertFalse(os.path.exists(self.lock))

    def test_cli_flag_beats_env_file(self):
        cli, filed = fake_token(), fake_token()
        self.run_ops(["--app-token", cli, "--env-file", self.env(token=filed), "--dry-run"])
        self.assertEqual([item[2] for item in self.recorder.built], [cli])

    def test_process_env_token_beats_env_file(self):
        process, filed = fake_token(), fake_token()
        with patch.dict(os.environ, {ENV_KEY: process}, clear=False):
            self.run_ops(["--env-file", self.env(token=filed), "--dry-run"])
        self.assertEqual([item[2] for item in self.recorder.built], [process])

    def test_missing_token_exits_2_without_building_a_client(self):
        code, _, err, run = self.run_ops(["--env-file", self.env(), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertEqual(self.recorder.built, [])
        run.assert_not_called()
        self.assertIn("error: app_token_missing: FEISHU_BITABLE_APP_TOKEN", err)
        self.assertFalse(os.path.exists(self.lock))

    def test_missing_token_blocks_ensure_tables_too(self):
        code, _, err, run = self.run_ops(["--env-file", self.env(), "--ensure-tables"])
        self.assertEqual(code, 2)
        self.assertEqual(self.recorder.built, [])
        run.assert_not_called()
        self.assertIn("app_token_missing", err)

    def test_missing_credentials_keeps_the_existing_message(self):
        code, _, err, run = self.run_ops(["--env-file", self.env(creds=False), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("error: Feishu credentials are required", err)
        self.assertNotIn("app_token_missing", err)
        run.assert_not_called()

    def test_token_value_never_reaches_output_or_log(self):
        secret = fake_token()
        code, out, err, _ = self.run_ops(["--env-file", self.env(token=secret), "--dry-run"])
        self.assertEqual(code, 0)
        # The rejected run logs an error line; the accepted one must stay clean too.
        _, out2, err2, _ = self.run_ops(["--env-file", self.env(), "--dry-run"])
        with open(self.log, encoding="utf-8") as handle:
            log_text = handle.read()
        for text in (out, err, out2, err2, log_text):
            self.assertNotIn(secret, text)
            self.assertNotIn("fake-app-secret-value", text)


if __name__ == "__main__":
    unittest.main()
