"""node_provider: schema contract with node_ops and zero runtime import.

Importing node_ops is allowed in *tests only*, for contract assertions.  The
package itself must import nothing from scripts/node_ops or any other existing
script; this is checked by AST inspection below (the executable form of the
grep evidence).
"""

import ast
import os
import sys
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_ops import schema as ops_schema  # noqa: E402
from node_provider import schema as provider_schema  # noqa: E402

PACKAGE = Path(SCRIPTS) / "node_provider"

FORBIDDEN_MODULES = {
    "node_ops",
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
        return False
    return any(
        name == bad or name.startswith(bad + ".") or name.startswith(bad + "-")
        for bad in FORBIDDEN_MODULES
    )


class ContractTests(unittest.TestCase):
    def test_valid_field_names_match_node_ops(self):
        ops_fields = tuple((f[0], f[1]) for f in ops_schema.VALID_TABLE["fields"])
        self.assertEqual(provider_schema.VALID_FIELDS, ops_fields)
        self.assertEqual(provider_schema.VALID_FIELD_NAMES,
                         ops_schema.field_names("valid_nodes"))

    def test_nodes_field_names_match_node_ops(self):
        ops_fields = tuple((f[0], f[1]) for f in ops_schema.NODES_TABLE["fields"])
        self.assertEqual(provider_schema.NODES_FIELDS, ops_fields)
        self.assertEqual(provider_schema.field_names("nodes"),
                         ops_schema.field_names("nodes"))

    def test_source_number_is_a_number_field_in_both_tables(self):
        for table in (provider_schema.NODES_TABLE, provider_schema.VALID_TABLE):
            by_name = {f[0]: f[1] for f in table["fields"]}
            self.assertEqual(by_name["源编号"], provider_schema.FT_NUMBER)
            self.assertEqual(by_name["源编号"], ops_schema.FT_NUMBER)

    def test_table_names_and_options_match(self):
        self.assertEqual(provider_schema.VALID_TABLE_NAME,
                         ops_schema.VALID_TABLE["name"])
        self.assertEqual(provider_schema.NODES_TABLE_NAME,
                         ops_schema.NODES_TABLE["name"])
        self.assertEqual(provider_schema.SOURCE_OPTIONS,
                         ops_schema.SOURCE_OPTIONS)
        self.assertEqual(provider_schema.REGION_GROUP_OPTIONS,
                         ops_schema.REGION_GROUP_OPTIONS)
        self.assertEqual(provider_schema.SOURCE_BY_INDEX,
                         ops_schema.SOURCE_BY_INDEX)

    def test_field_types_match(self):
        ops_by_name = {f[0]: f[1] for f in ops_schema.VALID_TABLE["fields"]}
        for name, ftype in provider_schema.VALID_FIELDS:
            self.assertEqual(ftype, ops_by_name[name], name)

    def test_row_ownership_partition_is_complete(self):
        identity = set(provider_schema.NODES_IDENTITY_FIELDS)
        measurement = set(provider_schema.NODES_MEASUREMENT_FIELDS)
        self.assertEqual(identity | measurement,
                         set(provider_schema.field_names("nodes")))
        self.assertEqual(identity & measurement, set())
        self.assertEqual(identity, set(ops_schema.NODES_IDENTITY_FIELDS))
        self.assertEqual(measurement, set(ops_schema.NODES_MEASUREMENT_FIELDS))


class IsolationTests(unittest.TestCase):
    def test_package_has_expected_modules(self):
        names = {path.name for path in package_files()}
        for expected in ("__init__.py", "__main__.py", "schema.py", "envfile.py",
                         "feishu.py", "logutil.py", "builder.py", "writer.py",
                         "runner.py", "subscription.py", "app_extract.py",
                         "collect.py", "lock.py"):
            self.assertIn(expected, names)

    def test_no_runtime_import_from_existing_scripts(self):
        offenders = []
        for path in package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name in imported_names(tree):
                if is_forbidden(name):
                    offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [])

    def test_no_local_state_tokens(self):
        offenders = []
        for path in package_files():
            source = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_LOCAL_DATA_TOKENS:
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [])

    def test_only_stdlib_and_allowed_third_party(self):
        allowed_third_party = {"yaml", "cryptography"}
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        for path in package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name in imported_names(tree):
                if name.startswith("."):
                    continue
                root = name.split(".", 1)[0]
                if root in ("node_provider",) or root in allowed_third_party:
                    continue
                self.assertIn(root, stdlib,
                              f"{path.name} imports unexpected module {name}")

    def test_no_controller_endpoints(self):
        """No mihomo controller URL/port literals in the A part."""
        suspicious = ("127.0.0.1:9090", "127.0.0.1:19090", "/proxies/",
                      "controller-port", "external-controller")
        offenders = []
        for path in package_files():
            source = path.read_text(encoding="utf-8")
            for token in suspicious:
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
