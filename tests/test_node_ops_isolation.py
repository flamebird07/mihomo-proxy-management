"""node_ops must be self-contained: no imports from any existing script.

S3 also makes node_ops source-agnostic: it must not contain subscription
collection or app-extraction code paths.  This is the executable form of the
"zero import" and "no collection path" grep evidence.
"""

import ast
import os
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = Path(HERE).parent / "scripts" / "node_ops"

FORBIDDEN_MODULES = {
    "convert_sub",
    "sync-feishu-bitable",
    "sync_feishu_bitable",
    "proxy-auto-switch",
    "proxy_auto_switch",
    "gpt_stability_policy",
    "source1_independent",
    "source3_compatible",
    "live_status_collect",
    "live_status_alert",
    "live_status_publish",
    "ippool-collector",
    "ippool_collector",
    "scripts.lib",
}

FORBIDDEN_LOCAL_DATA_TOKENS = (
    "feishu-gpt-history",
    "feishu_gpt_history",
    "sub1_check",
    "source1-cache",
    "source2-cache",
    "source3-cache",
    "source4-cache",
    "-cache.yaml",
    "-cache.json",
)


def package_files():
    return sorted(PACKAGE.glob("*.py"))


def imported_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = "." * node.level + module
            names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}")
    return names


def is_forbidden(name):
    if name.startswith("."):
        return False  # relative import inside node_ops itself
    return any(
        name == bad or name.startswith(bad + ".") or name.startswith(bad + "-")
        for bad in FORBIDDEN_MODULES
    )


class IsolationTests(unittest.TestCase):
    def test_package_has_modules(self):
        names = {path.name for path in package_files()}
        for expected in ("__init__.py", "schema.py", "probe.py", "gate.py",
                         "records.py", "feishu.py", "runner.py", "__main__.py"):
            self.assertIn(expected, names)

    def test_collection_modules_are_gone(self):
        names = {path.name for path in package_files()}
        self.assertNotIn("subscription.py", names)
        self.assertNotIn("app_extract.py", names)

    def test_no_imports_from_existing_scripts(self):
        offenders = []
        for path in package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name in imported_names(tree):
                if is_forbidden(name):
                    offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [])

    def test_no_import_of_node_provider_either_direction(self):
        for path in package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name in imported_names(tree):
                self.assertFalse(name.startswith("node_provider"),
                                 f"{path.name} imports A part: {name}")

    def test_no_collection_or_extraction_tokens(self):
        tokens = ("fetch_subscription", "convert_content", "source_settings",
                  "SUBSCRIPTION_URL", "from_app", "ReadProcessMemory",
                  "extract_source1", "extract_source3", "AESGCM")
        offenders = []
        for path in package_files():
            source = path.read_text(encoding="utf-8")
            for token in tokens:
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [])

    def test_no_local_node_data_paths(self):
        offenders = []
        for path in package_files():
            source = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_LOCAL_DATA_TOKENS:
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [])

    def test_only_stdlib_and_yaml_third_party(self):
        allowed_third_party = {"yaml"}
        stdlib = set(getattr(__import__("sys"), "stdlib_module_names", ()))
        for path in package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name in imported_names(tree):
                if name.startswith("."):
                    continue
                root = name.split(".", 1)[0]
                if root in ("node_ops",) or root in allowed_third_party:
                    continue
                self.assertIn(root, stdlib, f"{path.name} imports unexpected module {name}")


if __name__ == "__main__":
    unittest.main()
