"""Tests for source-4 (西瓜加速) collection + the short-id YAML quoting fix.

Covered contracts:
  * SOURCE_NAMES / TABLE_SPECS carry 西瓜加速, and a 4-result run keeps the
    表1 rows for sources 1-3 untouched (no drop, same order, same fields)
  * main() collects (1, 2, 3, 4) and test_isolated dumps its temp config
    proxies through _quote_numeric_like (structural guards on the source)
  * numeric-looking strings such as reality-opts short-id "12e45678" are
    force-quoted in the YAML dump and re-parse as str, while real ints stay
    ints
  * META_INFO_PATTERNS filters 防丢失 / 网不通 / 招募 rows, and collect_source
    with an empty URL returns url_missing without raising (source 4 has no
    subscription URL by design; the round must survive it)

No network, no production files: caches are fake paths in tmp dirs.
"""

import importlib.util
import inspect
import os
import sys
import tempfile
import unittest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "sync-feishu-bitable.py")

spec = importlib.util.spec_from_file_location("sync_feishu_bitable_src4", SCRIPT)
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


def make_result(index, nodes, result="成功", error=""):
    r = sync.SourceResult(index, sync.SOURCE_NAMES[index])
    r.nodes = nodes
    r.result = result
    r.error = error
    return r


class SourceFourRegistrationTests(unittest.TestCase):
    def test_source_names_include_watermelon(self):
        self.assertEqual(sync.SOURCE_NAMES.get(4), "西瓜加速")

    def test_table_specs_source_options_include_watermelon(self):
        for key, field in (("nodes", "来源"), ("gpt_regions", "来源")):
            spec = next(s for s in sync.TABLE_SPECS if s["key"] == key)
            options = next(
                prop["options"] for name, _t, prop in spec["fields"]
                if name == field
            )
            names = [o["name"] for o in options]
            self.assertIn("西瓜加速", names, f"{key}.{field}")
            for old in ("飞兔", "星辰", "CoffeeCloud"):
                self.assertIn(old, names, f"{key}.{field}")

    def test_main_loop_collects_four_sources(self):
        self.assertIn("for index in (1, 2, 3, 4):", inspect.getsource(sync.main))

    def test_isolated_quotes_numeric_like_proxies(self):
        src = inspect.getsource(sync.test_isolated)
        self.assertIn("_quote_numeric_like(proxies)", src)


class SourceFourRecordBuildingTests(unittest.TestCase):
    def test_source4_does_not_drop_source123_rows(self):
        results = [
            make_result(1, [{"name": "a1", "type": "ss", "server": "s1", "port": 1}]),
            make_result(2, [{"name": "b1", "type": "ss", "server": "s2", "port": 2}]),
            make_result(3, [{"name": "c1", "type": "ss", "server": "s3", "port": 3}]),
            make_result(4, [{"name": "d1", "type": "vless", "server": "s4", "port": 443}],
                        result="缓存", error="url_missing; using cache"),
        ]
        records = sync.build_source_records(results, sync_ms=1)
        self.assertEqual(len(records), 4)
        self.assertEqual(
            [r["fields"]["源名称"] for r in records],
            ["飞兔", "星辰", "CoffeeCloud", "西瓜加速"],
        )
        for index in (1, 2, 3):
            row = records[index - 1]["fields"]
            self.assertEqual(row["源编号"], index)
            self.assertEqual(row["拉取结果"], "成功")
        row4 = records[3]["fields"]
        self.assertEqual(row4["拉取结果"], "缓存")
        self.assertEqual(row4["节点数"], 1)

    def test_pool_keeps_source123_nodes_with_source4(self):
        results = [
            make_result(1, [{"name": "SG-1", "type": "ss", "server": "s1", "port": 1,
                             "password": "p"}]),
            make_result(2, [{"name": "US-2", "type": "ss", "server": "s2", "port": 2,
                             "password": "p"}]),
            make_result(3, [{"name": "JP-3", "type": "ss", "server": "s3", "port": 3,
                             "password": "p"}]),
            make_result(4, [{"name": "HK-4", "type": "vless", "server": "s4",
                             "port": 443, "uuid": "u"}], result="缓存"),
        ]
        pool = sync.build_pool(results, log=RecordingLog())
        sources = {source for _node, source in pool}
        self.assertEqual(sources, {"飞兔", "星辰", "CoffeeCloud", "西瓜加速"})
        names = {node["name"] for node, _source in pool}
        self.assertTrue({"SG-1", "US-2", "JP-3", "HK-4"} <= names)


