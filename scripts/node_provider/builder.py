"""Rebuild mihomo proxies from 「有效节点」 rows.

Pure, side-effect-free logic: no I/O, no Feishu, no filesystem.  The caller
fetches rows and writes the result.

Pipeline per row:
  GPT可用 filter (unless --all-valid) -> 地区组 filter -> freshness guard ->
  decode 节点配置JSON -> structural validation -> dedupe -> unique names.

De-duplication and name-suffix semantics mirror ``convert_sub.merge_proxies``
(copied/rewritten; no runtime import): identity is the proxy dict minus
``name``; a name collision gets ``<name> [2]``, ``[3]``, ... and never shadows
a name reserved by another input node.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import schema

DEFAULT_MAX_AGE_MINUTES = 1440


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


def field_bool(value: Any) -> bool:
    """Read a Bitable checkbox value.  Missing/unknown counts as False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, list):
        return any(field_bool(item) for item in value)
    if isinstance(value, dict):
        if "value" in value:
            return field_bool(value.get("value"))
        text = value.get("text")
        return str(text).strip().lower() in ("true", "1", "yes") if text is not None else False
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def field_timestamp_ms(value: Any) -> Optional[int]:
    """Read a Bitable date-time field (epoch milliseconds)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        return None
    if isinstance(value, list):
        for item in value:
            parsed = field_timestamp_ms(item)
            if parsed is not None:
                return parsed
    if isinstance(value, dict) and "value" in value:
        return field_timestamp_ms(value.get("value"))
    return None


def _fields(row: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    fields = row.get("fields")
    return fields if isinstance(fields, dict) else None


_AD_NAME = re.compile(r"★|官网|招募|防丢失|网不通|更新订阅|请咨询|客服")


def is_ad_node_name(name: Any) -> bool:
    """True for subscription advertisement entries (``★V-官网``, ``★!!!网不通
    重新更新订阅`` …).  They carry a real server so a url-test can "pass" them,
    but they are not proxies; keeping them in the valid pool lets auto-switch
    route GPT traffic through an ad endpoint."""
    return isinstance(name, str) and bool(_AD_NAME.search(name))


def _valid_proxy(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    name = config.get("name")
    ptype = config.get("type")
    server = config.get("server")
    if not isinstance(name, str) or not name.strip():
        return False
    if not isinstance(ptype, str) or not ptype.strip():
        return False
    if not isinstance(server, str) or not server.strip():
        return False
    if "port" not in config:
        return False
    port = config.get("port")
    if isinstance(port, bool) or port is None or port == "":
        return False
    if isinstance(port, int):
        return True
    return isinstance(port, str) and port.strip().isdigit()


def semantic_key(proxy: Dict[str, Any]) -> str:
    """Identity of a node: the whole dict except its display name."""
    return json.dumps({k: v for k, v in proxy.items() if k != "name"},
                      sort_keys=True, ensure_ascii=False, allow_nan=False)


@dataclass
class BuildResult:
    proxies: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    gpt_skipped: int = 0
    region_skipped: int = 0
    stale_skipped: int = 0
    invalid: int = 0
    duplicates: int = 0
    ad_skipped: int = 0

    @property
    def selected(self) -> int:
        return len(self.proxies)

    def summary(self) -> str:
        return (f"rows total={self.total} gpt_skipped={self.gpt_skipped} "
                f"region_skipped={self.region_skipped} stale_skipped={self.stale_skipped} "
                f"invalid={self.invalid} duplicates={self.duplicates} "
                f"ad_skipped={self.ad_skipped} selected={self.selected}")


def build_proxies(rows: Iterable[Any], *, all_valid: bool = False,
                  regions: Optional[Sequence[str]] = None,
                  max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
                  allow_stale: bool = False,
                  now_ms: Optional[int] = None) -> BuildResult:
    result = BuildResult()
    wanted_regions = set(regions or ())
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    max_age_ms = int(max_age_minutes) * 60_000

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        result.total += 1
        fields = _fields(row)
        if fields is None:
            result.invalid += 1
            continue
        if not all_valid and not field_bool(fields.get("GPT可用")):
            result.gpt_skipped += 1
            continue
        if wanted_regions:
            group = field_text(fields.get("地区组")).strip()
            if group not in wanted_regions:
                result.region_skipped += 1
                continue
        sync_ms = field_timestamp_ms(fields.get("同步时间"))
        if not allow_stale:
            # Missing sync time is treated as unverifiable => stale.
            if sync_ms is None or now_ms - sync_ms > max_age_ms:
                result.stale_skipped += 1
                continue
        try:
            config = json.loads(field_text(fields.get("节点配置JSON")))
        except (ValueError, TypeError):
            result.invalid += 1
            continue
        if is_ad_node_name(config.get("name") if isinstance(config, dict) else None):
            result.ad_skipped += 1
            continue
        if not _valid_proxy(config):
            result.invalid += 1
            continue
        candidates.append(config)

    reserved = {cfg["name"] for cfg in candidates}
    seen: set[str] = set()
    used_names: set[str] = set()
    for index, config in enumerate(candidates):
        try:
            key = semantic_key(config)
        except (ValueError, TypeError, RecursionError):
            result.invalid += 1
            continue
        if key in seen:
            result.duplicates += 1
            continue
        seen.add(key)
        name = config["name"]
        candidate, suffix = name, 2
        while candidate in used_names or (candidate != name and candidate in reserved):
            candidate = f"{name} [{suffix}]"
            suffix += 1
        used_names.add(candidate)
        result.proxies.append({**config, "name": candidate})

    return result


def select_table(tables: Iterable[dict], name: str = schema.VALID_TABLE_NAME) -> Optional[str]:
    for table in tables:
        if isinstance(table, dict) and table.get("name") == name:
            return table.get("table_id") or None
    return None


def table_names(tables: Iterable[dict]) -> List[str]:
    return [str(t.get("name")) for t in tables if isinstance(t, dict) and t.get("name")]
