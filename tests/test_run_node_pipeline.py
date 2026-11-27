"""Windows node pipeline: Feishu-pool step, provider hot reload, exit semantics."""

import atexit
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
SCRIPTS = os.path.join(ROOT, "scripts")
SCRIPT = os.path.join(SCRIPTS, "run-node-pipeline.py")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

# The module computes its install-dir constants at import time; root them under
# a throwaway $MPM_HOME so the tests never touch the real host (the same trick
# tests/test_proxy_auto_switch_portable_paths.py uses for MPM_HOME).
TEMP_HOME = tempfile.mkdtemp(prefix="node-pipeline-home-")
atexit.register(shutil.rmtree, TEMP_HOME, True)
_ORIGINAL_MPM_HOME = os.environ.get("MPM_HOME")
os.environ["MPM_HOME"] = TEMP_HOME

spec = importlib.util.spec_from_file_location("run_node_pipeline_under_test", SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pipeline
spec.loader.exec_module(pipeline)

if _ORIGINAL_MPM_HOME is None:
    os.environ.pop("MPM_HOME", None)
else:
    os.environ["MPM_HOME"] = _ORIGINAL_MPM_HOME

SECRET = "pipeline-test-secret-do-not-print"


def write_config(path, *, provider_type="file", provider_path="./providers/feishu-pool.yaml"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "proxy-providers:\n"
            "  subscription:\n"
            f"    type: {provider_type}\n"
            f"    path: {provider_path}\n"
        )


def write_env(path, values):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


class PoolModeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pipeline-pool-mode-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def config(self, **kwargs):
        path = os.path.join(self.directory, "config.yaml")
        write_config(path, **kwargs)
        return path

    def test_file_provider_at_managed_path_is_pool_mode(self):
        self.assertTrue(pipeline.pool_mode(self.config()))

    def test_http_provider_is_not_pool_mode(self):
        self.assertFalse(pipeline.pool_mode(self.config(provider_type="http")))

    def test_missing_file_is_not_pool_mode(self):
        self.assertFalse(pipeline.pool_mode(os.path.join(self.directory, "absent.yaml")))

    def test_garbage_file_is_not_pool_mode(self):
        path = os.path.join(self.directory, "garbage.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("proxy-providers: [unterminated\n")
        self.assertFalse(pipeline.pool_mode(path))

    def test_custom_file_provider_is_not_pool_mode(self):
        self.assertFalse(pipeline.pool_mode(
            self.config(provider_path="./providers/custom.yaml")))

    def test_bom_prefixed_config_is_read(self):
        path = os.path.join(self.directory, "bom.yaml")
        with open(path, "w", encoding="utf-8-sig") as handle:
            handle.write(
                "proxy-providers:\n"
                "  subscription:\n"
                "    type: file\n"
                "    path: ./providers/feishu-pool.yaml\n"
            )
        self.assertTrue(pipeline.pool_mode(path))


class BuildStepsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pipeline-steps-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.env_file = os.path.join(self.directory, "subscription.env")
        self.pool_config = os.path.join(self.directory, "config.yaml")
        write_config(self.pool_config)

    def _steps(self, *, source4=False, env_file=None):
        with mock.patch.object(pipeline, "_source4_input_available",
                               return_value=source4):
            return pipeline.build_steps(config_path=self.pool_config,
                                        env_file=env_file or self.env_file)

    def test_subscription_mode_has_the_original_three_steps(self):
        path = os.path.join(self.directory, "config.yaml")
        write_config(path, provider_type="http")
        steps = pipeline.build_steps(config_path=path, env_file=self.env_file)
        self.assertEqual([label for label, _argv in steps], ["collect", "ops", "provide"])
        # Regression: the pre-change argv of the three steps.
        self.assertEqual(steps[0][1], [
            sys.executable, "-m", "node_provider", "--collect",
            "--sources-env", str(pipeline.ROOT / "config" / "subscription.env"),
            "--from-app", "source4",
        ])
        self.assertEqual(steps[1][1], [sys.executable, "-m", "node_ops"])
        self.assertEqual(steps[2][1], [
            sys.executable, "-m", "node_provider",
            "--output", pipeline.PRODUCTION_PROVIDER,
        ])

    def test_fresh_pool_host_without_repo_credentials_skips_collect(self):
        # New-machine install: install env file with Feishu credentials only,
        # no repo config/subscription.env and no source4 input.
        write_env(self.env_file, {"FEISHU_APP_ID": "id"})
        steps = self._steps()
        self.assertEqual([label for label, _argv in steps],
                         ["pool", "ops", "provide"])
        flat = " ".join(" ".join(argv) for _label, argv in steps)
        self.assertNotIn(str(pipeline.ROOT / "config" / "subscription.env"), flat)
        self.assertEqual(steps[0][1], [
            sys.executable, pipeline.POOL_SCRIPT,
            "--env-file", self.env_file,
            "--output", pipeline.POOL_FILE,
        ])
        self.assertEqual(steps[1][1], [
            sys.executable, "-m", "node_ops",
            "--env-file", self.env_file,
            "--core", pipeline._install_path("mihomo.exe"),
        ])
        self.assertEqual(steps[2][1], [
            sys.executable, "-m", "node_provider",
            "--output", pipeline.PRODUCTION_PROVIDER,
            "--env-file", self.env_file,
        ])

    def test_pool_mode_with_a_configured_slot_runs_collect(self):
        write_env(self.env_file, {
            "FEISHU_APP_ID": "id",
            "SUBSCRIPTION_URL_1": "https://example.invalid/sub",
        })
        steps = self._steps()
        self.assertEqual([label for label, _argv in steps],
                         ["collect", "pool", "ops", "provide"])
        self.assertEqual(steps[0][1], [
            sys.executable, "-m", "node_provider", "--collect",
            "--env-file", self.env_file,
            "--sources-env", self.env_file,
        ])

    def test_pool_mode_with_source4_input_keeps_the_app_call(self):
        write_env(self.env_file, {"FEISHU_APP_ID": "id"})
        steps = self._steps(source4=True)
        self.assertEqual([label for label, _argv in steps],
                         ["collect", "pool", "ops", "provide"])
        self.assertEqual(steps[0][1], [
            sys.executable, "-m", "node_provider", "--collect",
            "--env-file", self.env_file,
            "--from-app", "source4",
        ])

    def test_existing_valid_repo_source_file_may_be_reused(self):
        write_env(self.env_file, {"FEISHU_APP_ID": "id"})
        fake_root = os.path.join(self.directory, "root")
        repo_sources = os.path.join(fake_root, "config", "subscription.env")
        write_env(repo_sources, {"SUBSCRIPTION_URL_2": "https://example.invalid/x"})
        with mock.patch.object(pipeline, "ROOT", fake_root):
            steps = self._steps()
        self.assertEqual(steps[0][0], "collect")
        self.assertIn(repo_sources, steps[0][1])
        # The credentials file is still the installed env file for every step.
        self.assertIn("--env-file", steps[0][1])
        self.assertEqual(steps[0][1][steps[0][1].index("--env-file") + 1],
                         self.env_file)

    def test_custom_install_dir_is_used_for_core_and_env(self):
        install_dir = os.path.join(self.directory, "custom-install")
        os.makedirs(install_dir)
        env_file = os.path.join(install_dir, "subscription.env")
        write_env(env_file, {"FEISHU_APP_ID": "id"})
        with mock.patch.dict(os.environ, {"MIHOMO_DIR": install_dir}), \
                mock.patch.object(pipeline.portable, "windows_install_dir",
                                  return_value=install_dir):
            steps = self._steps(env_file=env_file)
        ops = dict(steps)["ops"]
        self.assertEqual(ops[ops.index("--core") + 1],
                         os.path.join(install_dir, "mihomo.exe"))
        for _label, argv in steps:
            self.assertIn(env_file, argv)


class ReloadPoolProviderTest(unittest.TestCase):
    class Response:
        def __init__(self, status):
            self.status = status
            self.closed = False

        def getcode(self):
            return self.status

        def close(self):
            self.closed = True

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pipeline-reload-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.env_file = os.path.join(self.directory, "subscription.env")

    def test_missing_secret_makes_no_request(self):
        write_env(self.env_file, {"FEISHU_APP_ID": "id"})
        calls = []
        self.assertFalse(pipeline.reload_pool_provider(
            env_file=self.env_file, urlopen=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls, [])

    def test_put_request_targets_the_provider(self):
        write_env(self.env_file, {"SECRET": SECRET})
        captured = {}

        def opener(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return self.Response(204)

        self.assertTrue(pipeline.reload_pool_provider(
            env_file=self.env_file, urlopen=opener))
        request = captured["request"]
        self.assertEqual(request.get_method(), "PUT")
        self.assertTrue(request.full_url.endswith("/providers/proxies/subscription"))
        self.assertEqual(request.get_header("Authorization"), f"Bearer {SECRET}")
        self.assertEqual(captured["timeout"], 10)

    def test_server_error_is_false(self):
        write_env(self.env_file, {"SECRET": SECRET})
        self.assertFalse(pipeline.reload_pool_provider(
            env_file=self.env_file, urlopen=lambda *a, **k: self.Response(500)))

    def test_opener_exception_is_false(self):
        write_env(self.env_file, {"SECRET": SECRET})

        def opener(*_a, **_k):
            raise OSError("controller down")

        self.assertFalse(pipeline.reload_pool_provider(
            env_file=self.env_file, urlopen=opener))


class RefreshPoolTest(unittest.TestCase):
    class Response:
        def __init__(self, status):
            self.status = status

        def close(self):
            pass

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pipeline-refresh-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.env_file = os.path.join(self.directory, "subscription.env")
        write_env(self.env_file, {"SECRET": SECRET})
        os.makedirs(os.path.dirname(pipeline.POOL_FILE), exist_ok=True)
        self.addCleanup(self._cleanup_pool)

    def _cleanup_pool(self):
        for path in (pipeline.POOL_FILE,):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _write_pool(self, text):
        with open(pipeline.POOL_FILE, "w", encoding="utf-8") as handle:
            handle.write(text)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_changed_pool_reloads_and_writes_marker(self):
        digest = self._write_pool("proxies: []\n")
        marker = os.path.join(self.directory, "pool.sha256")
        called = []

        def opener(request, timeout=None):
            called.append(request)
            return self.Response(204)

        messages = []
        self.assertTrue(pipeline.refresh_pool(
            marker=marker, env_file=self.env_file, urlopen=opener,
            log=messages.append))
        self.assertEqual(len(called), 1)
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), digest)
        self.assertNotIn(SECRET, " ".join(messages))

    def test_unchanged_pool_is_not_reloaded(self):
        digest = self._write_pool("proxies: []\n")
        marker = os.path.join(self.directory, "pool.sha256")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(digest + "\n")
        called = []
        self.assertFalse(pipeline.refresh_pool(
            marker=marker, env_file=self.env_file,
            urlopen=lambda *a, **k: called.append(a), log=lambda _m: None))
        self.assertEqual(called, [])

    def test_failed_reload_does_not_write_marker(self):
        self._write_pool("proxies: []\n")
        marker = os.path.join(self.directory, "pool.sha256")
        self.assertFalse(pipeline.refresh_pool(
            marker=marker, env_file=self.env_file,
            urlopen=lambda *a, **k: self.Response(500),
            log=lambda _m: None))
        self.assertFalse(os.path.exists(marker))

    def test_missing_pool_file_is_false(self):
        marker = os.path.join(self.directory, "pool.sha256")
        missing = os.path.join(self.directory, "no-such-pool.yaml")
        with mock.patch.object(pipeline, "POOL_FILE", missing):
            self.assertFalse(pipeline.refresh_pool(
                marker=marker, env_file=self.env_file,
                urlopen=lambda *a, **k: self.Response(204),
                log=lambda _m: None))


class PoolCollectStepTest(unittest.TestCase):
    """The supplemental collect step appears only when there is something to read."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pipeline-collect-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.env_file = os.path.join(self.directory, "subscription.env")

    def _step(self, *, source4=False):
        with mock.patch.object(pipeline, "_source4_input_available",
                               return_value=source4):
            return pipeline._pool_collect_step(self.env_file)

    def test_no_credentials_skips_collect(self):
        self.assertIsNone(self._step())

    def test_credentials_only_skips_collect(self):
        write_env(self.env_file, {"FEISHU_APP_ID": "id"})
        self.assertIsNone(self._step())

    def test_malformed_env_skips_collect(self):
        with open(self.env_file, "w", encoding="utf-8") as handle:
            handle.write("this line has no equals sign\n")
        self.assertIsNone(self._step())

    def test_configurable_slot_enables_collect(self):
        write_env(self.env_file, {"SUBSCRIPTION_URL_3": "https://example.invalid/x"})
        step = self._step()
        self.assertIsNotNone(step)
        self.assertIn("--sources-env", step[1])

    def test_source4_input_alone_enables_collect(self):
        write_env(self.env_file, {"FEISHU_APP_ID": "id"})
        step = self._step(source4=True)
        self.assertIsNotNone(step)
        self.assertIn("--from-app", step[1])
        self.assertNotIn("--sources-env", step[1])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pipeline-main-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.log_path = os.path.join(self.directory, "pipeline.log")
        self.subscription = os.path.join(self.directory, "subscription.yaml")
        write_config(self.subscription, provider_type="http")
        self.pool_config = os.path.join(self.directory, "pool.yaml")
        write_config(self.pool_config)
        self.env_file = os.path.join(self.directory, "subscription.env")
        write_env(self.env_file, {
            "FEISHU_APP_ID": "id",
            "SUBSCRIPTION_URL_1": "https://example.invalid/sub",
        })
        self.executed = []

    def _runner(self, codes):
        """Fake subprocess.run that records labels and applies per-label codes."""
        def label_of(argv):
            if pipeline.POOL_SCRIPT in argv:
                return "pool"
            if "--collect" in argv:
                return "collect"
            if "node_ops" in argv:
                return "ops"
            return "provide"

        def run(argv, **_kwargs):
            label = label_of(argv)
            self.executed.append(label)
            return SimpleNamespace(returncode=codes.get(label, 0))

        return run

    def _run_main(self, config_path, codes, *, source4=False, env_file=None):
        refresh_calls = []
        with mock.patch.object(pipeline, "LOG", self.log_path), \
                mock.patch.object(pipeline, "PRODUCTION_CONFIG", config_path), \
                mock.patch.object(pipeline.portable, "default_env_file",
                                  return_value=env_file or self.env_file), \
                mock.patch.object(pipeline, "_source4_input_available",
                                  return_value=source4), \
                mock.patch.object(pipeline.subprocess, "run", side_effect=self._runner(codes)), \
                mock.patch.object(pipeline, "refresh_pool",
                                  side_effect=lambda **kw: refresh_calls.append(kw) or False):
            code = pipeline.main()
        return code, refresh_calls

    def test_subscription_mode_success_has_no_pool_refresh(self):
        code, refreshes = self._run_main(self.subscription, {})
        self.assertEqual(code, 0)
        self.assertEqual(self.executed, ["collect", "ops", "provide"])
        self.assertEqual(refreshes, [])

    def test_pool_success_refreshes_once(self):
        code, refreshes = self._run_main(self.pool_config, {})
        self.assertEqual(code, 0)
        self.assertEqual(self.executed, ["collect", "pool", "ops", "provide"])
        self.assertEqual(len(refreshes), 1)

    def test_pool_failure_skips_refresh_and_returns_1(self):
        code, refreshes = self._run_main(self.pool_config, {"pool": 1})
        self.assertEqual(code, 1)
        self.assertEqual(refreshes, [])
        # A failed step never blocks the following ones.
        self.assertEqual(self.executed, ["collect", "pool", "ops", "provide"])

    def test_any_failing_step_returns_1_but_continues(self):
        code, _refreshes = self._run_main(self.subscription, {"ops": 2})
        self.assertEqual(code, 1)
        self.assertEqual(self.executed, ["collect", "ops", "provide"])

    def test_pool_skips_collect_when_nothing_to_collect(self):
        creds_only = os.path.join(self.directory, "creds-only.env")
        write_env(creds_only, {"FEISHU_APP_ID": "id"})
        code, _refreshes = self._run_main(
            self.pool_config, {}, source4=False, env_file=creds_only)
        self.assertEqual(code, 0)
        self.assertEqual(self.executed, ["pool", "ops", "provide"])
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("SKIP collect", handle.read())

    def test_pool_collect_nonzero_is_never_masked(self):
        # node_provider uses 3 for write/API faults too: it must stay fatal.
        for code_value in (2, 3):
            with self.subTest(returncode=code_value):
                self.executed = []
                code, _refreshes = self._run_main(
                    self.pool_config, {"collect": code_value})
                self.assertEqual(code, 1)
                self.assertEqual(self.executed,
                                 ["collect", "pool", "ops", "provide"])

    def test_secret_never_reaches_the_log(self):
        write_env(os.path.join(self.directory, "subscription.env"),
                  {"SECRET": SECRET})
        with mock.patch.object(pipeline, "LOG", self.log_path), \
                mock.patch.object(pipeline, "PRODUCTION_CONFIG", self.subscription), \
                mock.patch.object(pipeline.subprocess, "run",
                                  side_effect=self._runner({})):
            pipeline.main()
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertNotIn(SECRET, handle.read())


if __name__ == "__main__":
    unittest.main()