class ShortIdQuotingTests(unittest.TestCase):
    def test_numeric_like_strings_are_dumped_quoted(self):
        proxies = [{
            "name": "n1", "type": "vless", "server": "example.invalid", "port": 443,
            "uuid": "12e45678",
            "reality-opts": {"public-key": "key", "short-id": "12e45678"},
            "password": "0123",
        }]
        dumped = yaml.safe_dump(
            {"proxies": sync._quote_numeric_like(proxies)},
            allow_unicode=True, sort_keys=False,
        )
        self.assertIn('short-id: "12e45678"', dumped)
        reloaded = yaml.safe_load(dumped)["proxies"][0]
        self.assertEqual(reloaded["reality-opts"]["short-id"], "12e45678")
        self.assertIsInstance(reloaded["reality-opts"]["short-id"], str)
        self.assertEqual(reloaded["uuid"], "12e45678")
        self.assertEqual(reloaded["password"], "0123")

    def test_real_ints_and_plain_strings_untouched(self):
        proxies = [{"name": "日本-优化", "port": 443, "udp": True}]
        converted = sync._quote_numeric_like(proxies)
        self.assertEqual(converted[0]["name"], "日本-优化")
        self.assertNotIsInstance(converted[0]["name"], sync._QuotedString)
        self.assertIsInstance(converted[0]["port"], int)
        self.assertIsInstance(converted[0]["udp"], bool)

    def test_ip_address_strings_not_quoted(self):
        # "1.2.3.4" is a str for every YAML consumer; no forced quotes needed.
        converted = sync._quote_numeric_like({"server": "1.2.3.4"})
        self.assertNotIsInstance(converted["server"], sync._QuotedString)


class MetaFilterTests(unittest.TestCase):
    def test_new_meta_patterns(self):
        for name in (
            "★防丢失网站(xiguajiasu6.com)",
            "★!!!网不通重新更新订阅",
            "🈴招募合作推广🈴请咨询官网客服",
            "★V-防丢失网站(xiguajiasu6.com)",
        ):
            self.assertTrue(sync.is_meta_info_name(name), name)

    def test_real_node_names_survive(self):
        for name in ("🇭🇰 香港-双倍", "美国- LosAngeles 0.5倍", "V-日本-东京-IEPL"):
            self.assertFalse(sync.is_meta_info_name(name), name)


class UrlMissingTests(unittest.TestCase):
    def test_collect_source_without_url_returns_url_missing(self):
        result = sync.collect_source(4, "", "ua", 1, None, RecordingLog())
        self.assertEqual(result.result, "失败")
        self.assertEqual(result.error, "url_missing")
        self.assertEqual(result.nodes, [])

    def test_url_missing_cache_fallback_marks_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "source4-cache.yaml")
            with open(cache, "w", encoding="utf-8") as handle:
                handle.write(yaml.safe_dump({"proxies": [
                    {"name": "HK-4", "type": "vless", "server": "s4",
                     "port": 443, "uuid": "u"},
                ]}, allow_unicode=True, sort_keys=False))
            result = sync.collect_source(4, "", "ua", 1, None, RecordingLog())
            applied = sync.apply_source_cache(result, [cache], RecordingLog())
            self.assertTrue(applied)
            self.assertEqual(result.result, "缓存")
            self.assertEqual(len(result.nodes), 1)

    def test_missing_cache_file_keeps_url_missing_without_raising(self):
        result = sync.collect_source(4, "", "ua", 1, None, RecordingLog())
        applied = sync.apply_source_cache(
            result, [r"C:\nonexistent\source4-cache.yaml"], RecordingLog())
        self.assertFalse(applied)
        self.assertEqual(result.result, "失败")
        self.assertEqual(result.error, "url_missing")


if __name__ == "__main__":
    unittest.main()
