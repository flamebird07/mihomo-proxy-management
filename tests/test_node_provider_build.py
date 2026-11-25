"""node_provider: lossless rebuild, filters, dedupe, freshness, writer."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import yaml  # noqa: E402

from node_provider import builder, writer  # noqa: E402

NOW_MS = 1_700_000_000_000
MINUTE = 60_000


def cfg(name="us-01", server="1.1.1.1", port=443, ptype="trojan", **extra):
    base = {"name": name, "type": ptype, "server": server, "port": port,
            "password": "pw"}
    base.update(extra)
    return base


def row(config=None, *, gpt=True, region="美国", sync_ms=NOW_MS,
        config_json=None, fields=None):
    if fields is not None:
        return {"fields": fields}
    if config_json is None:
        config_json = json.dumps(config, ensure_ascii=False)
    return {"fields": {
        "节点名": (config or {}).get("name", ""),
        "来源": "飞兔",
        "节点配置JSON": config_json,
        "地区组": region,
        "GPT可用": gpt,
        "同步时间": sync_ms,
    }}


class RebuildTests(unittest.TestCase):
    def test_advertisement_rows_are_not_valid_nodes(self):
        ad = cfg(name="★!!!网不通重新更新订阅")
        real = cfg(name="us-01")
        result = builder.build_proxies([row(ad), row(real)], now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in result.proxies], ["us-01"])
        self.assertEqual(result.ad_skipped, 1)

    def test_lossless_nested_fields(self):
        nested = cfg(
            name="reality-01", ptype="vless", server="edge.example.com",
            uuid="11111111-2222-3333-4444-555555555555", tls=True,
            flow="xtls-rprx-vision", servername="www.example.com",
            client_fingerprint="chrome",
            **{
                "reality-opts": {"public-key": "PUBKEY", "short-id": "abcd"},
                "ws-opts": {"path": "/ws", "headers": {"Host": "cdn.example.com"}},
                "alpn": ["h2", "http/1.1"],
                "skip-cert-verify": False,
            },
        )
        result = builder.build_proxies([row(nested)], now_ms=NOW_MS)
        self.assertEqual(result.proxies, [nested])
        self.assertEqual(result.invalid, 0)

        document = yaml.safe_load(writer.render_provider(result.proxies))
        self.assertEqual(document, {"proxies": [nested]})

    def test_text_field_as_bitable_runs(self):
        item = cfg(name="run-01")
        runs = [{"text": json.dumps(item, ensure_ascii=False), "type": "text"}]
        result = builder.build_proxies([row(config_json=runs)], now_ms=NOW_MS)
        self.assertEqual(result.proxies, [item])

    def test_gpt_filter_strict_and_all_valid(self):
        rows = [row(cfg(name="a")), row(cfg(name="b", server="2.2.2.2"), gpt=False)]
        strict = builder.build_proxies(rows, now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in strict.proxies], ["a"])
        self.assertEqual(strict.gpt_skipped, 1)

        relaxed = builder.build_proxies(rows, all_valid=True, now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in relaxed.proxies], ["a", "b"])
        self.assertEqual(relaxed.gpt_skipped, 0)

    def test_region_filter(self):
        rows = [row(cfg(name="us", server="1.1.1.1"), region="美国"),
                row(cfg(name="jp", server="2.2.2.2"), region="日本"),
                row(cfg(name="other", server="3.3.3.3"), region="其他")]
        result = builder.build_proxies(rows, regions=["美国"], now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in result.proxies], ["us"])
        self.assertEqual(result.region_skipped, 2)

        multi = builder.build_proxies(rows, regions=["美国", "日本"], now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in multi.proxies], ["us", "jp"])

    def test_dedupe_identical_config(self):
        rows = [row(cfg(name="one")), row(cfg(name="two"))]
        result = builder.build_proxies(rows, now_ms=NOW_MS)
        self.assertEqual(len(result.proxies), 1)
        self.assertEqual(result.proxies[0]["name"], "one")
        self.assertEqual(result.duplicates, 1)

    def test_same_name_suffix_and_reserved_names(self):
        rows = [
            row(cfg(name="dup", server="1.1.1.1")),
            row(cfg(name="dup", server="2.2.2.2")),
        ]
        result = builder.build_proxies(rows, now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in result.proxies], ["dup", "dup [2]"])
        self.assertEqual(result.proxies[1]["server"], "2.2.2.2")

        rows = [
            row(cfg(name="x", server="1.1.1.1")),
            row(cfg(name="x [2]", server="2.2.2.2")),
            row(cfg(name="x", server="3.3.3.3")),
        ]
        result = builder.build_proxies(rows, now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in result.proxies],
                         ["x", "x [2]", "x [3]"])

    def test_invalid_rows_skipped_and_counted(self):
        bad_json = row(config_json="{not json")
        not_object = row(config_json="[]")
        no_port = cfg(name="noport")
        no_port.pop("port")
        missing_server = cfg(name="noserver")
        missing_server.pop("server")
        bad_name = cfg(name=123)
        no_fields = {"record_id": "rec1"}
        good = row(cfg(name="good"))
        result = builder.build_proxies(
            [bad_json, not_object, row(no_port), row(missing_server),
             row(bad_name), no_fields, good],
            now_ms=NOW_MS,
        )
        self.assertEqual(result.invalid, 6)
        self.assertEqual([p["name"] for p in result.proxies], ["good"])

    def test_freshness_guard(self):
        fresh = row(cfg(name="fresh", server="1.1.1.1"),
                    sync_ms=NOW_MS - 10 * MINUTE)
        old = row(cfg(name="old", server="2.2.2.2"),
                  sync_ms=NOW_MS - 1441 * MINUTE)
        missing = {"fields": {
            "节点配置JSON": json.dumps(cfg(name="missing", server="3.3.3.3")),
            "GPT可用": True}}

        strict = builder.build_proxies([fresh, old, missing], now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in strict.proxies], ["fresh"])
        self.assertEqual(strict.stale_skipped, 2)

        relaxed = builder.build_proxies([fresh, old, missing], allow_stale=True,
                                        now_ms=NOW_MS)
        self.assertEqual([p["name"] for p in relaxed.proxies],
                         ["fresh", "old", "missing"])
        self.assertEqual(relaxed.stale_skipped, 0)

    def test_custom_max_age(self):
        old = row(cfg(name="old"), sync_ms=NOW_MS - 61 * MINUTE)
        result = builder.build_proxies([old], max_age_minutes=60, now_ms=NOW_MS)
        self.assertEqual(result.selected, 0)
        self.assertEqual(result.stale_skipped, 1)

    def test_no_mutation_of_source_config(self):
        item = cfg(name="keep")
        rows = [row(item)]
        builder.build_proxies(rows, all_valid=True, now_ms=NOW_MS)
        self.assertEqual(item["name"], "keep")


class WriterTests(unittest.TestCase):
    def test_render_round_trip(self):
        proxies = [cfg(name="a"), cfg(name="b", ptype="ss", **{"plugin": "obfs"}),
                   cfg(name="美国-01", server="4.4.4.4")]
        text = writer.render_provider(proxies)
        self.assertEqual(yaml.safe_load(text), {"proxies": proxies})
        self.assertIn("proxies:", text)
        self.assertIn("美国-01", text)  # allow_unicode: not \uXXXX escaped

    def test_atomic_replace_and_no_temp_left(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "provider.yaml")
            writer.atomic_write(path, [cfg(name="v1")])
            with open(path, encoding="utf-8") as handle:
                first = yaml.safe_load(handle)
            self.assertEqual(first["proxies"][0]["name"], "v1")

            writer.atomic_write(path, [cfg(name="v2"), cfg(name="v3")])
            with open(path, encoding="utf-8") as handle:
                second = yaml.safe_load(handle)
            self.assertEqual([p["name"] for p in second["proxies"]], ["v2", "v3"])
            self.assertEqual(sorted(os.listdir(root)), ["provider.yaml"])

    def test_missing_directory_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "missing", "provider.yaml")
            with self.assertRaises(writer.OutputError):
                writer.atomic_write(path, [cfg()])
            self.assertEqual(os.listdir(root), [])


if __name__ == "__main__":
    unittest.main()
