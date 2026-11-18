"""Build Feishu record payloads from pipeline results.

The 「节点配置JSON」 field is the lossless carrier of a node: the complete
mihomo proxy dict serialized with ``json.dumps(..., ensure_ascii=False)``.

Row ownership: node_ops must not invent node identity.  ``build_node_records``
takes the rows read from 「节点测速总表」 and copies the identity/config
columns (节点名/来源/源编号/协议/节点配置JSON/同步时间) verbatim, replacing
only the measurement columns it owns.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from . import schema
from .gate import field_number, field_text
from .model import NodeEntry, NodeEvaluation
from .probe import ProbeResult
from .regions import advise_groups, classify_region_group

SPEEDTEST_TARGET_LABEL = "grok/chatgpt/openai"


def config_to_json(config: Dict[str, Any]) -> str:
    if not isinstance(config, dict):
        raise ValueError("node config must be a dict")
    return json.dumps(config, ensure_ascii=False, allow_nan=False)


def config_from_json(text: Any) -> Dict[str, Any]:
    raw = field_text(text)
    if not raw:
        raise ValueError("empty node config JSON")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("node config JSON must decode to an object")
    return data


def compact(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in fields.items()
            if value is not None and value != ""}


def identity_index(rows: Iterable[Dict[str, Any]]) -> Dict[tuple[str, str], Dict[str, Any]]:
    """Map (来源, 节点名) -> the identity/config columns of a total-table row."""
    index: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        fields = row.get("fields") if isinstance(row, dict) else None
        if not isinstance(fields, dict):
            continue
        source = field_text(fields.get("来源")).strip()
        name = field_text(fields.get("节点名")).strip()
        if not source or not name:
            continue
        index[(source, name)] = {
            key: fields[key] for key in schema.NODES_IDENTITY_FIELDS if key in fields
        }
    return index


def _identity_fields(entry: NodeEntry,
                     previous: Optional[Dict[str, Dict[str, Any]]],
                     sync_ms: int) -> Dict[str, Any]:
    preserved = (previous or {}).get(entry.identity)
    if preserved is not None:
        fields = dict(preserved)
        # Feishu reads number fields back as strings; writing them verbatim
        # into the number-typed 源编号 column fails with api code 1254061.
        number = field_number(fields.get("源编号"))
        if number is not None:
            fields["源编号"] = number
        else:
            fields.pop("源编号", None)
        return fields
    return compact({
        "节点名": entry.name,
        "来源": entry.source or "未知",
        "源编号": entry.source_number or None,
        "协议": str(entry.config.get("type") or ""),
        "节点配置JSON": config_to_json(entry.config),
        "同步时间": sync_ms,
    })


def build_node_records(entries: Iterable[NodeEntry], result: ProbeResult,
                       sync_ms: int, region_ms: int,
                       previous_rows: Optional[Iterable[Dict[str, Any]]] = None,
                       target_label: str = SPEEDTEST_TARGET_LABEL) -> List[Dict[str, Any]]:
    """Merge owned measurement columns onto preserved identity columns."""
    previous = identity_index(previous_rows or [])
    records: List[Dict[str, Any]] = []
    for entry in entries:
        name = entry.probe_name
        delay = result.delays.get(name)
        area = (result.regions.get(name) or "").strip() or "未知"
        fields = _identity_fields(entry, previous, sync_ms)
        fields.update({
            "实际地区": area,
            "地区组": classify_region_group(entry.name, area),
            "延迟ms": delay if delay else -1,
            "chatgpt延迟ms": result.chatgpt.get(name) or -1,
            "openai延迟ms": result.openai.get(name) or -1,
            "存活": bool(delay),
            "测速目标": target_label,
            "地区检测时间": region_ms,
        })
        records.append({"fields": fields})
    return records


def build_valid_records(evaluations: Iterable[NodeEvaluation], sync_ms: int,
                        effective_ms: int) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for item in evaluations:
        if not item.gpt_available:
            continue
        fields = compact({
            "节点名": item.entry.name,
            "来源": item.entry.source or "未知",
            "源编号": item.entry.source_number or None,
            "节点配置JSON": config_to_json(item.entry.config),
            "地区组": item.region_group,
            "实际地区": item.actual_area,
            "延迟ms": item.delay_ms if item.delay_ms else -1,
            "chatgpt延迟ms": item.chatgpt_ms if item.chatgpt_ms else -1,
            "openai延迟ms": item.openai_ms if item.openai_ms else -1,
            "GPT可用": True,
            "连续通过轮数": item.streak,
            "建议代理组": advise_groups(item.region_group),
            "生效时间": effective_ms,
            "同步时间": sync_ms,
        })
        records.append({"fields": fields})
    return records


def source_number_from_row(fields: Dict[str, Any]) -> int:
    """Read 源编号; missing/invalid becomes 0 (preserved as-is by writers)."""
    value = field_number(fields.get("源编号"))
    return value if value is not None else 0
