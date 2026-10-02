"""Non-secret state files under ``/var/lib/mihomo-proxy-management/state``.

``plan.json`` records the decisions the CLI made (mode, providers, asset
suffix) so ``status`` can answer without reading secrets.  ``degraded.json``
records *why* the deployment is DEGRADED, as required by D3=B: an explicit,
human-readable reason that survives the process.

Both files are non-secret and mode 0600 anyway (they live beside the rendered
config and inherit the same hygiene).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .atomicio import read_text_strict, write_atomic
from .errors import FailClosed
from .sanitize import HOST

STATE_VERSION = 1

HEALTH_OK = "OK"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_DISABLED = "DISABLED"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Degraded:
    """Explicit degradation record; ``active`` is the single source of truth."""

    active: bool = False
    mode: str = ""
    reasons: list[str] = field(default_factory=list)
    cn: str = HEALTH_OK  # OK | DEGRADED | DISABLED
    cn_details: list[str] = field(default_factory=list)
    updated_at: str = ""

    def status(self) -> str:
        if self.active or self.cn == HEALTH_DEGRADED:
            return HEALTH_DEGRADED
        return HEALTH_OK

    def all_reasons(self) -> list[str]:
        return list(self.reasons) + list(self.cn_details)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": STATE_VERSION,
            "status": self.status(),
            "degraded": self.active,
            "mode": self.mode,
            "reasons": [HOST.text(r) for r in self.reasons],
            "cn": self.cn,
            "cn_details": [HOST.text(r) for r in self.cn_details],
            "updated_at": self.updated_at or now_iso(),
        }


@dataclass
class Plan:
    """The decisions taken at configure time (never secret material)."""

    schema_version: int = STATE_VERSION
    mode: str = ""
    asset_suffix: str = ""
    mihomo_tag: str = ""
    installed_version: str = ""
    foreign_provider: str = ""
    cn_provider: str = ""
    cn_configured: bool = False
    foreign_description: str = ""
    cn_description: str = ""
    ports: dict[str, int] = field(default_factory=dict)
    config_sha256: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read_json(path: str) -> dict[str, object]:
    if not os.path.isfile(path):
        return {}
    try:
        loaded = json.loads(read_text_strict(path))
    except FailClosed:
        raise
    except ValueError as exc:
        raise FailClosed(f"state file is not valid JSON: {os.path.basename(path)}") from exc
    if not isinstance(loaded, dict):
        raise FailClosed(f"state file must contain an object: {os.path.basename(path)}")
    return loaded


def write_json(path: str, payload: dict[str, object]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    write_atomic(path, text, mode=0o600)


def load_degraded(layout) -> Degraded:
    data = read_json(layout.degraded_file)
    return Degraded(
        active=bool(data.get("degraded", False)),
        mode=str(data.get("mode", "")),
        reasons=[str(item) for item in data.get("reasons", []) if isinstance(item, str)],
        cn=str(data.get("cn", HEALTH_OK)),
        cn_details=[str(item) for item in data.get("cn_details", []) if isinstance(item, str)],
        updated_at=str(data.get("updated_at", "")),
    )


def save_degraded(layout, degraded: Degraded) -> None:
    degraded.updated_at = now_iso()
    write_json(layout.degraded_file, degraded.to_dict())


def load_plan(layout) -> Plan:
    data = read_json(layout.plan_file)
    plan = Plan()
    for key, value in data.items():
        if hasattr(plan, key):
            setattr(plan, key, value)
    return plan


def save_plan(layout, plan: Plan) -> None:
    plan.updated_at = now_iso()
    plan.schema_version = STATE_VERSION
    write_json(layout.plan_file, plan.to_dict())


def backup(layout, path: str, *, keep: int = 3) -> str | None:
    """Copy a previous rendered config into the 0700 backup area.

    The index stores hashes and sizes only - never the file contents, and the
    filenames are opaque so nothing about the secret inputs leaks.
    """
    if not os.path.isfile(path):
        return None
    os.makedirs(layout.backups_dir, exist_ok=True)
    os.chmod(layout.backups_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"config.{stamp}.bak"
    dest = os.path.join(layout.backups_dir, name)
    with open(path, "rb") as handle:
        payload = handle.read()
    write_atomic(dest, payload, mode=0o600, mkdir=False)
    index = read_json(layout.backup_index)
    entries = index.get("entries")
    entries = list(entries) if isinstance(entries, list) else []
    entries.append(
        {
            "name": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "created_at": stamp,
        }
    )
    entries = entries[-keep:]
    # prune files that fell out of the window
    keep_names = {str(entry.get("name")) for entry in entries}
    for existing in sorted(os.listdir(layout.backups_dir)):
        if existing.startswith("config.") and existing not in keep_names:
            try:
                os.unlink(os.path.join(layout.backups_dir, existing))
            except OSError:
                pass
    write_json(
        layout.backup_index,
        {"schema_version": STATE_VERSION, "entries": entries, "updated_at": now_iso()},
    )
    return dest


def backup_index(layout) -> dict[str, object]:
    data = read_json(layout.backup_index)
    return {
        "schema_version": data.get("schema_version", STATE_VERSION),
        "entries": data.get("entries", []),
    }


__all__ = [
    "HEALTH_DEGRADED",
    "HEALTH_DISABLED",
    "HEALTH_OK",
    "STATE_VERSION",
    "Degraded",
    "Plan",
    "backup",
    "backup_index",
    "load_degraded",
    "load_plan",
    "now_iso",
    "read_json",
    "save_degraded",
    "save_plan",
    "write_json",
]
