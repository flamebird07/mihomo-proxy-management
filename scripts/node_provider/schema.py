"""Feishu Bitable contract for node_provider (read + collect write side).

This is a self-contained copy of the contract defined by the B part
(``scripts/node_ops/schema.py``).  Runtime code must not import node_ops; the
contract is duplicated here on purpose and asserted by
``tests/test_node_provider_contract.py``.

Field names, types and single-select options must match node_ops exactly.
Do not rename.

Row ownership (S3 data-flow correction):

  * 「节点测速总表」 identity/config columns - 节点名, 来源, 源编号, 协议,
    节点配置JSON, 同步时间 - are written by node_provider --collect.  The
    measurement columns are written by node_ops and are reset to defaults by
    --collect.
  * 「有效节点」 is read here (default provide run) and written by node_ops.
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

# Subscription env slot -> source number.  The env file keeps its original
# slots: slot 1=飞兔, slot 2=星辰, slot 3=CoffeeCloud.  Per the 2026-11-19 user
# decision the source numbering is renumbered (星辰 removed, CoffeeCloud=2,
# 西瓜加速=3), but the env slots never change.  Slot 2 (星辰) is therefore
# permanently excluded: that subscription is not subscribable.
SUBSCRIPTION_SOURCE_BY_ENV_SLOT = {1: 1, 3: 2}

NODES_TABLE_KEY = "nodes"
NODES_TABLE_NAME = "节点测速总表"
VALID_TABLE_KEY = "valid_nodes"
VALID_TABLE_NAME = "有效节点"

NODES_IDENTITY_FIELDS = (
    "节点名", "来源", "源编号", "协议", "节点配置JSON", "同步时间",
)
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

# Cross-machine daily-collect bookkeeping.  This table is owned exclusively by
# node_provider --collect and is deliberately NOT part of TABLE_SPECS: the
# contract with node_ops covers only the two tables above.
COLLECT_STATE_KEY = "collect_state"
COLLECT_STATE_TABLE_NAME = "采集状态"
COLLECT_CHANNEL_SUBSCRIPTION = "subscription"
COLLECT_CHANNEL_APP = "app"
COLLECT_CHANNEL_OPTIONS = (COLLECT_CHANNEL_SUBSCRIPTION, COLLECT_CHANNEL_APP)

COLLECT_STATE_TABLE: dict[str, Any] = {
    "key": COLLECT_STATE_KEY,
    "name": COLLECT_STATE_TABLE_NAME,
    "fields": [
        ("源编号", FT_NUMBER, NUMBER_PROPERTY),
        _source_field(),
        ("通道", FT_TEXT, None),
        ("读取机器", FT_TEXT, None),
        ("最近读取时间", FT_DATE_TIME, DATE_PROPERTY),
        ("节点数", FT_NUMBER, NUMBER_PROPERTY),
    ],
}

# (field name, type) in contract order - asserted against node_ops.
VALID_FIELDS = tuple((name, ftype) for name, ftype, _prop in VALID_TABLE["fields"])
VALID_FIELD_NAMES = tuple(name for name, _ in VALID_FIELDS)
NODES_FIELDS = tuple((name, ftype) for name, ftype, _prop in NODES_TABLE["fields"])

# Every reconstructed proxy object must carry these keys.
REQUIRED_PROXY_KEYS = ("name", "type", "server", "port")

REGION_FILTER_CHOICES = ("美国", "新加坡", "日本")


def table_by_key(key: str) -> dict[str, Any]:
    for spec in TABLE_SPECS:
        if spec["key"] == key:
            return spec
    raise KeyError(f"unknown table key: {key}")


def field_names(key: str) -> tuple[str, ...]:
    return tuple(field[0] for field in table_by_key(key)["fields"])
