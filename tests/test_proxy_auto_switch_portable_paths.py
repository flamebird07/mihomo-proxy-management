"""Windows deployment defaults must never become literal POSIX filenames.

Regression: running ``scripts/proxy-auto-switch.py`` on macOS wrote
``C:\\mihomo\\logs\\proxy-failover-rounds.jsonl`` *into the current directory*
(ready 2026-11-22) because POSIX treats a drive-letter path as a relative name.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
SCRIPT = os.path.join(SCRIPTS, "proxy-auto-switch.py")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

spec = importlib.util.spec_from_file_location("proxy_auto_switch_paths", SCRIPT)
pas = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pas
spec.loader.exec_module(pas)


def _load(name: str, filename: str):
    module_spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    return module


@unittest.skipIf(os.name == "nt", "POSIX default mapping only")
class PortableDefaultTest(unittest.TestCase):
    def test_imported_defaults_are_posix_paths(self):
        values = {
            "env": pas.DEFAULT_ENV_FILE,
            "state": pas.DEFAULT_STATE_FILE,
            "log": pas.DEFAULT_LOG_FILE,
            "alert": pas.DEFAULT_ALERT_LOG,
            "target": pas.DEFAULT_ALERT_TARGET,
            "rounds": pas.DEFAULT_ROUNDS_JSONL,
        }
        for label, value in values.items():
            with self.subTest(label=label):
                self.assertNotRegex(value, r"^[A-Za-z]:[\\/]")
                self.assertNotIn("\\", value)
                self.assertTrue(os.path.isabs(value), value)

    def test_rounds_jsonl_keeps_its_name_under_the_state_home(self):
        rounds = Path(pas.DEFAULT_ROUNDS_JSONL)
        self.assertEqual(rounds.name, "proxy-failover-rounds.jsonl")
        self.assertEqual(rounds.parent.name, "logs")
        self.assertEqual(rounds.parent.parent.name, "mihomo")

    def test_windows_user_home_maps_under_the_current_home(self):
        mapped = pas.portable_default(r"C:\Users\Administrator\.hermes\.env")
        self.assertEqual(Path(mapped), Path.home() / ".hermes" / ".env")

    def test_mpm_home_override_is_respected(self):
        with patch.dict(os.environ, {"MPM_HOME": "/tmp/mpm-test-home"}):
            mapped = pas.portable_default(r"C:\mihomo\state\x.json")
        self.assertEqual(mapped, "/tmp/mpm-test-home/state/x.json")

    def test_posix_and_relative_paths_are_untouched(self):
        for raw in ("/var/log/mihomo.log", "logs/mihomo.log", ""):
            with self.subTest(raw=raw):
                self.assertEqual(pas.portable_default(raw), raw)

    def test_installed_sync_script_defaults_are_portable(self):
        base = _load("portable_base_test", "sync-feishu-bitable.py")
        values = {
            "env": base.DEFAULT_ENV_FILE,
            "state": base.DEFAULT_STATE_FILE,
            "sync_state": base.DEFAULT_SYNC_STATE_FILE,
            "log": base.DEFAULT_LOG_FILE,
            "gpt_history": base.DEFAULT_GPT_HISTORY_FILE,
            "live_env": base.DEFAULT_LIVE_ENV,
            "lock": base.DEFAULT_LOCK_FILE,
            "provider_dir": base.DEFAULT_PROVIDER_DIR,
        }
        for label, value in values.items():
            with self.subTest(label=label):
                self.assertNotRegex(value, r"^[A-Za-z]:[\\/]")
                self.assertTrue(os.path.isabs(value), value)
        self.assertNotRegex(base.DEFAULT_CORE, r"^[A-Za-z]:[\\/]")

    def test_health_check_default_core_is_portable(self):
        health = _load("portable_health_test", "health-check-provider.py")
        args = health.parse_args([
            "--candidate", "/tmp/c.yaml",
            "--output", "/tmp/out.yaml",
            "--target", "https://www.gstatic.com/generate_204",
        ])
        self.assertNotRegex(str(args.core), r"^[A-Za-z]:[\\/]")


if __name__ == "__main__":
    unittest.main()
