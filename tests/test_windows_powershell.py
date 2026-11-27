"""Host-neutral static checks for the Windows PowerShell bootstrap wiring.

These never execute PowerShell: they only read the scripts and compare them
against the Python deploy CLI / platform adapter they must stay compatible with.
"""

import importlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ENTRY_SCRIPTS = ("deploy.ps1", "install.ps1", "install-node-pipeline-task.ps1")


def read_text(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return handle.read()


class PowerShellFilesTests(unittest.TestCase):
    def test_every_ps1_decodes_as_utf8_with_or_without_bom(self):
        files = sorted(REPO_ROOT.rglob("*.ps1"))
        self.assertTrue(files)
        for path in files:
            with open(path, "rb") as handle:
                raw = handle.read()
            raw.decode("utf-8-sig")  # raises if not valid UTF-8 (BOM allowed)

    def test_entry_scripts_declare_a_parameter_block(self):
        for name in ENTRY_SCRIPTS:
            text = read_text(SCRIPTS / name)
            self.assertTrue("[CmdletBinding()]" in text or "param(" in text, name)

    def test_new_deploy_ps1_is_bom_crlf(self):
        raw = (SCRIPTS / "deploy.ps1").read_bytes()
        self.assertEqual(raw[:3], b"\xef\xbb\xbf")
        self.assertGreater(raw.count(b"\r\n"), 0)
        self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0)

    def test_install_ps1_keeps_its_bom(self):
        self.assertEqual((SCRIPTS / "install.ps1").read_bytes()[:3], b"\xef\xbb\xbf")


class DeployCliArgumentsTests(unittest.TestCase):
    def _parser_options(self):
        cli = importlib.import_module("deploy.__main__")
        parser = cli.build_parser()
        return {opt for action in parser._actions
                for opt in action.option_strings}

    def test_cli_switches_used_by_deploy_ps1_are_defined_by_build_parser(self):
        text = read_text(SCRIPTS / "deploy.ps1")
        cli_lines = [line for line in text.splitlines() if "$cliArgs" in line]
        self.assertTrue(cli_lines)
        used = set(re.findall(r"--[a-z][a-z0-9-]*", "\n".join(cli_lines)))
        options = self._parser_options()
        self.assertTrue(used)
        self.assertLessEqual(used, options, f"undeclared CLI switches: {used - options}")
        self.assertIn("--platform", used)
        self.assertIn("windows", text)

    def test_platform_choice_windows_is_valid(self):
        platforms = importlib.import_module("deploy.platforms")
        self.assertEqual(platforms.WINDOWS, "windows")

    def test_callback_arguments_match_the_windows_adapter(self):
        platforms = importlib.import_module("deploy.platforms")
        bootstrap = importlib.import_module("deploy.bootstrap")
        platform = platforms.WindowsPlatform(str(REPO_ROOT), environ={})
        request = bootstrap.InstallRequest(staging_dir=platform.staging_dir,
                                           pool_path=platform.pool_path,
                                           start=False)
        with mock.patch("subprocess.call", return_value=0) as call_mock:
            platform.install(request)
        argv = call_mock.call_args[0][0]

        # The adapter must pass -Phase Install explicitly.
        self.assertIn("-Phase", argv)
        self.assertEqual(argv[argv.index("-Phase") + 1], "Install")
        self.assertIn("-StagingDir", argv)
        self.assertIn("-NoStart", argv)

        text = read_text(SCRIPTS / "deploy.ps1")
        for token in ("-Phase", "Install", "-StagingDir", "-NoStart"):
            self.assertIn(token, text, token)

    def test_install_subprocess_output_goes_to_stderr(self):
        platforms = importlib.import_module("deploy.platforms")
        bootstrap = importlib.import_module("deploy.bootstrap")
        platform = platforms.WindowsPlatform(str(REPO_ROOT), environ={})
        request = bootstrap.InstallRequest(staging_dir=platform.staging_dir,
                                           pool_path=platform.pool_path,
                                           start=True)
        with mock.patch("subprocess.call", return_value=0) as call_mock:
            platform.install(request)
        self.assertIs(call_mock.call_args[1]["stdout"], sys.stderr)


