"""Small in-memory data types used across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class NodeEntry:
    """One node read from the 「节点测速总表」 table.

    ``name`` is the node name and part of the identity key (source, name).
    ``source_number`` is the numeric source id (1飞兔/2星辰/3CoffeeCloud/
    4西瓜加速) written by node_provider; it is preserved verbatim.
    ``probe_name`` is unique inside this run so that two sources offering the
    same node name do not collide in the temporary core.
    ``config`` is the full mihomo proxy dict (lossless provenance).
    """

    name: str
    source: str
    config: Dict[str, Any]
    probe_name: str
    source_number: int = 0

    @property
    def identity(self) -> tuple[str, str]:
        return (self.source, self.name)


@dataclass
class NodeEvaluation:
    entry: NodeEntry
    delay_ms: Optional[int]
    chatgpt_ms: Optional[int]
    openai_ms: Optional[int]
    actual_area: str
    region_group: str
    passed: bool
    streak: int

    @property
    def gpt_available(self) -> bool:
        return self.passed and self.streak >= 2
