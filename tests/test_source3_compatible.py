import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('source3', Path(__file__).parents[1] / 'scripts/source3_compatible.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PriorityTests(unittest.TestCase):
    def test_japan_is_last_tier_and_must_pass_twice(self):
        names = ['jp', 'sg', 'us', 'jp-once', 'jp-failed', '日本假名']
        regions = dict(zip(names, ['日本', '新加坡', '美国', '日本', '日本', '未知']))
        passed = dict.fromkeys(names, True)
        passed['jp-failed'] = False
        streaks = dict.fromkeys(names, 2)
        streaks['jp-once'] = 1
        candidates = module.eligible_candidates(names, regions, passed, streaks,
            {'jp': 1, 'sg': 10, 'us': 100}, lambda x: x, 2)
        self.assertEqual(candidates, ['us', 'sg', 'jp'])
        self.assertEqual(module.choose_candidate(['jp'], 'us', regions, lambda x: x), 'jp')

    def test_keep_current_same_tier(self):
        regions = {'fast': '美国', 'current': '美国', 'jp': '日本'}
        self.assertEqual(module.choose_candidate(['fast', 'current', 'jp'], 'current', regions, lambda x: x), 'current')
        self.assertEqual(module.choose_candidate(['fast', 'jp'], 'jp', regions, lambda x: x), 'fast')

    def test_measured_japan_record_is_opt_in(self):
        import runpy
        helpers = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/sync-feishu-bitable.py'))
        classify = helpers['group_from_actual_area']
        self.assertIsNone(classify('日本/东京'))
        self.assertEqual(classify('日本/东京', include_japan=True), '日本')
        self.assertIsNone(classify('未知', include_japan=True))
        records = helpers['build_region_records']([({'name': 'test'}, 'CoffeeCloud')],
            {'test': 20}, {}, 123, {'test': '日本/东京'}, {'test': True}, {'test': 2}, include_japan=True)
        self.assertEqual(records[0]['fields']['地区组'], '日本')
        self.assertTrue(records[0]['fields']['GPT可用'])

    def test_measured_region_dual_probe_streak_and_priority(self):
        names = ['美国假名称', 'US慢', 'SG快', 'US失败', 'US一轮']
        regions = dict(zip(names, ['未知', '美国', '新加坡', '美国', '美国']))
        passed = dict.fromkeys(names, True)
        passed['US失败'] = False
        streaks = dict.fromkeys(names, 2)
        streaks['US一轮'] = 1
        actual = module.eligible_candidates(names, regions, passed, streaks,
            {'US慢': 900, 'SG快': 100}, lambda x: x if x in ('美国', '新加坡') else None, 2)
        self.assertEqual(actual, ['US慢', 'SG快'])

    def test_no_eligible_means_no_direct_fallback(self):
        self.assertEqual(module.eligible_candidates(['美国'], {'美国': '未知'},
            {'美国': True}, {'美国': 20}, {'美国': 1}, lambda _: None, 2), [])
