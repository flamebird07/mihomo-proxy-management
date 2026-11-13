"""Tests for FEISHU_BITABLE_APP_TOKEN resolution (no hardcoded app token).

Covered contracts:
  * resolve_app_token priority: --app-token > process env
    FEISHU_BITABLE_APP_TOKEN > env-file FEISHU_BITABLE_APP_TOKEN;
    whitespace-only candidates fall through; missing everywhere -> ""
  * DEFAULT_APP_TOKEN stays a module attribute (None) so entrypoints that
    reference base.DEFAULT_APP_TOKEN keep working; no script hardcodes a
    token literal any more
  * live_status_publish: build_client fails closed (credentials_missing) when
    the token is absent; --init/--apply exit 1, dry-run never needs a token,
    runtime failures still exit 0
  * sync-feishu-bitable: write paths exit 1 (token_error/app_token_missing)
    when the token is absent; --dry-run stays 0; the resolved token reaches
    FeishuClient in the documented priority order
  * sync-xigua-ippool-feishu: missing token -> config_error exit 1 with the
    key name in the log; CLI/process-env tokens reach FeishuClient
  * source1_independent: __main__ uses the shared resolver and fails closed
    with key-name-only messages (POSIX subprocess; structural check anywhere)

Every token value here is a runtime-generated fake. No network: Feishu
clients and locks are stubbed, and the source1 subprocess exits at the
credential gate before any Feishu or mihomo access.
"""

import importlib.util
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync = load_module("app_token_sync", "sync-feishu-bitable.py")
live = load_module("app_token_live", "live_status_publish.py")
xigua = load_module("app_token_xigua", "sync-xigua-ippool-feishu.py")

ENV_KEY = "FEISHU_BITABLE_APP_TOKEN"


def fake_token():
    return "fake-app-token-" + secrets.token_hex(8)


def env_without_token():
    return {key: value for key, value in os.environ.items() if key != ENV_KEY}


class StubLock:
    """Stands in for SyncLock/PublishLock; never touches a real mutex."""

    def __init__(self, path, log, **kwargs):
        self.path = path
        self.released = 0

    def acquire(self):
        return self

    def release(self):
        self.released += 1


class ClientRecorder:
    """Stands in for FeishuClient; records the token it was built with."""

    instances = []

    def __init__(self, app_id, app_secret, app_token, log):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        ClientRecorder.instances.append(self)


