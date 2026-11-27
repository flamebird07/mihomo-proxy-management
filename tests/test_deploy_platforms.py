"""deploy.platforms: the concrete macOS / Windows platforms (offline, mocked)."""

import ntpath
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import bootstrap, platforms  # noqa: E402


def make_tmp_repo(parent):
    """A checkout look-alike whose ``mpm_macos.paths`` is the real module."""
    repo = os.path.join(parent, "repo")
    package = os.path.join(repo, "macos", "python", "mpm_macos")
    os.makedirs(package)
    real = os.path.join(REPO_ROOT, "macos", "python", "mpm_macos")
    shutil.copy(os.path.join(real, "paths.py"), package)
    shutil.copy(os.path.join(real, "__init__.py"), package)
    return repo


def macos_layout(root):
    sys.path.insert(0, os.path.join(REPO_ROOT, "macos", "python"))
    from mpm_macos.paths import Layout
    return Layout(root=root)


class TempRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="platforms-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_tmp_repo(self.tmp)
        self.host = os.path.join(self.tmp, "host")  # sandbox host root


class FindRepoRootTests(unittest.TestCase):
    def test_root_holds_the_deploy_kit(self):
        root = platforms.find_repo_root()
        self.assertTrue(os.path.isfile(
            os.path.join(root, "scripts", "deploy", "bootstrap.py")))


class MacosPlatformTests(TempRepoTestCase):
    def test_paths_come_from_the_real_layout(self):
        host_root = os.path.join(self.tmp, "host")
        platform = platforms.MacosPlatform(self.repo, environ={}, root=host_root)
        layout = macos_layout(host_root)
        self.assertEqual(platform.name, "macos")
        self.assertEqual(platform.supply_os, "macos")
        self.assertEqual(platform.arch, "arm64")
        self.assertEqual(platform.pool_path,
                         os.path.join(layout.support, "providers",
                                      "feishu-pool.yaml"))
        self.assertTrue(platform.pool_path.startswith(layout.support + os.sep))
        self.assertEqual(platform.secret_path, layout.secret_file)
        relative = "usr/local/libexec/mihomo-proxy-management/supply"
        self.assertEqual(platform.staging_dir,
                         os.path.join(host_root, *relative.split("/")))
        self.assertTrue(platform.staging_dir.startswith(host_root + os.sep))

    def test_root_slash_gives_the_real_staging_dir(self):
        platform = platforms.MacosPlatform(self.repo, environ={}, root="/")
        self.assertEqual(platform.staging_dir, platforms.MACOS_STAGING)

    def test_python_env_var_wins_over_the_default(self):
        platform = platforms.MacosPlatform(
            self.repo, environ={"MPM_PYTHON": "  /opt/custom/py  "}, root="/")
        self.assertEqual(platform.python, "/opt/custom/py")
        platform = platforms.MacosPlatform(self.repo, environ={}, root="/")
        self.assertEqual(platform.python, platforms.MACOS_DEFAULT_PYTHON)

    def invoke(self, platform, start=True):
        request = bootstrap.InstallRequest(staging_dir="/tmp/staging",
                                           pool_path="/tmp/pool.yaml",
                                           start=start)
        with mock.patch("subprocess.call", return_value=7) as call_mock:
            code = platform.install(request)
        self.assertEqual(code, 7)
        args, kwargs = call_mock.call_args
        argv = args[0]
        return argv, kwargs

    def test_install_invokes_the_installer_with_a_clean_env(self):
        platform = platforms.MacosPlatform(self.repo, environ={}, root="/")
        argv, kwargs = self.invoke(platform)
        self.assertEqual(argv[0], platform.python)
        self.assertEqual(argv[1:4], ["-m", "mpm_macos.cli", "install"])
        self.assertNotIn("--no-start", argv)
        env = kwargs["env"]
        self.assertEqual(env["MPM_MIHOMO_BINARY"],
                         os.path.join("/tmp/staging", "mihomo"))
        pythonpath = env["PYTHONPATH"].split(os.pathsep)
        self.assertIn(os.path.join(self.repo, "common", "python"), pythonpath)
        self.assertIn(os.path.join(self.repo, "macos", "python"), pythonpath)
        self.assertEqual(kwargs["cwd"], self.repo)

    def test_install_start_false_appends_no_start(self):
        platform = platforms.MacosPlatform(self.repo, environ={}, root="/")
        argv, _ = self.invoke(platform, start=False)
        self.assertEqual(argv[-1], "--no-start")

    def test_install_feeds_the_file_main_url_as_the_env_override(self):
        """The installer normalizes the main URL from the secret file: a stale
        placeholder MPM_SUBSCRIPTION_URL must not bypass it."""
        platform = platforms.MacosPlatform(self.repo, environ={}, root=self.host)
        os.makedirs(os.path.dirname(platform.secret_path), exist_ok=True)
        real = "https://sub.real-host.test/one?token=abc"
        with open(platform.secret_path, "w", encoding="utf-8") as stream:
            stream.write(f"SUBSCRIPTION_URL={real}\nSECRET=keep\n")
        stale = platforms.MacosPlatform(
            self.repo,
            environ={"MPM_SUBSCRIPTION_URL": "https://example.com/your-sub"},
            root=self.host)
        _, kwargs = self.invoke(stale)
        self.assertEqual(kwargs["env"]["MPM_SUBSCRIPTION_URL"], real)

    def test_install_pool_mode_feeds_an_empty_main_url(self):
        platform = platforms.MacosPlatform(self.repo, environ={}, root=self.host)
        os.makedirs(os.path.dirname(platform.secret_path), exist_ok=True)
        with open(platform.secret_path, "w", encoding="utf-8") as stream:
            stream.write("SUBSCRIPTION_URL=\nSECRET=keep\n")
        _, kwargs = self.invoke(platform)
        self.assertEqual(kwargs["env"]["MPM_SUBSCRIPTION_URL"], "")

    def test_install_without_a_secret_file_feeds_an_empty_main_url(self):
        platform = platforms.MacosPlatform(self.repo, environ={}, root=self.host)
        _, kwargs = self.invoke(platform)
        self.assertEqual(kwargs["env"]["MPM_SUBSCRIPTION_URL"], "")

    def test_install_reads_a_bom_prefixed_file(self):
        platform = platforms.MacosPlatform(self.repo, environ={}, root=self.host)
        os.makedirs(os.path.dirname(platform.secret_path), exist_ok=True)
        real = "https://sub.real-host.test/one?token=abc"
        with open(platform.secret_path, "wb") as stream:
            stream.write("\ufeff".encode("utf-8")
                         + f"SUBSCRIPTION_URL={real}\n".encode("utf-8"))
        _, kwargs = self.invoke(platform)
        self.assertEqual(kwargs["env"]["MPM_SUBSCRIPTION_URL"], real)


