"""Validity gate and consecutive-pass streak handling.

The streak lives in the Feishu 「有效节点」 table - NOT in a local history file.
Before writing, the old row for (来源, 节点名) is read; a pass increments the
streak, a failure resets it to 0.  Nodes that disappear this round lose their
row automatically because the module replaces all of its rows.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .model import NodeEntry, NodeEvaluation
from .probe import ProbeResult
from .regions import GROUP_UNKNOWN, classify_region_group

GPT_REQUIRED_CONSECUTIVE = 2


def field_text(value: Any) -> str:
    """Read a text-ish Bitable field value into a plain string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                parts.append(str(text) if text is not None else "")
            elif isinstance(item, (str, int, float)):
                parts.append(str(item))
        return "".join(parts)
    if isinstance(value, dict):
        text = value.get("text")
        return str(text) if text is not None else ""
    return str(value)


def _number_from_text(text: str) -> Optional[int]:
    try:
        return int(float(text.strip()))
    except (TypeError, ValueError):
        return None


def field_number(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        # Feishu reads number fields back as strings.
        return _number_from_text(value)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                return int(item)
            if isinstance(item, str):
                parsed = _number_from_text(item)
                if parsed is not None:
                    return parsed
            if isinstance(item, dict) and isinstance(item.get("value"), (int, float)):
                return int(item["value"])
    return None


def previous_streaks(rows: Iterable[Dict[str, Any]]) -> Dict[tuple[str, str], int]:
    """Map (来源, 节点名) -> 连续通过轮数 from 「有效节点」 rows."""
    streaks: Dict[tuple[str, str], int] = {}
    for row in rows:
        fields = row.get("fields") if isinstance(row, dict) else None
        if not isinstance(fields, dict):
            continue
        source = field_text(fields.get("来源")).strip()
        name = field_text(fields.get("节点名")).strip()
        if not source or not name:
            continue
        value = field_number(fields.get("连续通过轮数"))
        if value is None:
            continue
        streaks[(source, name)] = max(0, value)
    return streaks


def previous_passes(rows: Iterable[Dict[str, Any]]) -> Dict[tuple[str, str], bool]:
    """Map (来源, 节点名) -> last round's chatgpt+openai pass, read from table one.

    「有效节点」 only stores nodes whose streak is already >= 2, so a node that
    passed exactly once last round has no row there.  Table one keeps last
    round's chatgpt/openai delays for every node, which is enough to know that
    such a node's previous streak was exactly 1 (otherwise it would have had a
    「有效节点」 row) and therefore its current streak is 2.
    """
    passed: Dict[tuple[str, str], bool] = {}
    for row in rows:
        fields = row.get("fields") if isinstance(row, dict) else None
        if not isinstance(fields, dict):
            continue
        source = field_text(fields.get("来源")).strip()
        name = field_text(fields.get("节点名")).strip()
        if not source or not name:
            continue
        chatgpt = field_number(fields.get("chatgpt延迟ms")) or 0
        openai = field_number(fields.get("openai延迟ms")) or 0
        passed[(source, name)] = chatgpt > 0 and openai > 0
    return passed


def evaluate_nodes(entries: List[NodeEntry], result: ProbeResult,
                   previous: Optional[Dict[tuple[str, str], int]] = None,
                   previous_passed: Optional[Dict[tuple[str, str], bool]] = None
                   ) -> List[NodeEvaluation]:
    previous = previous or {}
    previous_passed = previous_passed or {}
    evaluations: List[NodeEvaluation] = []
    for entry in entries:
        delay = result.delays.get(entry.probe_name)
        chatgpt = result.chatgpt.get(entry.probe_name)
        openai = result.openai.get(entry.probe_name)
        passed = bool(chatgpt) and bool(openai)
        if not passed:
            streak = 0
        elif entry.identity in previous:
            streak = max(0, int(previous[entry.identity] or 0)) + 1
        elif previous_passed.get(entry.identity):
            # Not in 「有效节点」 last round + passed last round => streak was 1.
            streak = 2
        else:
            streak = 1
        area = (result.regions.get(entry.probe_name) or "").strip() or GROUP_UNKNOWN
        group = classify_region_group(entry.name, area)
        evaluations.append(NodeEvaluation(
            entry=entry,
            delay_ms=delay,
            chatgpt_ms=chatgpt,
            openai_ms=openai,
            actual_area=area,
            region_group=group,
            passed=passed,
            streak=streak,
        ))
    return evaluations


def valid_evaluations(evaluations: Iterable[NodeEvaluation]) -> List[NodeEvaluation]:
    return [item for item in evaluations if item.gpt_available]
