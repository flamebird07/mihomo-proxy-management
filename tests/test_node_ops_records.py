"""node_ops: lossless node-config JSON, record building, row ownership."""

import json
import os
import sys
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from node_ops import records, schema  # noqa: E402
from node_ops.gate import evaluate_nodes  # noqa: E402
from node_ops.model import NodeEntry  # noqa: E402
from node_ops.probe import ProbeResult  # noqa: E402

FULL_NODE = {
    "name": "us-reality-01",
    "type": "vless",
    "server": "2001:db8::1",
    "port": 443,
    "uuid": "11111111-2222-3333-4444-555555555555",
    "network": "grpc",
    "tls": True,
    "servername": "cdn.example.com",
    "client-fingerprint": "chrome",
    "alpn": ["h2", "http/1.1"],
    "flow": "xtls-rprx-vision",
    "skip-cert-verify": False,
    "reality-opts": {"public-key": "PUBKEY", "short-id": "12e45678"},
    "grpc-opts": {"grpc-service-name": "svc"},
    "ws-opts": {"path": "/ws", "headers": {"Host": "cdn.example.com"}},
    "plugin": "obfs",
    "plugin_opts": "obfs-local;obfs=http",
    "udp": True,
}


def entry(name="us-reality-01", source="飞兔", config=None, probe_name=None,
          source_number=1):
    return NodeEntry(name=name, source=source, config=config or dict(FULL_NODE),
                     probe_name=probe_name or name, source_number=source_number)


def previous_row(**overrides):
    fields = {
        "节点名": FULL_NODE["name"],
        "来源": "飞兔",
        "源编号": 1,
        "协议": "vless",
        "节点配置JSON": records.config_to_json(FULL_NODE),
        "实际地区": "旧地区",
        "地区组": "未知",
        "延迟ms": 999,
        "chatgpt延迟ms": 999,
        "openai延迟ms": 999,
        "存活": True,
        "测速目标": "old",
        "地区检测时间": 1,
        "同步时间": 1_500_000_000_000,
    }
    fields.update(overrides)
    return {"record_id": "rec1", "fields": fields}


class ConfigJsonRoundTripTests(unittest.TestCase):
    def test_json_round_trip_is_identical(self):
        text = records.config_to_json(FULL_NODE)
        loaded = records.config_from_json(text)
        self.assertEqual(loaded, FULL_NODE)

    def test_yaml_round_trip_keeps_every_field(self):
        loaded = records.config_from_json(records.config_to_json(FULL_NODE))
        re_dumped = yaml.safe_load(yaml.safe_dump(loaded, allow_unicode=True, sort_keys=False))
        self.assertEqual(re_dumped, FULL_NODE)
        for key in (
            "name", "type", "server", "port", "uuid", "network", "tls", "servername",
            "client-fingerprint", "alpn", "flow", "skip-cert-verify", "reality-opts",
            "grpc-opts", "ws-opts", "plugin", "plugin_opts", "udp",
        ):
            self.assertIn(key, re_dumped)

    def test_non_dict_json_rejected(self):
        with self.assertRaises(ValueError):
            records.config_from_json("[1, 2, 3]")