def write_env_file(path, token=None, creds=True):
    lines = []
    if creds:
        lines += ["FEISHU_APP_ID=id", "FEISHU_APP_SECRET=secret"]
    if token is not None:
        lines.append(f"{ENV_KEY}={token}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class ResolverPriorityTests(unittest.TestCase):
    """The same resolver contract must hold in every script that ships it."""

    def resolve(self, module, cli=None, process=None, file_token=None):
        environ = env_without_token()
        if process is not None:
            environ[ENV_KEY] = process
        env = {} if file_token is None else {ENV_KEY: file_token}
        with mock.patch.dict(os.environ, environ, clear=True):
            return module.resolve_app_token(cli, env)

    def test_cli_wins_over_process_env_and_file(self):
        for module in (sync, live, xigua.base):
            cli, process, file_token = fake_token(), fake_token(), fake_token()
            self.assertEqual(self.resolve(module, cli, process, file_token), cli)

    def test_process_env_wins_over_file(self):
        for module in (sync, live, xigua.base):
            process, file_token = fake_token(), fake_token()
            self.assertEqual(self.resolve(module, None, process, file_token), process)

    def test_env_file_used_when_nothing_else_set(self):
        for module in (sync, live, xigua.base):
            file_token = fake_token()
            self.assertEqual(self.resolve(module, None, None, file_token), file_token)

    def test_whitespace_candidates_fall_through_and_result_is_stripped(self):
        for module in (sync, live, xigua.base):
            file_token = fake_token()
            self.assertEqual(
                self.resolve(module, "   ", "\t", f"  {file_token}  "), file_token)

    def test_missing_everywhere_returns_empty(self):
        for module in (sync, live, xigua.base):
            self.assertEqual(self.resolve(module), "")
            self.assertEqual(self.resolve(module, "", "", ""), "")

    def test_only_the_exact_key_is_read(self):
        with mock.patch.dict(os.environ, env_without_token(), clear=True):
            self.assertEqual(
                sync.resolve_app_token(None, {"FEISHU_APP_TOKEN": fake_token()}), "")

    def test_key_name_and_default_attribute(self):
        for module in (sync, live, xigua.base):
            self.assertEqual(module.APP_TOKEN_ENV_KEY, ENV_KEY)
            self.assertIsNone(module.DEFAULT_APP_TOKEN)

    def test_app_token_flag_still_parses_with_none_default(self):
        token = fake_token()
        for parse in (sync.parse_args, xigua.parse_args,
                      lambda argv: live.build_parser().parse_args(argv)):
            self.assertIsNone(parse([]).app_token)
            self.assertEqual(parse(["--app-token", token]).app_token, token)

    def test_no_script_hardcodes_a_token_literal(self):
        for name in ("sync-feishu-bitable.py", "live_status_publish.py",
                     "sync-xigua-ippool-feishu.py", "source1_independent.py"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"DEFAULT_APP_TOKEN\s*=\s*[\"']", name)


class LiveBuildClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / ".env"
        self.log = live.Logger(str(self.root / "publish.log"))

    def args(self, cli_token=None):
        return mock.Mock(env_file=str(self.env_file), app_token=cli_token)

    def test_env_file_token_reaches_client(self):
        token = fake_token()
        write_env_file(self.env_file, token=token)
        client = live.build_client(self.args(), self.log)
        self.assertEqual(client.app_token, token)

    def test_cli_token_overrides_env_file(self):
        file_token, cli_token = fake_token(), fake_token()
        write_env_file(self.env_file, token=file_token)
        client = live.build_client(self.args(cli_token), self.log)
        self.assertEqual(client.app_token, cli_token)

    def test_process_env_token_overrides_env_file(self):
        file_token, process_token = fake_token(), fake_token()
        write_env_file(self.env_file, token=file_token)
        environ = dict(env_without_token(), **{ENV_KEY: process_token})
        with mock.patch.dict(os.environ, environ, clear=True):
            client = live.build_client(self.args(), self.log)
        self.assertEqual(client.app_token, process_token)

    def test_missing_token_raises_credentials_missing_with_key_name_only(self):
        write_env_file(self.env_file)
        with self.assertRaises(live.PublishError) as ctx:
            live.build_client(self.args(), self.log)
        self.assertEqual(ctx.exception.kind, "credentials_missing")
        self.assertIn(ENV_KEY, ctx.exception.detail)
        self.assertIn("--app-token", ctx.exception.detail)

    def test_whitespace_token_is_treated_as_missing(self):
        write_env_file(self.env_file, token="   ")
        with self.assertRaises(live.PublishError) as ctx:
            live.build_client(self.args(), self.log)
        self.assertEqual(ctx.exception.kind, "credentials_missing")

    def test_missing_id_secret_is_checked_before_the_token(self):
        write_env_file(self.env_file, token=fake_token(), creds=False)
        with self.assertRaises(live.PublishError) as ctx:
            live.build_client(self.args(), self.log)
        self.assertEqual(ctx.exception.kind, "credentials_missing")
        self.assertIn("FEISHU_APP_ID", ctx.exception.detail)
        self.assertNotIn(ENV_KEY, ctx.exception.detail)


class LiveMainExitCodeTests(unittest.TestCase):
    """--init/--apply without a token exit 1; dry-run never needs one."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / ".env"
        self.log_file = self.root / "publish.log"
        self.collect_file = self.root / "collected.json"
        # An empty document collapses into the stale fallback row; no collector
        # run, no network.
        self.collect_file.write_text("{}", encoding="utf-8")

    def base_argv(self, *extra):
        return ["--collect-json", str(self.collect_file),
                "--log-file", str(self.log_file),
                "--lock-file", str(self.root / "publish.lock"),
                "--alert-state-file", str(self.root / "alert-state.json"),
                "--publish-state-file", str(self.root / "publish-state.json"),
                "--env-file", str(self.env_file), *extra]

    def run_main(self, argv, client_factory=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env_without_token(), clear=True), \
                redirect_stdout(out):
            code = live.main(argv, client_factory=client_factory,
                             lock_factory=StubLock)
        return code, out.getvalue()

    def log_text(self):
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""

    def test_apply_without_token_exits_1(self):
        write_env_file(self.env_file)
        code, _out = self.run_main(self.base_argv("--apply"))
        self.assertEqual(code, 1)
        text = self.log_text()
        self.assertIn("kind=credentials_missing", text)
        self.assertIn(ENV_KEY, text)
        self.assertIn("result=failed_credentials_missing", text)

    def test_init_without_token_exits_1(self):
        write_env_file(self.env_file)
        code, _out = self.run_main(self.base_argv("--init"))
        self.assertEqual(code, 1)
        self.assertIn("kind=credentials_missing", self.log_text())

    def test_dry_run_without_token_stays_zero(self):
        write_env_file(self.env_file)
        code, out = self.run_main(self.base_argv("--dry-run"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["mode"], "dry-run")
        self.assertNotIn("credentials_missing", self.log_text())

    def test_apply_with_env_file_token_passes_the_gate(self):
        token = fake_token()
        write_env_file(self.env_file, token=token)
        seen = []

        class FakeFeishu:
            def list_records(self, table_id):
                return []

            def create_record(self, table_id, fields):
                return "recFake0001"

        def factory(args, log):
            client = live.build_client(args, log)
            seen.append(client.app_token)
            return FakeFeishu()

        code, _out = self.run_main(self.base_argv("--apply", "--table-id", "tblFake"),
                                   client_factory=factory)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [token])
        self.assertIn("result=apply_create", self.log_text())

    def test_runtime_client_failure_still_exits_zero(self):
        write_env_file(self.env_file, token=fake_token())

        def boom(args, log):
            raise live.PublishError("token_error", "code=99991663")

        code, _out = self.run_main(self.base_argv("--apply"), client_factory=boom)
        self.assertEqual(code, 0)
        self.assertIn("kind=token_error", self.log_text())


class SyncMainTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / "feishu.env"
        self.log_file = self.root / "sync.log"
        self.sync_state = self.root / "sync-state.json"
        ClientRecorder.instances = []

    def base_argv(self, *extra):
        return ["--env-file", str(self.env_file),
                "--log-file", str(self.log_file),
                "--state-file", str(self.root / "tables.json"),
                "--sync-state-file", str(self.sync_state),
                "--sources-env", str(self.root / "missing-sources.env"),
                "--live-env", str(self.root / "missing-live.env"),
                "--gpt-history-file", str(self.root / "gpt-history.json"),
                "--config", str(self.root / "missing-config.yaml"),
                "--source1-cache", str(self.root / "missing-cache1.yaml"),
                "--source2-cache", str(self.root / "missing-cache2.yaml"),
                "--source3-cache", str(self.root / "missing-cache3.yaml"),
                "--source4-cache", str(self.root / "missing-cache4.yaml"),
                "--lock-file", str(self.root / "sync.lock"), *extra]

    def run_main(self, argv, extra_patches=()):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env_without_token(), clear=True), \
                redirect_stdout(out):
            with ExitStack() as stack:
                stack.enter_context(patch.object(sync, "SyncLock", StubLock))
                stack.enter_context(patch.object(sync, "cleanup_temp_corpses",
                                                 lambda log, **kw: (0, 0)))
                for target, value in extra_patches:
                    stack.enter_context(patch.object(sync, target, value))
                code = sync.main(argv)
        return code, out.getvalue()

    def log_text(self):
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""

    def test_ensure_tables_without_token_exits_1(self):
        write_env_file(self.env_file)
        code, out = self.run_main(self.base_argv("--ensure-tables"),
                                  [("FeishuClient", ClientRecorder)])
        self.assertEqual(code, 1)
        text = self.log_text()
        self.assertIn("class=token_error", text)
        self.assertIn("app_token_missing", text)
        self.assertIn(ENV_KEY, text)
        self.assertIn("错误=token_error", out)
        self.assertEqual(ClientRecorder.instances, [])
        # --ensure-tables never mirrors the run state file
        self.assertFalse(self.sync_state.exists())

    def test_default_write_run_without_token_records_state(self):
        write_env_file(self.env_file)
        code, _out = self.run_main(self.base_argv(), [("FeishuClient", ClientRecorder)])
        self.assertEqual(code, 1)
        state = json.loads(self.sync_state.read_text(encoding="utf-8"))
        self.assertEqual(state["last_error"], "token_error")
        self.assertEqual(ClientRecorder.instances, [])

    def test_missing_id_secret_is_checked_before_the_token(self):
        write_env_file(self.env_file, creds=False)
        code, _out = self.run_main(self.base_argv("--ensure-tables"))
        self.assertEqual(code, 1)
        text = self.log_text()
        self.assertIn("detail=credentials_missing", text)
        self.assertNotIn("app_token_missing", text)

    def test_dry_run_without_token_stays_zero(self):
        write_env_file(self.env_file)

        def fake_collect(index, url, ua, timeout, proxy, log):
            result = sync.SourceResult(index, f"sub{index}")
            result.result = "失败"
            result.error = "url_missing"
            return result

        guard = MagicMock(side_effect=AssertionError("speedtest must not run"))
        code, out = self.run_main(
            self.base_argv("--dry-run"),
            [("FeishuClient", ClientRecorder), ("collect_source", fake_collect),
             ("test_isolated", guard), ("test_live", guard)])
        self.assertEqual(code, 0)
        self.assertNotIn("app_token_missing", self.log_text())
        self.assertIn("dry run: no write", self.log_text())
        self.assertFalse(self.sync_state.exists())
        guard.assert_not_called()

    def test_cli_token_reaches_client(self):
        file_token, cli_token = fake_token(), fake_token()
        write_env_file(self.env_file, token=file_token)
        code, _out = self.run_main(
            self.base_argv("--ensure-tables", "--app-token", cli_token),
            [("FeishuClient", ClientRecorder),
             ("ensure_tables", lambda client, state_file, keep, log: {})])
        self.assertEqual(code, 0)
        self.assertEqual([c.app_token for c in ClientRecorder.instances], [cli_token])

    def test_process_env_token_beats_env_file(self):
        file_token, process_token = fake_token(), fake_token()
        write_env_file(self.env_file, token=file_token)
        environ = dict(env_without_token(), **{ENV_KEY: process_token})
        out = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), redirect_stdout(out):
            with patch.object(sync, "SyncLock", StubLock), \
                    patch.object(sync, "cleanup_temp_corpses", lambda log, **kw: (0, 0)), \
                    patch.object(sync, "FeishuClient", ClientRecorder), \
                    patch.object(sync, "ensure_tables",
                                 lambda client, state_file, keep, log: {}):
                code = sync.main(self.base_argv("--ensure-tables"))
        self.assertEqual(code, 0)
        self.assertEqual([c.app_token for c in ClientRecorder.instances], [process_token])

    def test_env_file_token_used_when_nothing_else_set(self):
        file_token = fake_token()
        write_env_file(self.env_file, token=file_token)
        code, _out = self.run_main(
            self.base_argv("--ensure-tables"),
            [("FeishuClient", ClientRecorder),
             ("ensure_tables", lambda client, state_file, keep, log: {})])
        self.assertEqual(code, 0)
        self.assertEqual([c.app_token for c in ClientRecorder.instances], [file_token])


class XiguaMainTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / "feishu.env"
        self.log_file = self.root / "xigua.log"
        ClientRecorder.instances = []

    def base_argv(self, *extra):
        return ["--env-file", str(self.env_file),
                "--log-file", str(self.log_file),
                "--ippool-state", str(self.root / "ippool.json"),
                "--final-provider", str(self.root / "ippool.yaml"),
                "--cursor", str(self.root / "cursor.json"),
                "--lock", str(self.root / "xigua.lock"), *extra]

    def run_main(self, argv, extra_patches=()):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env_without_token(), clear=True), \
                redirect_stdout(out), redirect_stderr(err):
            with ExitStack() as stack:
                stack.enter_context(patch.object(xigua.base, "SyncLock", StubLock))
                for target, name, value in extra_patches:
                    stack.enter_context(patch.object(target, name, value))
                code = xigua.main(argv)
        return code, out.getvalue(), err.getvalue()

    def log_text(self):
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""

    def test_missing_token_exits_1_with_key_name(self):
        write_env_file(self.env_file)
        code, _out, err = self.run_main(
            self.base_argv(), [(xigua.base, "FeishuClient", ClientRecorder)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err), {"status": "error", "class": "config_error"})
        text = self.log_text()
        self.assertIn("class=config_error", text)
        self.assertIn("feishu_app_token_missing", text)
        self.assertIn(ENV_KEY, text)
        self.assertEqual(ClientRecorder.instances, [])

    def test_missing_credentials_checked_first(self):
        write_env_file(self.env_file, creds=False)
        code, _out, err = self.run_main(self.base_argv())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["class"], "config_error")
        self.assertIn("feishu_credentials_missing", self.log_text())
        self.assertNotIn("feishu_app_token_missing", self.log_text())

    def test_cli_token_reaches_client(self):
        token = fake_token()
        write_env_file(self.env_file)
        code, out, _err = self.run_main(
            self.base_argv("--app-token", token),
            [(xigua.base, "FeishuClient", ClientRecorder),
             (xigua, "sync_snapshot", MagicMock(return_value="skipped"))])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "skipped")
        self.assertEqual([c.app_token for c in ClientRecorder.instances], [token])

    def test_process_env_token_beats_env_file(self):
        file_token, process_token = fake_token(), fake_token()
        write_env_file(self.env_file, token=file_token)
        environ = dict(env_without_token(), **{ENV_KEY: process_token})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), \
                redirect_stdout(out), redirect_stderr(err):
            with ExitStack() as stack:
                stack.enter_context(patch.object(xigua.base, "SyncLock", StubLock))
                stack.enter_context(patch.object(xigua.base, "FeishuClient", ClientRecorder))
                stack.enter_context(patch.object(xigua, "sync_snapshot",
                                                 MagicMock(return_value="skipped")))
                code = xigua.main(self.base_argv())
        self.assertEqual(code, 0)
        self.assertEqual([c.app_token for c in ClientRecorder.instances], [process_token])


class Source1EntrypointTests(unittest.TestCase):
    """source1_independent cannot be imported off Windows (source3_compatible
    pulls ctypes.wintypes), so the wiring is checked structurally everywhere
    and functionally in a POSIX subprocess that stops at the credential gate.
    """

    def test_main_uses_the_shared_resolver_and_fails_closed(self):
        text = (SCRIPTS / "source1_independent.py").read_text(encoding="utf-8")
        self.assertIn("h['resolve_app_token'](args.app_token, env)", text)
        self.assertIn("app_token_missing: FEISHU_BITABLE_APP_TOKEN", text)
        self.assertIn("credentials_missing: FEISHU_APP_ID/FEISHU_APP_SECRET", text)
        self.assertNotIn("env['FEISHU_APP_ID']", text)

    def run_entrypoint(self, env_text, process_token=None):
        """Run __main__ in a temp cwd where the Windows default env path is a
        relative filename; stub source3_compatible so the import works on POSIX.
        The run must exit at the credential gate — never reaches Feishu/mihomo.
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / sync.DEFAULT_ENV_FILE).write_text(env_text, encoding="utf-8")
            child = (
                "import sys, types, runpy\n"
                "stub = types.ModuleType('source3_compatible')\n"
                "stub.atomic_json = lambda *a, **k: None\n"
                "stub.eligible_candidates = lambda *a, **k: []\n"
                "sys.modules['source3_compatible'] = stub\n"
                f"runpy.run_path({str(SCRIPTS / 'source1_independent.py')!r}, "
                "run_name='__main__')\n"
            )
            environ = env_without_token()
            if process_token is not None:
                environ[ENV_KEY] = process_token
            return subprocess.run([sys.executable, "-c", child], cwd=tmp,
                                  env=environ, capture_output=True, text=True,
                                  timeout=60)

    @unittest.skipIf(os.name == "nt", "POSIX-only shadowing of the Windows env path")
    def test_missing_token_exits_1_with_key_name_only(self):
        proc = self.run_entrypoint("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("app_token_missing", proc.stderr)
        self.assertIn(ENV_KEY, proc.stderr)
        self.assertEqual(proc.stdout, "")

    @unittest.skipIf(os.name == "nt", "POSIX-only shadowing of the Windows env path")
    def test_missing_credentials_checked_first_and_token_never_printed(self):
        token = fake_token()
        proc = self.run_entrypoint(f"{ENV_KEY}={token}\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("credentials_missing", proc.stderr)
        self.assertIn("FEISHU_APP_ID", proc.stderr)
        self.assertNotIn(token, proc.stderr)
        self.assertNotIn(token, proc.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX-only shadowing of the Windows env path")
    def test_process_env_token_passes_the_gate(self):
        token = fake_token()
        proc = self.run_entrypoint("FEISHU_APP_ID=id\nFEISHU_APP_SECRET=secret\n",
                                   process_token=token)
        # The gate passes; the run then stops at the absent production state
        # file (KeyError 'tables') — proof the token was accepted without ever
        # reaching Feishu or mihomo.
        self.assertNotIn("app_token_missing", proc.stderr)
        self.assertNotIn("credentials_missing", proc.stderr)
        self.assertIn("KeyError", proc.stderr)
        self.assertNotIn(token, proc.stderr)


if __name__ == "__main__":
    unittest.main()