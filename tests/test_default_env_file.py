"""Runtime resolution of the credentials env file (no hardcoded Administrator).

Everything here is host-neutral: the Windows branch is exercised by passing
``os_name="nt"`` plus temp directories, never by touching a real ``C:\\mihomo``
or the real user home.  No network; Feishu clients and publishers are stubs.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import portable_paths  # noqa: E402
from node_provider import envfile as provider_envfile  # noqa: E402
from node_ops import envfile as ops_envfile  # noqa: E402
from node_provider import __main__ as provider_cli  # noqa: E402
from node_ops import __main__ as ops_cli  # noqa: E402
from node_provider import runner as provider_runner  # noqa: E402
from node_ops import runner as ops_runner  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync_base = _load("default_env_sync_base", "sync-feishu-bitable.py")
proxy_mod = _load("default_env_proxy_switch", "proxy-auto-switch.py")
live_mod = _load("default_env_live_status", "live-status-publish.py"
                 if os.path.exists(os.path.join(SCRIPTS, "live-status-publish.py"))
                 else "live_status_publish.py")

ENV_KEY = "FEISHU_BITABLE_APP_TOKEN"
MIRRORS = (
    ("portable_paths", portable_paths),
    ("node_provider", provider_envfile),
    ("node_ops", ops_envfile),
)


def write_subscription(directory, text):
    path = os.path.join(directory, "subscription.env")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def write_creds_file(path, token="fake-token-value"):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("FEISHU_APP_ID=fake-id\n"
                     "FEISHU_APP_SECRET=fake-secret\n"
                     f"{ENV_KEY}={token}\n")
    return path


class DefaultEnvFileTests(unittest.TestCase):
    def test_windows_prefers_marked_install_subscription(self):
        with tempfile.TemporaryDirectory() as install:
            expected = write_subscription(install, "FEISHU_APP_ID=fake\n")
            got = portable_paths.default_env_file(
                os_name="nt", install_dir=install, legacy="/some/legacy.env")
            self.assertEqual(got, expected)

    def test_windows_without_file_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as install:
            got = portable_paths.default_env_file(
                os_name="nt", install_dir=install, legacy="/some/legacy.env")
            self.assertEqual(got, "/some/legacy.env")

    def test_windows_without_marker_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as install:
            write_subscription(install, "SECRET=abc\n")
            got = portable_paths.default_env_file(
                os_name="nt", install_dir=install, legacy="/some/legacy.env")
            self.assertEqual(got, "/some/legacy.env")

    def test_posix_ignores_marked_install_subscription(self):
        with tempfile.TemporaryDirectory() as install:
            write_subscription(install, "FEISHU_APP_ID=fake\n")
            got = portable_paths.default_env_file(
                os_name="posix", install_dir=install, legacy="/some/legacy.env")
            self.assertEqual(got, "/some/legacy.env")

    def test_defaults_follow_os_name(self):
        self.assertEqual(
            portable_paths.default_env_file(
                os_name="nt", install_dir="/nonexistent-install", legacy="/l.env"),
            "/l.env")

    def test_bom_and_spaced_key_are_recognised(self):
        with tempfile.TemporaryDirectory() as install:
            expected = write_subscription(
                install, "\ufeff# leading comment\n\n   FEISHU_APP_ID = fake\n")
            self.assertTrue(portable_paths.has_feishu_keys(expected))
            got = portable_paths.default_env_file(
                os_name="nt", install_dir=install, legacy="/l.env")
            self.assertEqual(got, expected)

    def test_garbage_and_directory_are_false_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            garbage = os.path.join(tmp, "garbage.env")
            with open(garbage, "wb") as handle:
                handle.write(b"\xff\xfe\x00\x01 not text")
            self.assertFalse(portable_paths.has_feishu_keys(garbage))
            self.assertFalse(portable_paths.has_feishu_keys(tmp))
            self.assertFalse(portable_paths.has_feishu_keys(os.path.join(tmp, "missing.env")))
            self.assertFalse(portable_paths.has_feishu_keys(
                os.path.join(tmp, "noequals.env")))
            with open(os.path.join(tmp, "noequals.env"), "w", encoding="utf-8") as handle:
                handle.write("FEISHU_APP_ID\n")
            self.assertFalse(portable_paths.has_feishu_keys(
                os.path.join(tmp, "noequals.env")))


class WindowsInstallDirTests(unittest.TestCase):
    def test_env_var_pointing_at_a_directory_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MIHOMO_DIR": tmp}, clear=False):
                self.assertEqual(portable_paths.windows_install_dir(), tmp)

    def test_missing_or_empty_env_var_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope")
            with patch.dict(os.environ, {"MIHOMO_DIR": missing}, clear=False):
                self.assertEqual(portable_paths.windows_install_dir(),
                                 portable_paths.WINDOWS_INSTALL_DIR)
            with patch.dict(os.environ, {"MIHOMO_DIR": ""}, clear=False):
                self.assertEqual(portable_paths.windows_install_dir(),
                                 portable_paths.WINDOWS_INSTALL_DIR)

    def test_explicit_environ_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                portable_paths.windows_install_dir({"MIHOMO_DIR": tmp}), tmp)
            self.assertEqual(
                portable_paths.windows_install_dir({"MIHOMO_DIR": "/no/such/dir"}),
                portable_paths.WINDOWS_INSTALL_DIR)


@unittest.skipIf(os.name == "nt", "POSIX home mapping only")
class LegacyEnvFileTests(unittest.TestCase):
    def test_legacy_env_file_is_the_current_home(self):
        self.assertEqual(Path(portable_paths.legacy_env_file()),
                         Path.home() / ".hermes" / ".env")
        # and it matches the portable mapping of the old hardcoded path
        self.assertEqual(
            Path(portable_paths.legacy_env_file()),
            Path(portable_paths.portable_path(r"C:\Users\Administrator\.hermes\.env")))


class MirrorConsistencyTests(unittest.TestCase):
    def test_all_implementations_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            marked = os.path.join(tmp, "marked")
            os.mkdir(marked)
            write_subscription(marked, "FEISHU_APP_ID=fake\n")
            unmarked = os.path.join(tmp, "unmarked")
            os.mkdir(unmarked)
            write_subscription(unmarked, "SECRET=only\n")
            empty = os.path.join(tmp, "empty")
            os.mkdir(empty)

            cases = [
                ("nt", marked, "/legacy.env"),
                ("nt", unmarked, "/legacy.env"),
                ("nt", empty, "/legacy.env"),
                ("posix", marked, "/legacy.env"),
                ("nt", "", "/legacy.env"),
            ]
            for os_name, install_dir, legacy in cases:
                with self.subTest(case=(os_name, install_dir, legacy)):
                    results = [
                        module.default_env_file(
                            os_name=os_name, install_dir=install_dir, legacy=legacy)
                        for _, module in MIRRORS
                    ]
                    self.assertEqual(len(set(results)), 1, results)


class ParsedArgResolutionTests(unittest.TestCase):
    def test_sync_feishu_parse_args_resolves_at_runtime(self):
        with patch.object(sync_base, "default_env_file",
                          MagicMock(return_value="/tmp/resolved.env")) as resolver:
            args = sync_base.parse_args([])
        self.assertEqual(args.env_file, "/tmp/resolved.env")
        resolver.assert_called_once()

    def test_sync_feishu_explicit_env_file_skips_resolver(self):
        with patch.object(sync_base, "default_env_file",
                          MagicMock(side_effect=AssertionError("must not resolve"))):
            args = sync_base.parse_args(["--env-file", "/tmp/explicit.env"])
        self.assertEqual(args.env_file, "/tmp/explicit.env")

    def test_proxy_auto_switch_parse_args_resolves_at_runtime(self):
        with patch.object(proxy_mod, "default_env_file",
                          MagicMock(return_value="/tmp/resolved.env")) as resolver:
            args = proxy_mod.parse_args([])
        self.assertEqual(args.env_file, "/tmp/resolved.env")
        resolver.assert_called_once()

    def test_proxy_auto_switch_explicit_env_file_skips_resolver(self):
        with patch.object(proxy_mod, "default_env_file",
                          MagicMock(side_effect=AssertionError("must not resolve"))):
            args = proxy_mod.parse_args(["--env-file", "/tmp/explicit.env"])
        self.assertEqual(args.env_file, "/tmp/explicit.env")

    def test_live_status_parser_default_is_none(self):
        self.assertIsNone(live_mod.build_parser().parse_args([]).env_file)


class _FakeLock:
    def __init__(self, *args, **kwargs):
        pass

    def acquire(self):
        return True

    def release(self):
        pass


class LiveStatusMainTests(unittest.TestCase):
    def _run(self, argv, fake_resolver):
        published = MagicMock(return_value="current")
        with patch.object(live_mod, "default_env_file", fake_resolver), \
                patch.object(live_mod, "publish", published), \
                patch.object(live_mod, "Logger", lambda *a, **k: MagicMock()):
            code = live_mod.main(argv, client_factory=lambda *a: None,
                                 lock_factory=_FakeLock)
        return code, published

    def test_main_resolves_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = write_creds_file(os.path.join(tmp, ".env"))
            code, published = self._run(
                ["--log-file", os.devnull, "--lock-file", os.path.join(tmp, "l")],
                MagicMock(return_value=env))
            self.assertEqual(code, 0)
            self.assertEqual(published.call_args.args[0].env_file, env)

    def test_main_explicit_env_file_skips_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = os.path.join(tmp, "explicit.env")
            code, published = self._run(
                ["--env-file", explicit, "--log-file", os.devnull,
                 "--lock-file", os.path.join(tmp, "l")],
                MagicMock(side_effect=AssertionError("must not resolve")))
            self.assertEqual(code, 0)
            self.assertEqual(published.call_args.args[0].env_file, explicit)


# ---- node_provider / node_ops CLI -----------------------------------------


def _fake_token():
    return "fake-app-token-value"


class _ProviderClient:
    def __init__(self):
        self.built = []

    def __call__(self, app_id, app_secret, app_token, log=None):
        self.built.append((app_id, app_secret, app_token))
        return MagicMock()


class _OpsClient:
    def __init__(self):
        self.built = []
        self.clients = []

    def __call__(self, app_id, app_secret, app_token, log=None):
        self.built.append((app_id, app_secret, app_token))
        client = MagicMock()
        self.clients.append(client)
        return client


class ProviderOpsResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        patcher = patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                          if k != ENV_KEY}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_provider_main_resolves_when_omitted(self):
        token = _fake_token()
        env = write_creds_file(os.path.join(self.root, "creds.env"), token)
        output = os.path.join(self.root, "provider.yaml")
        recorder = _ProviderClient()
        with patch.object(provider_cli, "default_env_file",
                          MagicMock(return_value=env)) as resolver, \
                patch.object(provider_cli, "run", return_value=provider_runner.EXIT_OK):
            code = provider_cli.main(["-o", output], client_factory=recorder,
                                     now_ms=1_700_000_000_000)
        self.assertEqual(code, provider_runner.EXIT_OK)
        resolver.assert_called_once()
        self.assertEqual([item[2] for item in recorder.built], [token])

    def test_provider_main_explicit_env_file_skips_resolver(self):
        env = write_creds_file(os.path.join(self.root, "creds.env"))
        output = os.path.join(self.root, "provider.yaml")
        with patch.object(provider_cli, "default_env_file",
                          MagicMock(side_effect=AssertionError("must not resolve"))), \
                patch.object(provider_cli, "run", return_value=provider_runner.EXIT_OK):
            provider_cli.main(["--env-file", env, "-o", output],
                              client_factory=_ProviderClient(),
                              now_ms=1_700_000_000_000)

    def test_ops_main_resolves_when_omitted(self):
        token = _fake_token()
        env = write_creds_file(os.path.join(self.root, "creds.env"), token)
        recorder = _OpsClient()
        with patch.object(ops_cli, "default_env_file",
                          MagicMock(return_value=env)) as resolver, \
                patch.object(ops_cli, "FeishuClient", recorder), \
                patch.object(ops_cli, "run", return_value=ops_runner.RunSummary()):
            code = ops_cli.main(["--dry-run",
                                 "--lock-file", os.path.join(self.root, "o.lock"),
                                 "--log-file", os.path.join(self.root, "o.log")])
        self.assertEqual(code, 0)
        resolver.assert_called_once()
        self.assertEqual([item[2] for item in recorder.built], [token])

    def test_ops_main_explicit_env_file_skips_resolver(self):
        env = write_creds_file(os.path.join(self.root, "creds.env"))
        with patch.object(ops_cli, "default_env_file",
                          MagicMock(side_effect=AssertionError("must not resolve"))), \
                patch.object(ops_cli, "FeishuClient", _OpsClient()), \
                patch.object(ops_cli, "run", return_value=ops_runner.RunSummary()):
            code = ops_cli.main(["--env-file", env, "--dry-run",
                                 "--lock-file", os.path.join(self.root, "o.lock"),
                                 "--log-file", os.path.join(self.root, "o.log")])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