class SupplyLockTests(unittest.TestCase):
    def test_python_asset_matches_the_lock(self):
        lock = json.loads((SCRIPTS / "deploy" / "supply.lock.json")
                          .read_text(encoding="utf-8"))
        assets = [asset for asset in lock["assets"]
                  if asset["key"] == "python-windows-amd64"]
        self.assertEqual(len(assets), 1)
        asset = assets[0]
        self.assertTrue(asset["install_as"].endswith(".exe"))
        text = read_text(SCRIPTS / "deploy.ps1")
        self.assertIn("python-windows-amd64", text)
        # The script must use the lock's install_as value, never a literal name.
        self.assertIn("$asset.install_as", text)


class NoHardcodedSecretsTests(unittest.TestCase):
    def test_deploy_ps1_has_no_user_profile_or_urls_or_secrets(self):
        text = read_text(SCRIPTS / "deploy.ps1")
        # A hard-coded profile dir, a Feishu/subscription address, or a secret
        # assignment must never live in the bootstrap entrypoint.
        self.assertNotIn("C:\\Users\\", text)
        self.assertNotRegex(text, r"(?i)feishu\.(cn|com)|open\.feishu")
        self.assertNotRegex(text, r"(?i)SUBSCRIPTION_URL")
        self.assertNotRegex(text, r"(?i)feishu_app_secret")
        self.assertNotRegex(text, r"(?i)app_secret\s*=")


class InteractiveBlockingTests(unittest.TestCase):
    def test_install_and_common_do_not_block_on_read_host(self):
        for name in ("install.ps1", os.path.join("lib", "common.ps1")):
            text = read_text(SCRIPTS / name)
            self.assertNotIn("Read-Host", text, name)

    def test_install_ps1_keeps_the_notepad_handoff(self):
        self.assertIn("notepad.exe", read_text(SCRIPTS / "install.ps1"))