class RecordBuildingTests(unittest.TestCase):
    def setUp(self):
        self.entry = entry()
        self.result = ProbeResult(
            delays={self.entry.probe_name: 120},
            chatgpt={self.entry.probe_name: 800},
            openai={self.entry.probe_name: None},
            regions={self.entry.probe_name: "美国/加利福尼亚/圣何塞"},
        )

    def test_node_record_fields_match_contract(self):
        rows = records.build_node_records([self.entry], self.result, 1_700_000_000_000,
                                          1_700_000_000_000)
        fields = rows[0]["fields"]
        self.assertEqual(set(fields), set(schema.field_names("nodes")))
        self.assertEqual(fields["来源"], "飞兔")
        self.assertEqual(fields["源编号"], 1)
        self.assertEqual(fields["地区组"], "美国")
        self.assertEqual(fields["延迟ms"], 120)
        self.assertEqual(fields["openai延迟ms"], -1)
        self.assertTrue(fields["存活"])
        self.assertEqual(records.config_from_json(fields["节点配置JSON"]), FULL_NODE)

    def test_identity_columns_copied_verbatim_and_measurement_replaced(self):
        old = previous_row()
        rows = records.build_node_records([self.entry], self.result, 1_700_000_000_000,
                                          1_700_000_000_000, previous_rows=[old])
        fields = rows[0]["fields"]
        # Write-discipline ruling: 同步时间 is the measuring side's
        # last-valid-rate clock, NOT a verbatim identity column - a valid
        # rate stamps this round; every other identity field stays verbatim.
        for key in schema.NODES_IDENTITY_FIELDS:
            if key == "同步时间":
                continue
            self.assertEqual(fields[key], old["fields"][key], key)
        self.assertEqual(fields["同步时间"], 1_700_000_000_000,
                         "valid rate advances the tombstone clock")
        self.assertEqual(fields["延迟ms"], 120)
        self.assertEqual(fields["chatgpt延迟ms"], 800)
        self.assertEqual(fields["openai延迟ms"], -1)
        self.assertEqual(fields["实际地区"], "美国/加利福尼亚/圣何塞")

    def test_string_source_number_readback_normalized_to_int(self):
        old = previous_row()
        old["fields"]["源编号"] = "1"
        rows = records.build_node_records([self.entry], self.result, 1_700_000_000_000,
                                          1_700_000_000_000, previous_rows=[old])
        self.assertEqual(rows[0]["fields"]["源编号"], 1)
        self.assertIsInstance(rows[0]["fields"]["源编号"], int)

    def test_unparseable_source_number_readback_dropped(self):
        old = previous_row()
        old["fields"]["源编号"] = "not-a-number"
        rows = records.build_node_records([self.entry], self.result, 1_700_000_000_000,
                                          1_700_000_000_000, previous_rows=[old])
        self.assertNotIn("源编号", rows[0]["fields"])

    def test_identity_index_skips_unkeyed_rows(self):
        index = records.identity_index([
            previous_row(),
            {"fields": {"节点名": "no-source"}},
            {"fields": "not-a-dict"},
        ])
        self.assertEqual(list(index), [("飞兔", FULL_NODE["name"])])

    def test_valid_record_only_for_gpt_available(self):
        result = ProbeResult(
            delays={self.entry.probe_name: 120},
            chatgpt={self.entry.probe_name: 800},
            openai={self.entry.probe_name: 900},
            regions={self.entry.probe_name: "美国/加利福尼亚/圣何塞"},
        )
        evaluations = evaluate_nodes([self.entry], result, {("飞兔", self.entry.name): 3})
        rows = records.build_valid_records(evaluations, 1_700_000_000_000, 1_700_000_000_000)
        self.assertEqual(len(rows), 1)
        fields = rows[0]["fields"]
        self.assertEqual(set(fields), set(schema.field_names("valid_nodes")))
        self.assertTrue(fields["GPT可用"])
        self.assertEqual(fields["源编号"], 1)
        self.assertEqual(fields["连续通过轮数"], 4)
        self.assertEqual(fields["建议代理组"], "GPT/US-FAST/MUSE")
        self.assertEqual(records.config_from_json(fields["节点配置JSON"]), FULL_NODE)

    def test_failed_node_has_no_valid_row(self):
        result = ProbeResult(delays={"x": 1}, chatgpt={"x": None}, openai={"x": 1})
        evaluations = evaluate_nodes([entry("x")], result, {})
        self.assertEqual(records.build_valid_records(evaluations, 1, 1), [])

    def test_source_number_defaults_to_zero_when_absent(self):
        row = {"fields": {"节点名": "x", "来源": "飞兔", "节点配置JSON": "{}"}}
        self.assertEqual(records.source_number_from_row(row["fields"]), 0)


if __name__ == "__main__":
    unittest.main()
