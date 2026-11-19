"""Feishu Bitable contract shared with the A part (scripts/node_provider).

Two tables in one Bitable app:

  * 节点测速总表  key=nodes       - one row per collected node
  * 有效节点      key=valid_nodes - only nodes passing the validity gate

Field names, types and single-select options below are the *contract*.  The A
part (node_provider) writes the node identity/configuration columns of the
total table; this B part owns the measurement columns.  Do not rename.

Row ownership (S3 data-flow correction):

  * 节点测速总表 identity/config columns - 节点名, 来源, 源编号, 协议,
    节点配置JSON, 同步时间 - are written by node_provider --collect and must
    be preserved verbatim by node_ops.
  * 节点测速总表 measurement columns - 实际地区, 地区组, 延迟ms,
    chatgpt延迟ms, openai延迟ms, 存活, 测速目标, 地区检测时间 - are written by
    node_ops only.
  * 有效节点 is fully written by node_ops.

Node identity key is (来源, 节点名).

This module has no I/O; it is the single source of truth for the schema.
"""

from __future__ import annotations

from typing import Any

# Feishu Bitable field types.
FT_TEXT = 1
FT_NUMBER = 2
FT_SINGLE_SELECT = 3
FT_DATE_TIME = 5
FT_CHECKBOX = 7

DATE_PROPERTY: dict[str, Any] = {"date_formatter": "yyyy/MM/dd HH:mm", "auto_fill": False}
NUMBER_PROPERTY: dict[str, Any] = {"formatter": "0"}

SOURCE_OPTIONS = ("飞兔", "CoffeeCloud", "西瓜加速", "未知")
REGION_GROUP_OPTIONS = ("美国", "新加坡", "日本", "其他", "未知")

SOURCE_BY_INDEX = {1: "飞兔", 2: "CoffeeCloud", 3: "西瓜加速"}

NODES_TABLE_KEY = "nodes"
NODES_TABLE_NAME = "节点测速总表"
VALID_TABLE_KEY = "valid_nodes"
VALID_TABLE_NAME = "有效节点"

# Columns of 节点测速总表 owned by node_provider --collect.
NODES_IDENTITY_FIELDS = (
    "节点名", "来源", "源编号", "协议", "节点配置JSON", "同步时间",
)
# Columns of 节点测速总表 owned by node_ops.
NODES_MEASUREMENT_FIELDS = (
    "实际地区", "地区组", "延迟ms", "chatgpt延迟ms", "openai延迟ms",
    "存活", "测速目标", "地区检测时间",
)


def _source_field(name: str = "来源") -> tuple:
    return (name, FT_SINGLE_SELECT, {"options": [{"name": n} for n in SOURCE_OPTIONS]})


def _region_field(name: str = "地区组") -> tuple:
    return (name, FT_SINGLE_SELECT, {"options": [{"name": n} for n in REGION_GROUP_OPTIONS]})


NODES_TABLE: dict[str, Any] = {
    "key": NODES_TABLE_KEY,
    "name": NODES_TABLE_NAME,
    "fields": [
        ("节点名", FT_TEXT, None),
        _source_field(),
        ("源编号", FT_NUMBER, NUMBER_PROPERTY),
        ("协议", FT_TEXT, None),
        # Full mihomo proxy dict as JSON - the lossless carrier A part writes
        # and B part reads back to build probe configurations.
        ("节点配置JSON", FT_TEXT, None),
        ("实际地区", FT_TEXT, None),
        _region_field(),
        ("延迟ms", FT_NUMBER, NUMBER_PROPERTY),
        ("chatgpt延迟ms", FT_NUMBER, NUMBER_PROPERTY),
        ("openai延迟ms", FT_NUMBER, NUMBER_PROPERTY),
        ("存活", FT_CHECKBOX, None),
        ("测速目标", FT_TEXT, None),
        ("地区检测时间", FT_DATE_TIME, DATE_PROPERTY),
        ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
    ],
}

VALID_TABLE: dict[str, Any] = {
    "key": VALID_TABLE_KEY,
    "name": VALID_TABLE_NAME,
    "fields": [
        ("节点名", FT_TEXT, None),
        _source_field(),
        ("源编号", FT_NUMBER, NUMBER_PROPERTY),
        ("节点配置JSON", FT_TEXT, None),
        _region_field(),
        ("实际地区", FT_TEXT, None),
        ("延迟ms", FT_NUMBER, NUMBER_PROPERTY),
        ("chatgpt延迟ms", FT_NUMBER, NUMBER_PROPERTY),
        ("openai延迟ms", FT_NUMBER, NUMBER_PROPERTY),
        ("GPT可用", FT_CHECKBOX, None),
        ("连续通过轮数", FT_NUMBER, NUMBER_PROPERTY),
        ("建议代理组", FT_TEXT, None),
        ("生效时间", FT_DATE_TIME, DATE_PROPERTY),
        ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
    ],
}

TABLE_SPECS: tuple[dict[str, Any], ...] = (NODES_TABLE, VALID_TABLE)


def table_by_key(key: str) -> dict[str, Any]:
    for spec in TABLE_SPECS:
        if spec["key"] == key:
            return spec
    raise KeyError(f"unknown table key: {key}")


def field_names(key: str) -> tuple[str, ...]:
    return tuple(field[0] for field in table_by_key(key)["fields"])


def contract_summary() -> dict[str, list[dict[str, Any]]]:
    """Machine-readable schema, used by tests and by the A part."""
    result: dict[str, list[dict[str, Any]]] = {}
    for spec in TABLE_SPECS:
        result[spec["key"]] = [
            {
                "name": name,
                "type": ftype,
                "property": prop,
            }
            for name, ftype, prop in spec["fields"]
        ]
    return result