class BootstrapTaskIdentityTests(unittest.TestCase):
    """The two bootstrap tasks must be Interactive/Limited, never SYSTEM."""

    TASK_SCRIPTS = ("sync-feishu-bitable.ps1", "install-node-pipeline-task.ps1")

    def test_task_entry_scripts_are_utf8_bom(self):
        # Windows PowerShell 5.1 reads a BOM-less file as ANSI; the non-ASCII
        # comments would then corrupt parsing.
        for name in self.TASK_SCRIPTS:
            raw = (SCRIPTS / name).read_bytes()
            self.assertEqual(raw[:3], b"\xef\xbb\xbf", name)
            raw.decode("utf-8-sig")

    def test_no_hardcoded_elevated_task_identity(self):
        for name in self.TASK_SCRIPTS:
            text = read_text(SCRIPTS / name)
            self.assertNotRegex(text, r"-UserId\s+'SYSTEM'", name)
            self.assertNotRegex(text, r"-RunLevel\s+'Highest'", name)
        # The sync task builds the deploying user's principal from the helper;
        # the pipeline refuses to inherit an elevated sync principal.
        sync = read_text(SCRIPTS / "sync-feishu-bitable.ps1")
        self.assertIn("New-BootstrapTaskPrincipalSpec", sync)
        self.assertIn("Assert-BootstrapTaskPrincipal", sync)
        pipeline = read_text(SCRIPTS / "install-node-pipeline-task.ps1")
        self.assertIn("Test-ElevatedTaskIdentity", pipeline)

    def test_sync_registers_the_current_user_after_the_acl_grants(self):
        text = read_text(SCRIPTS / "sync-feishu-bitable.ps1")
        segment = text.split("function Register-BootstrapSyncTask", 1)[1]
        self.assertIn("Grant-BootstrapTaskAcls", segment)
        self.assertIn("Assert-BootstrapTaskPrincipal", segment)
        self.assertIn("Register-ScheduledTask", segment)
        self.assertLess(segment.index("Grant-BootstrapTaskAcls"),
                        segment.index("Register-ScheduledTask"))
        self.assertLess(segment.index("Assert-BootstrapTaskPrincipal"),
                        segment.index("Register-ScheduledTask"))
        # The action is a direct powershell -File, never a generated .cmd.
        self.assertIn("New-PowerShellFileActionSpec", segment)
        self.assertNotIn(".cmd", segment)

    def test_pipeline_grants_acls_before_registering(self):
        text = read_text(SCRIPTS / "install-node-pipeline-task.ps1")
        self.assertIn("Grant-BootstrapTaskAcls", text)
        self.assertIn("Test-ElevatedTaskIdentity", text)
        self.assertLess(text.index("Grant-BootstrapTaskAcls"),
                        text.index("Register-ScheduledTask"))
        self.assertIn("New-PowerShellFileActionSpec", text)
        self.assertNotIn(".cmd", text)

    def test_run_mode_keeps_sync_then_switch_and_mihomo_dir(self):
        text = read_text(SCRIPTS / "sync-feishu-bitable.ps1")
        self.assertIn("if ($Run)", text)
        self.assertIn("$env:MIHOMO_DIR = $dir", text)
        run_segment = text.split("function Invoke-RunMode", 1)[1].split("function ", 1)[0]
        self.assertIn("sync-feishu-bitable.py", run_segment)
        self.assertIn("proxy-auto-switch.py", run_segment)
        self.assertLess(run_segment.index("sync-feishu-bitable.py"),
                        run_segment.index("proxy-auto-switch.py"))
        # Non-zero from either stage becomes the task's exit code.
        self.assertRegex(run_segment, r"if \(\$syncExit -ne 0\)")
        self.assertRegex(run_segment, r"if \(\$switchExit -ne 0\)")

    def test_legacy_manual_entry_is_preserved(self):
        text = read_text(SCRIPTS / "sync-feishu-bitable.ps1")
        self.assertIn("run-feishu-bitable-sync.bat", text)
        self.assertIn("/RL LIMITED", text)
        self.assertIn("'python not found in PATH'", text)


class BootstrapDeployPs1Tests(unittest.TestCase):
    def test_store_python_placeholder_is_skipped(self):
        text = read_text(SCRIPTS / "deploy.ps1")
        self.assertIn("WindowsApps", text)
        # Skipped before the version probe, inside the candidate loop.
        loop = text.split("foreach ($candidate in $candidates)", 1)[1]
        self.assertLess(loop.index("WindowsApps"), loop.index("version_info"))

    def test_install_dir_is_normalized_and_pinned_for_the_cli(self):
        text = read_text(SCRIPTS / "deploy.ps1")
        segment = text.split("function Invoke-Bootstrap", 1)[1].split(
            "function Invoke-Install", 1)[0]
        self.assertIn("[System.IO.Path]::GetFullPath", segment)
        self.assertIn("$env:MIHOMO_DIR = $InstallDir", segment)
        self.assertIn("$env:MPM_PYTHON = $python", segment)
        self.assertNotIn("$explicitDir", segment)

    def test_install_phase_honours_pinned_env_before_get_installdir(self):
        text = read_text(SCRIPTS / "deploy.ps1")
        segment = text.split("function Invoke-Install", 1)[1]
        # The pinned MIHOMO_DIR wins; Get-InstallDir is only the fallback.
        self.assertIn(
            "if ($env:MIHOMO_DIR) { $InstallDir = $env:MIHOMO_DIR } else { $InstallDir = Get-InstallDir }",
            segment)
        self.assertIn("[System.IO.Path]::GetFullPath", segment)


if __name__ == "__main__":
    unittest.main()