class WindowsPlatformTests(TempRepoTestCase):
    def test_paths_use_ntpath_and_defaults_in_order(self):
        platform = platforms.WindowsPlatform(self.repo, environ={})
        self.assertEqual(platform.install_dir,
                         ntpath.normpath(platforms.WINDOWS_DEFAULT_INSTALL_DIR))
        self.assertEqual(platform.name, "windows")
        self.assertEqual(platform.supply_os, "windows")
        self.assertEqual(platform.arch, "amd64")
        expected_pool = ntpath.join(platform.install_dir, "providers",
                                    "feishu-pool.yaml")
        expected_secret = ntpath.join(platform.install_dir, "subscription.env")
        expected_staging = ntpath.join(platform.install_dir, "staging", "supply")
        self.assertEqual(ntpath.normpath(platform.pool_path),
                         ntpath.normpath(expected_pool))
        self.assertEqual(ntpath.normpath(platform.secret_path),
                         ntpath.normpath(expected_secret))
        self.assertEqual(ntpath.normpath(platform.staging_dir),
                         ntpath.normpath(expected_staging))

    def test_env_var_beats_the_default(self):
        platform = platforms.WindowsPlatform(self.repo,
                                             environ={"MIHOMO_DIR": r"E:\y"})
        self.assertEqual(platform.install_dir, r"E:\y")

    def test_explicit_dir_beats_the_env_var(self):
        platform = platforms.WindowsPlatform(self.repo,
                                             environ={"MIHOMO_DIR": r"E:\y"},
                                             install_dir=r"D:\x")
        self.assertEqual(platform.install_dir, r"D:\x")
        self.assertEqual(platform.staging_dir, r"D:\x\staging\supply")

    def invoke(self, platform, start=True):
        request = bootstrap.InstallRequest(staging_dir=r"C:\mihomo\staging\supply",
                                           pool_path=r"C:\mihomo\providers\pool.yaml",
                                           start=start)
        with mock.patch("subprocess.call", return_value=3) as call_mock:
            code = platform.install(request)
        self.assertEqual(code, 3)
        args, kwargs = call_mock.call_args
        argv = args[0]
        return argv, kwargs

    def test_install_invokes_powershell_with_the_script(self):
        platform = platforms.WindowsPlatform(self.repo, environ={})
        argv, kwargs = self.invoke(platform)
        self.assertEqual(argv[0], platforms.POWERSHELL)
        self.assertEqual(argv[1:5], ["-NoProfile", "-NonInteractive",
                                     "-ExecutionPolicy", "Bypass"])
        script = ntpath.join(self.repo, "scripts", "deploy.ps1")
        self.assertEqual(argv[5:8], ["-File", script, "-StagingDir"])
        self.assertEqual(argv[8], platform.staging_dir)
        self.assertNotIn("-NoStart", argv)
        self.assertEqual(kwargs["cwd"], self.repo)

    def test_install_start_false_appends_no_start(self):
        platform = platforms.WindowsPlatform(self.repo, environ={})
        argv, _ = self.invoke(platform, start=False)
        self.assertEqual(argv[-1], "-NoStart")


class GetPlatformTests(TempRepoTestCase):
    def test_unknown_name_is_rejected(self):
        with self.assertRaises(ValueError):
            platforms.get_platform("nope", self.repo, {})

    def test_macos_and_windows_are_built(self):
        platform = platforms.get_platform("macos", self.repo, {})
        self.assertIsInstance(platform, platforms.MacosPlatform)
        platform = platforms.get_platform("windows", self.repo, {})
        self.assertIsInstance(platform, platforms.WindowsPlatform)


if __name__ == "__main__":
    unittest.main()
