"""node_ops: region classification and allocation advice."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_ops import allocate, regions  # noqa: E402
from node_ops.model import NodeEntry, NodeEvaluation  # noqa: E402


def evaluation(name, group, delay, source="飞兔"):
    entry = NodeEntry(name=name, source=source, config={"name": name}, probe_name=name)
    return NodeEvaluation(
        entry=entry, delay_ms=delay, chatgpt_ms=100, openai_ms=100,
        actual_area="", region_group=group, passed=True, streak=2,
    )


class RegionTests(unittest.TestCase):
    def test_name_regex(self):
        self.assertEqual(regions.group_from_name("美国 洛杉矶 01"), "美国")
        self.assertEqual(regions.group_from_name("新加坡-02"), "新加坡")
        self.assertEqual(regions.group_from_name("日本06-×1"), "日本")
        self.assertIsNone(regions.group_from_name("香港 01"))

    def test_measured_area_wins_over_name(self):
        self.assertEqual(
            regions.classify_region_group("美国-伪装", "新加坡/新加坡"), "新加坡"
        )

    def test_measured_other_beats_name(self):
        self.assertEqual(
            regions.classify_region_group("美国-01", "香港/九龙"), "其他"
        )

    def test_unknown_without_name_match(self):
        self.assertEqual(regions.classify_region_group("node-01", "未知"), "未知")
        self.assertEqual(regions.classify_region_group("美国-01", "未知"), "美国")

    def test_format_geo_payload(self):
        text = regions.format_geo_payload(
            {"country": "US", "region": "California", "city": "San Jose"}
        )
        self.assertEqual(text, "美国/加利福尼亚/圣何塞")

    def test_format_cf_trace(self):
        text = regions.format_cf_trace("ip=1.2.3.4\nloc=SG\n")
        self.assertEqual(text, "新加坡/1.2.3.4")

    def test_advice_respects_muse_us_only(self):
        self.assertEqual(regions.advise_groups("美国"), "GPT/US-FAST/MUSE")
        self.assertEqual(regions.advise_groups("新加坡"), "GPT/US-FAST")
        self.assertEqual(regions.advise_groups("日本"), "GPT/US-FAST")
        self.assertEqual(regions.advise_groups("其他"), "")

    def test_tiers(self):
        self.assertGreater(regions.tier_of("美国"), regions.tier_of("新加坡"))
        self.assertGreater(regions.tier_of("新加坡"), regions.tier_of("日本"))
        self.assertEqual(regions.preferred_order(), ("美国", "新加坡", "日本"))


class AllocationTests(unittest.TestCase):
    def test_gpt_prefers_region_tier_over_delay(self):
        us = evaluation("us", "美国", 900)
        jp = evaluation("jp", "日本", 50)
        self.assertIs(allocate.pick_best([jp, us], "GPT"), us)

    def test_muse_is_us_only(self):
        sg = evaluation("sg", "新加坡", 10)
        jp = evaluation("jp", "日本", 10)
        self.assertIsNone(allocate.pick_best([sg, jp], "MUSE"))
        us = evaluation("us", "美国", 500)
        self.assertIs(allocate.pick_best([sg, jp, us], "MUSE"), us)

    def test_rank_within_same_tier_uses_delay(self):
        slow = evaluation("slow", "美国", 800)
        fast = evaluation("fast", "美国", 100)
        ranked = allocate.rank_candidates([slow, fast], "US-FAST")
        self.assertEqual([item.entry.name for item in ranked], ["fast", "slow"])


if __name__ == "__main__":
    unittest.main()
