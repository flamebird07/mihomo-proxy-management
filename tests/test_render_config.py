"""The Windows post-processor that turns the subscription provider into the
Feishu pool file provider."""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
SCRIPTS = os.path.join(ROOT, "scripts")
SCRIPT = os.path.join(SCRIPTS, "render_config.py")
TEMPLATE = os.path.join(ROOT, "config", "config.yaml.template")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

spec = importlib.util.spec_from_file_location("render_config_under_test", SCRIPT)
rc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rc
spec.loader.exec_module(rc)

FAKE_SECRET = "unit-test-secret-DO-NOT-PRINT"


def rendered_template():
    with open(TEMPLATE, "r", encoding="utf-8-sig") as handle:
        text = handle.read()
    return (text.replace("__SUBSCRIPTION_URL__", "")
                .replace("__SUBSCRIPTION_INTERVAL__", "60")
                .replace("__SECRET__", FAKE_SECRET))


class ConvertDocumentTest(unittest.TestCase):
    def test_subscription_becomes_pool_file_provider(self):
        document = yaml.safe_load(rendered_template())
        converted, changed = rc.convert_document(document)
        self.assertTrue(changed)
        provider = converted["proxy-providers"]["subscription"]
        self.assertEqual(provider["type"], "file")
        self.assertEqual(provider["path"], "./providers/feishu-pool.yaml")
        self.assertEqual(provider["health-check"], {
            "enable": True,
            "url": "https://www.gstatic.com/generate_204",
            "interval": 300,
            "timeout": 5000,
            "lazy": True,
        })

    def test_every_other_key_is_semantically_equal(self):
        original = yaml.safe_load(rendered_template())
        converted, _changed = rc.convert_document(original)
        for key in original:
            if key == "proxy-providers":
                continue
            with self.subTest(key=key):
                self.assertEqual(original[key], converted[key])
        original_providers = original["proxy-providers"]
        converted_providers = converted["proxy-providers"]
        self.assertEqual(set(converted_providers), set(original_providers))
        for name in original_providers:
            if name == "subscription":
                continue
            self.assertEqual(original_providers[name], converted_providers[name])

    def test_input_mapping_is_not_mutated(self):
        original = yaml.safe_load(rendered_template())
        snapshot = yaml.safe_load(rendered_template())
        rc.convert_document(original)
        self.assertEqual(original, snapshot)

    def test_conversion_is_idempotent(self):
        once, changed_once = rc.convert_text(rendered_template())
        self.assertTrue(changed_once)
        twice, changed_twice = rc.convert_text(once)
        self.assertFalse(changed_twice)
        self.assertEqual(twice, once)

    def test_original_health_check_is_reused(self):
        document = yaml.safe_load(rendered_template())
        document["proxy-providers"]["subscription"]["health-check"]["interval"] = 999
        converted, _changed = rc.convert_document(document)
        health = converted["proxy-providers"]["subscription"]["health-check"]
        self.assertEqual(health["interval"], 999)
        self.assertEqual(health["url"], "https://www.gstatic.com/generate_204")

    def test_residual_subscription_keys_are_cleaned(self):
        document = yaml.safe_load(rendered_template())
        document["proxy-providers"]["subscription"] = {
            "type": "file",
            "path": "./providers/feishu-pool.yaml",
            "url": "https://stale.example/sub",
            "interval": 60,
            "health-check": {"enable": True},
        }
        converted, changed = rc.convert_document(document)
        self.assertTrue(changed)
        provider = converted["proxy-providers"]["subscription"]
        self.assertNotIn("url", provider)
        self.assertNotIn("interval", provider)
        self.assertEqual(provider["path"], "./providers/feishu-pool.yaml")

    def test_missing_provider_raises(self):
        document = yaml.safe_load(rendered_template())
        document["proxy-providers"].pop("subscription")
        with self.assertRaises(rc.RenderConfigError) as caught:
            rc.convert_document(document)
        self.assertEqual(caught.exception.kind, "provider_missing")

    def test_non_mapping_raises(self):
        with self.assertRaises(rc.RenderConfigError) as caught:
            rc.convert_document(["not", "a", "mapping"])
        self.assertEqual(caught.exception.kind, "not_a_mapping")

    def test_invalid_yaml_raises(self):
        with self.assertRaises(rc.RenderConfigError):
            rc.convert_text("a: [unterminated")

    def test_top_level_list_raises(self):
        with self.assertRaises(rc.RenderConfigError) as caught:
            rc.convert_text("- one\n- two\n")
        self.assertEqual(caught.exception.kind, "not_a_mapping")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="render-config-test-")
        self.addCleanup(self._remove_directory)
        self.input_path = os.path.join(self.directory, "config.yaml")
        with open(self.input_path, "w", encoding="utf-8") as handle:
            handle.write(rendered_template())

    def _remove_directory(self):
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def test_output_file_gets_new_text(self):
        output = os.path.join(self.directory, "out.yaml")
        self.assertEqual(rc.main(["--input", self.input_path, "--output", output]), 0)
        with open(output, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        provider = document["proxy-providers"]["subscription"]
        self.assertEqual(provider["type"], "file")
        self.assertEqual(provider["path"], "./providers/feishu-pool.yaml")

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_output_file_is_private(self):
        output = os.path.join(self.directory, "out.yaml")
        self.assertEqual(rc.main(["--input", self.input_path, "--output", output]), 0)
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_default_in_place_rewrite(self):
        self.assertEqual(rc.main(["--input", self.input_path]), 0)
        with open(self.input_path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        provider = document["proxy-providers"]["subscription"]
        self.assertEqual(provider["type"], "file")

    def test_dry_run_does_not_write(self):
        with open(self.input_path, encoding="utf-8") as handle:
            before = handle.read()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = rc.main(["--input", self.input_path, "--dry-run"])
        self.assertEqual(code, 0)
        with open(self.input_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)
        self.assertIn("changed=True", stdout.getvalue())

    def _write_target_shaped_input(self):
        rendered, changed = rc.convert_text(rendered_template())
        self.assertTrue(changed)
        with open(self.input_path, "w", encoding="utf-8") as handle:
            handle.write(rendered)

    def test_explicit_output_is_written_even_when_already_converted(self):
        # Idempotent conversion: changed=False, but an explicit --output is a
        # request to produce the file.
        self._write_target_shaped_input()
        output = os.path.join(self.directory, "out.yaml")
        self.assertEqual(rc.main(["--input", self.input_path, "--output", output]), 0)
        self.assertTrue(os.path.isfile(output))
        with open(output, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        provider = document["proxy-providers"]["subscription"]
        self.assertEqual(provider["type"], "file")
        self.assertEqual(provider["path"], "./providers/feishu-pool.yaml")

    def test_default_in_place_repeat_does_not_rewrite(self):
        self._write_target_shaped_input()
        with mock.patch.object(rc, "write_atomic") as write_atomic:
            self.assertEqual(rc.main(["--input", self.input_path]), 0)
        write_atomic.assert_not_called()

    def test_missing_output_directory_returns_4(self):
        output = os.path.join(self.directory, "missing", "out.yaml")
        code = rc.main(["--input", self.input_path, "--output", output])
        self.assertEqual(code, 4)
        self.assertFalse(os.path.exists(output))

    def test_controller_secret_never_printed(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc.main(["--input", self.input_path, "--dry-run"])
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(FAKE_SECRET, combined)

    def test_unreadable_input_returns_3(self):
        missing = os.path.join(self.directory, "nope.yaml")
        self.assertEqual(rc.main(["--input", missing]), 3)


if __name__ == "__main__":
    unittest.main()
