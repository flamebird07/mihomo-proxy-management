"""node_ops: validity gate and consecutive-pass streak restored from Feishu rows."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_ops.gate import (  # noqa: E402
    GPT_REQUIRED_CONSECUTIVE,
    evaluate_nodes,
    field_number,
    field_text,
    previous_passes,
    previous_streaks,
    valid_evaluations,
)
from node_ops.model import NodeEntry  # noqa: E402
from node_ops.probe import ProbeResult  # noqa: E402


def entry(name, source="飞兔"):
    return NodeEntry(name=name, source=source,
                     config={"name": name, "type": "trojan", "server": "1.1.1.1", "port": 443},
                     probe_name=name)


class FieldParsingTests(unittest.TestCase):
    def test_text_field_list_form(self):
        self.assertEqual(field_text([{"text": "美国", "type": "text"}]), "美国")
        self.assertEqual(field_text("飞兔"), "飞兔")
        self.assertEqual(field_text(None), "")

    def test_number_field(self):
        self.assertEqual(field_number(3), 3)
        self.assertEqual(field_number(3.0), 3)
        self.assertEqual(field_number(True), None)
        self.assertEqual(field_number("3"), 3)
        self.assertEqual(field_number(" 3.0 "), 3)
        self.assertEqual(field_number("abc"), None)
        self.assertEqual(field_number(["2"]), 2)


class StreakTests(unittest.TestCase):
    def test_previous_streaks_from_rows(self):
        rows = [
            {"fields": {"来源": "飞兔", "节点名": [{"text": "a"}], "连续通过轮数": 3}},
            {"fields": {"来源": [{"text": "星辰"}], "节点名": "b", "连续通过轮数": 0}},
            {"fields": {"来源": "飞兔", "节点名": "bad"}},
            {"fields": "not-a-dict"},
        ]
        streaks = previous_streaks(rows)
        self.assertEqual(streaks[("飞兔", "a")], 3)
        self.assertEqual(streaks[("星辰", "b")], 0)
        self.assertNotIn(("飞兔", "bad"), streaks)

    def test_previous_passes_from_table_one(self):
        rows = [
            {"fields": {"来源": "飞兔", "节点名": "a",
                        "chatgpt延迟ms": 200, "openai延迟ms": 300}},
            {"fields": {"来源": "飞兔", "节点名": "b",
                        "chatgpt延迟ms": 200, "openai延迟ms": -1}},
            {"fields": {"来源": "飞兔", "节点名": "c",
                        "chatgpt延迟ms": -1, "openai延迟ms": -1}},
        ]
        passed = previous_passes(rows)
        self.assertTrue(passed[("飞兔", "a")])
        self.assertFalse(passed[("飞兔", "b")])
        self.assertFalse(passed[("飞兔", "c")])

    def test_absent_from_valid_but_passed_last_round_reaches_gate(self):
        entries = [entry("a")]
        result = ProbeResult(chatgpt={"a": 1}, openai={"a": 1},
                             delays={"a": 1}, regions={"a": "美国"})
        # No 「有效节点」 row (streak was 1), but table one shows last round passed.
        evaluations = evaluate_nodes(entries, result, {}, {("飞兔", "a"): True})
        self.assertEqual(evaluations[0].streak, 2)
        self.assertTrue(evaluations[0].gpt_available)

    def test_first_ever_pass_stays_at_one(self):
        entries = [entry("a")]
        result = ProbeResult(chatgpt={"a": 1}, openai={"a": 1},
                             delays={"a": 1}, regions={"a": "美国"})
        evaluations = evaluate_nodes(entries, result, {}, {})
        self.assertEqual(evaluations[0].streak, 1)
        self.assertFalse(evaluations[0].gpt_available)

    def test_pass_increments_and_fail_resets(self):
        entries = [entry("a"), entry("b")]
        result = ProbeResult(
            delays={"a": 100, "b": 100},
            chatgpt={"a": 200, "b": None},
            openai={"a": 300, "b": 300},
            regions={"a": "美国/加利福尼亚", "b": "美国/加利福尼亚"},
        )
        evaluations = evaluate_nodes(entries, result, {("飞兔", "a"): 1, ("飞兔", "b"): 5})
        by_name = {item.entry.name: item for item in evaluations}
        self.assertTrue(by_name["a"].passed)
        self.assertEqual(by_name["a"].streak, 2)
        self.assertTrue(by_name["a"].gpt_available)
        self.assertFalse(by_name["b"].passed)
        self.assertEqual(by_name["b"].streak, 0)
        self.assertFalse(by_name["b"].gpt_available)

    def test_gate_requires_two_consecutive(self):
        entries = [entry("a")]
        result = ProbeResult(chatgpt={"a": 1}, openai={"a": 1}, delays={"a": 1})
        first = evaluate_nodes(entries, result, {})
        self.assertEqual(first[0].streak, 1)
        self.assertFalse(first[0].gpt_available)
        second = evaluate_nodes(entries, result, {("飞兔", "a"): 1})
        self.assertTrue(second[0].gpt_available)

    def test_valid_evaluations_filters(self):
        entries = [entry("a"), entry("b")]
        result = ProbeResult(chatgpt={"a": 1}, openai={"a": 1},
                             delays={"a": 1}, regions={"a": "美国"})
        evaluations = evaluate_nodes(entries, result, {("飞兔", "a"): 1})
        valid = valid_evaluations(evaluations)
        self.assertEqual([item.entry.name for item in valid], ["a"])

    def test_required_consecutive_constant(self):
        self.assertEqual(GPT_REQUIRED_CONSECUTIVE, 2)

    def test_identity_is_source_and_name(self):
        entries = [entry("same", "飞兔"), entry("same", "星辰")]
        result = ProbeResult(chatgpt={"same": 1}, openai={"same": 1}, delays={"same": 1})
        evaluations = evaluate_nodes(entries, result, {("星辰", "same"): 4})
        by_source = {item.entry.source: item for item in evaluations}
        self.assertEqual(by_source["飞兔"].streak, 1)
        self.assertEqual(by_source["星辰"].streak, 5)


if __name__ == "__main__":
    unittest.main()
