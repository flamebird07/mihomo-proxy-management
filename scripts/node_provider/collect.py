"""Collection orchestration for node_provider --collect.

Two extraction channels feed one merged node pool:

  subscription - HTTP fetch + three-format parse + semantic de-dupe
  app          - explicit, opt-in extraction from source1/source3

The channels are independent: one failing source never blocks the others.  A
source returning HTTP 200 with an empty body is classified ``empty`` and
counted as failed, never raised.

The merged pool replaces 「节点测速总表」.  Only identity/config columns are
written; measurement columns are reset to defaults and belong to node_ops.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import app_extract, schema
from .subscription import (
    SubscriptionError,
    convert_content,
    dedupe_nodes,
    fetch_subscription,
    semantic_key,
    source_settings,
)

MEASUREMENT_DEFAULTS: Dict[str, Any] = {
    "实际地区": "",
    "地区组": "未知",
    "延迟ms": -1,
    "chatgpt延迟ms": -1,
    "openai延迟ms": -1,
    "存活": False,
    "测速目标": "",
}


@dataclass
class CollectedNode:
    config: Dict[str, Any]
    source: str
    source_number: int


@dataclass
class CollectResult:
    nodes: List[CollectedNode] = field(default_factory=list)
    per_channel: Dict[str, int] = field(default_factory=dict)
    failed: List[str] = field(default_factory=list)
    attempted: int = 0
    duplicates: int = 0

    @property
    def collected(self) -> int:
        return len(self.nodes)

    def summary(self) -> str:
        channels = ",".join(f"{key}={value}" for key, value in sorted(self.per_channel.items()))
        return (f"collected={self.collected} duplicates={self.duplicates} "
                f"failed={len(self.failed)} channels=[{channels}]")


def _merge(items: Iterable[CollectedNode]) -> tuple[List[CollectedNode], int]:
    """Semantic de-dupe across channels (name ignored) + unique names."""
    merged: List[CollectedNode] = []
    seen: set[str] = set()
    used_names: set[str] = set()
    duplicates = 0
    reserved = {item.config["name"] for item in items
                if isinstance(item.config.get("name"), str)}
    for item in items:
        try:
            key = semantic_key(item.config)
        except SubscriptionError:
            duplicates += 1
            continue
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        name = item.config.get("name")
        if not isinstance(name, str) or not name.strip():
            name = f"node#{len(merged) + 1}"
        candidate, suffix = name, 2
        while candidate in used_names or (candidate != name and candidate in reserved):
            candidate = f"{name} [{suffix}]"
            suffix += 1
        used_names.add(candidate)
        merged.append(CollectedNode(
            config={**item.config, "name": candidate},
            source=item.source,
            source_number=item.source_number,
        ))
    return merged, duplicates


SUBSCRIPTION_RETRIES = 2


def _collect_subscription(env_values: Dict[str, str], timeout: int,
                          fetcher: Callable[[str, str, int], str],
                          warn: Callable[[str], None],
                          result: CollectResult) -> List[CollectedNode]:
    nodes: List[CollectedNode] = []
    settings_by_index = source_settings(env_values)
    for index in range(1, 4):
        source = schema.SOURCE_BY_INDEX[index]
        info = settings_by_index.get(str(index))
        if info is None:
            warn(f"source={source} skipped reason=no_url")
            continue
        result.attempted += 1
        try:
            content = None
            for attempt in range(SUBSCRIPTION_RETRIES + 1):
                try:
                    content = fetcher(info["url"], info["ua"], timeout)
                    break
                except SubscriptionError as exc:
                    if exc.kind != "network" or attempt >= SUBSCRIPTION_RETRIES:
                        raise
                    warn(f"source={source} retry={attempt + 1} class=network")
                    time.sleep(1 + attempt)
            if not content or not content.strip():
                raise SubscriptionError("empty body", "empty")
            parsed = dedupe_nodes(convert_content(content, warn=warn))
            if not parsed:
                raise SubscriptionError("source produced no candidates", "empty")
        except SubscriptionError as exc:
            result.failed.append(f"subscription:{source}")
            warn(f"source={source} failed class={exc.kind}")
            continue
        except Exception as exc:  # pragma: no cover - defensive
            result.failed.append(f"subscription:{source}")
            warn(f"source={source} failed class={type(exc).__name__.lower()}")
            continue
        result.per_channel[f"subscription:{source}"] = len(parsed)
        nodes.extend(CollectedNode(config=node, source=source, source_number=index)
                     for node in parsed)
        warn(f"source={source} collected={len(parsed)}")
    return nodes


def _collect_app(from_app: List[str], warn: Callable[[str], None],
                 result: CollectResult, *,
                 source1_pid: Optional[int],
                 source1_exe, source1_reader,
                 vault_dir, hostname) -> List[CollectedNode]:
    nodes: List[CollectedNode] = []
    for name in from_app:
        source_number = app_extract.APP_SOURCE_NUMBER[name]
        source = schema.SOURCE_BY_INDEX[source_number]
        result.attempted += 1
        try:
            extracted = app_extract.extract_app(
                name,
                pid=source1_pid,
                source1_exe=source1_exe,
                source1_reader=source1_reader,
                vault_dir=vault_dir,
                hostname=hostname,
            )
        except app_extract.AppExtractError as exc:
            result.failed.append(f"app:{name}")
            warn(f"source={source} app={name} failed class={exc.kind}")
            continue
        except Exception as exc:  # pragma: no cover - defensive
            result.failed.append(f"app:{name}")
            warn(f"source={source} app={name} failed class={type(exc).__name__.lower()}")
            continue
        result.per_channel[f"app:{name}"] = len(extracted)
        nodes.extend(CollectedNode(config=node, source=source, source_number=source_number)
                     for node in extracted)
        warn(f"source={source} app={name} collected={len(extracted)}")
    return nodes


def collect(env_values: Dict[str, str], *, timeout: int = 30,
            fetcher: Callable[[str, str, int], str] = fetch_subscription,
            from_app: Optional[List[str]] = None,
            source1_pid: Optional[int] = None,
            source1_exe=app_extract.DEFAULT_SOURCE1_EXE,
            source1_reader: Optional[Any] = None,
            vault_dir=None,
            hostname: Optional[str] = None,
            warn: Optional[Callable[[str], None]] = None) -> CollectResult:
    log = warn or (lambda _message: None)
    result = CollectResult()
    candidates: List[CollectedNode] = []
    candidates.extend(_collect_subscription(env_values, timeout, fetcher, log, result))
    if from_app:
        candidates.extend(_collect_app(
            list(dict.fromkeys(from_app)), log, result,
            source1_pid=source1_pid, source1_exe=source1_exe,
            source1_reader=source1_reader, vault_dir=vault_dir, hostname=hostname,
        ))
    result.nodes, result.duplicates = _merge(candidates)
    return result


def build_records(nodes: Iterable[CollectedNode], sync_ms: int) -> List[Dict[str, Any]]:
    """Identity/config columns only; measurement columns are reset to defaults."""
    records: List[Dict[str, Any]] = []
    for node in nodes:
        fields: Dict[str, Any] = {
            "节点名": node.config["name"],
            "来源": node.source,
            "源编号": node.source_number,
            "协议": str(node.config.get("type") or ""),
            "节点配置JSON": json.dumps(node.config, ensure_ascii=False, allow_nan=False),
            "同步时间": sync_ms,
        }
        fields.update(MEASUREMENT_DEFAULTS)
        records.append({"fields": fields})
    return records


def now_ms() -> int:
    return int(time.time() * 1000)
