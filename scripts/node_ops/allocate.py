"""Allocation *advice* only - never applied to any controller.

Existing project semantics:
  * GPT and US-FAST candidate order is US > SG > JP.
  * MUSE is US-only.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from .model import NodeEvaluation
from .regions import GROUP_JP, GROUP_SG, GROUP_US, advise_groups, preferred_order, tier_of

__all__ = [
    "advise_groups",
    "preferred_order",
    "pick_best",
    "rank_candidates",
]

GROUP_GPT = "GPT"
GROUP_US_FAST = "US-FAST"
GROUP_MUSE = "MUSE"
SWITCH_GROUPS = (GROUP_GPT, GROUP_US_FAST, GROUP_MUSE)


def _eligible(group: str, item: NodeEvaluation) -> bool:
    if group == GROUP_MUSE:
        return item.region_group == GROUP_US
    return item.region_group in (GROUP_US, GROUP_SG, GROUP_JP)


def rank_candidates(evaluations: Iterable[NodeEvaluation], group: str) -> List[NodeEvaluation]:
    """Higher tier first, then lower measured delay, then stable name order."""
    candidates = [item for item in evaluations if _eligible(group, item)]
    return sorted(
        candidates,
        key=lambda item: (
            -tier_of(item.region_group),
            item.delay_ms if item.delay_ms else 10 ** 12,
            item.entry.source,
            item.entry.name,
        ),
    )


def pick_best(evaluations: Iterable[NodeEvaluation], group: str) -> Optional[NodeEvaluation]:
    ranked = rank_candidates(evaluations, group)
    return ranked[0] if ranked else None


def plan_advice(evaluations: Iterable[NodeEvaluation]) -> dict:
    """Advisory plan per group - returned only, never executed."""
    items = list(evaluations)
    return {group: pick_best(items, group) for group in SWITCH_GROUPS}
