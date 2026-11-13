import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest
import io
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('source3_switch_test', Path(__file__).parents[1] / 'scripts/proxy-auto-switch.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class BridgeTests(unittest.TestCase):
    def test_positive_delay_with_failed_http_status_is_rejected(self):
        for health, expected in [({'alive': False}, None), ({}, None), ({'alive': True}, 123)]:
            responses = [io.BytesIO(b'{"delay":123}'),
                         io.BytesIO(json.dumps({'alive': True, 'extra': {module.DEFAULT_TARGET: health}}).encode())]
            with patch.object(module.urllib.request, 'urlopen', side_effect=responses) as mock:
                self.assertEqual(module.request_delay('http://127.0.0.1:9090', 'bridge', module.DEFAULT_TARGET, 5000), expected)
                self.assertIn('expected=200', mock.call_args_list[0].args[0].full_url)

    def state(self, region='日本', **extra):
        return {'sync_ms': int(time.time()*1000), 'readback_verified': True,
                'selected': 'measured-node', 'candidates': ['measured-node'],
                'selected_streak': 2, 'selected_region': region, **extra}

    def test_measured_regions_and_two_round_gate(self):
        for region, tier in [('美国', module.TIER_US), ('新加坡', module.TIER_SG), ('日本', module.TIER_JP)]:
            with patch.object(Path, 'read_text', return_value=json.dumps(self.state(region))):
                self.assertEqual(module.tier_of('源3独立GPT'), tier)
                self.assertTrue(module.candidate_stable('源3独立GPT', {}))

    def test_stale_or_unqualified_bridge_is_rejected(self):
        for changes in [{'sync_ms': 1}, {'selected_streak': 1}, {'candidates': []},
                        {'selected_region': '未知'}, {'readback_verified': False}]:
            with patch.object(Path, 'read_text', return_value=json.dumps(self.state(**changes))):
                self.assertIsNone(module.region_of('源3独立GPT'))
                self.assertFalse(module.candidate_stable('源3独立GPT', {}))

    def test_other_candidates_keep_existing_rules(self):
        self.assertEqual(module.tier_of('美国普通节点'), module.TIER_US)
        self.assertFalse(module.candidate_stable('美国普通节点', {'samples': []}))
        self.assertEqual(module.tier_of('日本随意名称'), module.TIER_OTHER)
