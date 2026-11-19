"""node_provider --supplement-yaml: local nodes merge into source 1.

Upstream-conformant offline tests in the existing collect-test style: the
fetcher is fake, Feishu is stubbed, and the supplement is a synthetic
provider YAML under tmp_path.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
for entry in (str(SCRIPTS),):
    if entry not in sys.path:
        sys.path.insert(0, entry)

node_provider_main = importlib.import_module("node_provider.__main__")
from node_provider import collect as collect_mod  # noqa: E402
from node_provider import runner as runner_mod  # noqa: E402
from node_provider import schema  # noqa: E402

SUPPLEMENT_YAML = """proxies:
  - name: 补充-01
    type: ss
    server: 203.0.113.9
    port: 8388
    cipher: aes-128-gcm
    password: fake
"""


class SupplementTests(unittest.TestCase):
    def _settings(self, **overrides):
        from node_provider.runner import Settings

        defaults = dict(collect=True, sources_env=None, supplement_yaml=[])
        defaults.update(overrides)
        return Settings(**defaults)

    def _result(self, nodes):
        result = collect_mod.CollectResult()
        result.nodes = nodes
        return result

    def _node(self, name, port=8000):
        return collect_mod.CollectedNode(
            config={"name": name, "type": "ss", "server": "203.0.113.1",
                    "port": port, "cipher": "aes-128-gcm", "password": "x"},
            source=schema.SOURCE_BY_INDEX[2],
            source_number=2,
        )

    def _write_supplement(self, tmp_path, text=SUPPLEMENT_YAML):
        path = tmp_path / "nextin.yaml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_load_supplement_parses_provider_yaml(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_supplement(Path(tmp))
            nodes = runner_mod._load_supplement([path], lambda _m: None)
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].source_number, 1)
            self.assertEqual(nodes[0].source, schema.SOURCE_BY_INDEX[1])
            self.assertEqual(nodes[0].config["name"], "补充-01")

    def test_load_supplement_tolerates_missing_and_malformed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            warns = []
            missing = runner_mod._load_supplement(
                [str(Path(tmp) / "nope.yaml")], warns.append)
            self.assertEqual(missing, [])
            self.assertTrue(any("unreadable" in w for w in warns))
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("[unclosed", encoding="utf-8")
            warns.clear()
            self.assertEqual(runner_mod._load_supplement([str(bad)], warns.append), [])
            self.assertTrue(any("unparsable" in w for w in warns))
            empty = Path(tmp) / "empty.yaml"
            empty.write_text("proxies: []\n", encoding="utf-8")
            warns.clear()
            self.assertEqual(runner_mod._load_supplement([str(empty)], warns.append), [])
            self.assertTrue(any("supplement empty" in w for w in warns))

    def test_supplement_enters_source_one_and_fetched_nodes_win_dedupe(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_supplement(Path(tmp))
            settings = self._settings(supplement_yaml=[path])
            # fetched: one source-2 node + one source-1 node identical in
            # config to the supplement node (differing name) -> the fetched
            # one must survive, the supplement copy is the duplicate.
            fetched = [
                self._node("cc-01"),
                collect_mod.CollectedNode(
                    config={"name": "fetched-dup", "type": "ss",
                            "server": "203.0.113.9", "port": 8388,
                            "cipher": "aes-128-gcm", "password": "fake"},
                    source=schema.SOURCE_BY_INDEX[1], source_number=1),
            ]
            with patch.object(collect_mod, "collect", return_value=self._result(fetched)), \
                 patch.object(runner_mod, "_record_attempts") as record, \
                 patch("node_provider.feishu.FeishuClient") as client_cls, \
                 patch.object(runner_mod.collect_state, "read_skip_set", return_value=set()), \
                 patch.object(runner_mod, "ensure_table",
                              return_value={"table_id": "tblX"}), \
                 patch.object(runner_mod, "tombstone_stale_rows", return_value=0), \
                 patch.object(runner_mod, "upsert_source_rows",
                              side_effect=lambda _c, _t, number, rows, _n, _l: len(rows)) as replace:
                logger = runner_mod.Logger(None) if hasattr(runner_mod, "Logger") else None
                code = runner_mod.run_collect(
                    settings, client=object(), logger=_NullLogger())
                self.assertEqual(code, runner_mod.EXIT_OK)
            record.assert_called_once()
            written = {call.args[2]: len(call.args[3])
                       for call in replace.call_args_list}
            self.assertEqual(written.get(1), 1, "fetched dup beats supplement copy")
            self.assertEqual(written.get(2), 1)

    def test_supplement_alone_writes_source_one_when_fetch_fails(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_supplement(Path(tmp))
            settings = self._settings(supplement_yaml=[path])
            with patch.object(collect_mod, "collect", return_value=self._result([])), \
                 patch.object(runner_mod, "_record_attempts"), \
                 patch("node_provider.feishu.FeishuClient"), \
                 patch.object(runner_mod.collect_state, "read_skip_set", return_value=set()), \
                 patch.object(runner_mod, "ensure_table",
                              return_value={"table_id": "tblX"}), \
                 patch.object(runner_mod, "tombstone_stale_rows", return_value=0), \
                 patch.object(runner_mod, "upsert_source_rows",
                              side_effect=lambda _c, _t, number, rows, _n, _l: len(rows)) as replace:
                code = runner_mod.run_collect(
                    settings, client=object(), logger=_NullLogger())
                self.assertEqual(code, runner_mod.EXIT_OK)
            written = {call.args[2]: len(call.args[3])
                       for call in replace.call_args_list}
            self.assertEqual(written, {1: 1}, "supplement-only still writes source 1")

    def test_all_skipped_with_supplement_still_writes(self):
        """Rolling-24h skip no longer short-circuits when a supplement exists."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_supplement(Path(tmp))
            settings = self._settings(supplement_yaml=[path])
            empty = self._result([])
            empty.skipped.append("subscription:飞兔")
            with patch.object(collect_mod, "collect", return_value=empty), \
                 patch.object(runner_mod, "_record_attempts"), \
                 patch("node_provider.feishu.FeishuClient"), \
                 patch.object(runner_mod.collect_state, "read_skip_set",
                              return_value={("subscription", 1)}), \
                 patch.object(runner_mod, "ensure_table",
                              return_value={"table_id": "tblX"}), \
                 patch.object(runner_mod, "tombstone_stale_rows", return_value=0), \
                 patch.object(runner_mod, "upsert_source_rows",
                              side_effect=lambda _c, _t, number, rows, _n, _l: len(rows)) as replace:
                code = runner_mod.run_collect(
                    settings, client=object(), logger=_NullLogger())
                self.assertEqual(code, runner_mod.EXIT_OK)
            written = {call.args[2]: len(call.args[3])
                       for call in replace.call_args_list}
            self.assertEqual(written, {1: 1})


class _NullLogger:
    def info(self, _message):
        pass

    def warn(self, _message):
        pass

    def error(self, _message):
        pass


if __name__ == "__main__":
    unittest.main()
