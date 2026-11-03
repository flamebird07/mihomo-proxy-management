"""Unit tests for GPT/OpenAI reachability, history gating, and source-4 alignment."""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "sync-feishu-bitable.py")
spec = importlib.util.spec_from_file_location("sync_feishu_bitable_gpt", SCRIPT)
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)


class RecordingLog:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("INFO", message))

    def warn(self, message):
        self.lines.append(("WARN", message))

    def error(self, message):
        self.lines.append(("ERROR", message))

    def text(self):
        return "\n".join(message for _level, message in self.lines)


class TableAndProbeTests(unittest.TestCase):
    def test_new_fields_and_safe_worker_default(self):
        node_spec = next(item for item in sync.TABLE_SPECS if item["key"] == "nodes")
        region_spec = next(item for item in sync.TABLE_SPECS if item["key"] == "gpt_regions")
        self.assertIn("chatgpt延迟ms", [field[0] for field in node_spec["fields"]])
        self.assertIn("openai延迟ms", [field[0] for field in node_spec["fields"]])
        self.assertIn("连续通过轮数", [field[0] for field in region_spec["fields"]])
        self.assertEqual(sync.parse_args([]).workers, 4)

    def test_three_targets_and_worker_clamp(self):
        log = RecordingLog()
        expected_by_target = {}

        def fake_delay(_api, name, target, _timeout, _token, expected):
            expected_by_target[target] = expected
            return len(name) + len(target)

        with patch.object(sync, "request_delay", side_effect=fake_delay):
            values = sync.test_delay_targets(
                "http://controller", ["a", "b"],
                {"grok": "g", "chatgpt": "c", "openai": "o"},
                100, 8, log, budget_s=30,
            )
        self.assertEqual(set(values), {"grok", "chatgpt", "openai"})
        self.assertTrue(all(values[label]["a"] for label in values))
        self.assertIn("workers_clamped requested=8 effective=4", log.text())
        self.assertEqual(expected_by_target["o"], "200-299/421")

    def test_node_records_write_failed_as_minus_one(self):
        pool = [({"name": "n1", "type": "ss"}, "飞兔")]
        row = sync.build_node_records(
            pool, {"n1": 10}, sync.DEFAULT_TARGET, 1, {"n1": "美国"},
            {"n1": 20}, {"n1": None},
        )[0]["fields"]
        self.assertEqual(row["chatgpt延迟ms"], 20)
        self.assertEqual(row["openai延迟ms"], -1)

    def test_geo_priority_accepts_any_reachable_target(self):
        combined = sync.combine_reachable_delays(
            ["grok", "chatgpt", "openai", "dead"],
            {"grok": 1}, {"chatgpt": 2}, {"openai": 3},
        )
        self.assertEqual(combined["grok"], 1)
        self.assertEqual(combined["chatgpt"], 2)
        self.assertEqual(combined["openai"], 3)
        self.assertIsNone(combined["dead"])


class HistoryGateTests(unittest.TestCase):
    def test_requires_two_consecutive_dual_passes(self):
        first_streak, first_pass, history = sync.advance_gpt_history(
            ["n1"], {"n1": 11}, {"n1": 12}, 1000, {"version": 1, "nodes": {}}
        )
        second_streak, second_pass, history = sync.advance_gpt_history(
            ["n1"], {"n1": 13}, {"n1": 14}, 2000, history
        )
        pool = [({"name": "n1", "type": "ss"}, "飞兔")]
        first = sync.build_region_records(
            pool, {"n1": 1}, {}, 1000, {"n1": "美国"}, first_pass, first_streak
        )[0]["fields"]
        second = sync.build_region_records(
            pool, {"n1": 1}, {}, 2000, {"n1": "美国"}, second_pass, second_streak
        )[0]["fields"]
        self.assertFalse(first["GPT可用"])
        self.assertEqual(first["连续通过轮数"], 1)
        self.assertTrue(second["GPT可用"])
        self.assertEqual(second["连续通过轮数"], 2)
        event = history["nodes"]["n1"][-1]
        self.assertEqual(set(event), {"time", "passed"})

    def test_failure_resets_streak(self):
        history = {"version": 1, "nodes": {"n1": [
            {"time": 1, "passed": True}, {"time": 2, "passed": True}
        ]}}
        streaks, passed, _next = sync.advance_gpt_history(
            ["n1"], {"n1": 20}, {"n1": None}, 3, history
        )
        self.assertFalse(passed["n1"])
        self.assertEqual(streaks["n1"], 0)


class SourceFourAlignmentTests(unittest.TestCase):
    def test_aligns_by_server_port_and_keeps_unmatched_name(self):
        result = sync.SourceResult(4, "西瓜加速")
        result.nodes = [
            {"name": "VV1-美国04", "type": "vless", "server": "a.example", "port": 443},
            {"name": "原名保留", "type": "vless", "server": "b.example", "port": 8443},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.yaml")
            with open(config, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"proxies": [{
                    "name": "西瓜-美国02", "type": "vless",
                    "server": "a.example", "port": 443, "password": "not-logged",
                }]}, handle, allow_unicode=True, sort_keys=False)
            log = RecordingLog()
            sync.align_source4_names([result], config, log)
        self.assertEqual(result.nodes[0]["name"], "西瓜-美国02")
        self.assertEqual(result.nodes[1]["name"], "原名保留")
        self.assertIn("matched=1 unmatched=1", log.text())
        self.assertNotIn("not-logged", log.text())


if __name__ == "__main__":
    unittest.main()
