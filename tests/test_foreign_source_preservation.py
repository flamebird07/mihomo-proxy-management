"""Foreign-source preservation in the sync-chain table replace (multi-host).

Scenario: Windows and macOS both run sync-feishu-bitable.py against the same
Feishu tables but hold different source sets (Windows: 飞兔/CoffeeCloud/西瓜;
mac: wmsxwd only).  The old planner replaced every main-path row with the
local payload, so whichever host ran last wiped the other host's sources.
The new planner keeps main-path rows whose 来源 is absent from the payload
while their 同步时间 is fresh, and prunes them after the foreign-staleness
window; Windows' own full-run payloads still cover every group, so its
end-state is unchanged.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-feishu-bitable.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("sync_under_test", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

NOW_MS = 1_700_000_000_000


def _row(rid: str, source: str, *, path: str = "订阅 Mihomo",
         age_minutes: float | None = 10.0) -> dict:
    fields = {"来源": source, "测速路径": path}
    if age_minutes is not None:
        fields["同步时间"] = NOW_MS - int(age_minutes * 60 * 1000)
    return {"record_id": rid, "fields": fields}


def _payload_rows(source: str) -> list[dict]:
    return [{"fields": {"来源": source, "测速路径": "订阅 Mihomo",
                        "节点名": f"{source}-fresh"}}]


class ForeignSourceTests(unittest.TestCase):
    def plan(self, existing, records, **kwargs):
        return sync.plan_table_deletions(
            existing, records, preserve_ninja=True, table_key="nodes",
            now_ms=NOW_MS, stale_minutes=60, log=None, **kwargs)

    def test_foreign_fresh_row_is_kept(self):
        """Mac payload (飞兔 only) must not delete Windows' fresh CoffeeCloud row."""
        existing = [
            _row("r1", "飞兔", age_minutes=10),
            _row("r2", "CoffeeCloud", age_minutes=30),
            _row("r3", "西瓜加速", age_minutes=30),
        ]
        plan = self.plan(existing, _payload_rows("飞兔"))
        deleted = [item["record_id"] for item in plan]
        self.assertEqual(deleted, ["r1"],
                         "only the payload-owned group is replaced; foreign rows stay")

    def test_foreign_stale_row_is_pruned(self):
        existing = [
            _row("r1", "飞兔"),
            _row("r2", "CoffeeCloud", age_minutes=8 * 24 * 60),  # 8 days
        ]
        plan = self.plan(existing, _payload_rows("飞兔"))
        self.assertEqual([item["record_id"] for item in plan], ["r1", "r2"])
        self.assertIn("stale_foreign_source", [item["reason"] for item in plan])

    def test_foreign_row_with_unreadable_sync_time_is_kept(self):
        """Fail-closed: an unreadable 同步时间 keeps the foreign row."""
        existing = [
            _row("r1", "飞兔"),
            _row("r2", "CoffeeCloud", age_minutes=None),
        ]
        plan = self.plan(existing, _payload_rows("飞兔"))
        self.assertEqual([item["record_id"] for item in plan], ["r1"])

    def test_custom_window_overrides_the_default(self):
        existing = [
            _row("r1", "飞兔"),
            _row("r2", "CoffeeCloud", age_minutes=2 * 24 * 60),  # 2 days
        ]
        plan = self.plan(existing, _payload_rows("飞兔"), foreign_stale_minutes=1440)
        self.assertEqual(sorted(item["record_id"] for item in plan), ["r1", "r2"])

    def test_full_payload_replaces_every_group(self):
        """Windows parity: a payload covering all sources replaces them all."""
        existing = [
            _row("r1", "飞兔"),
            _row("r2", "CoffeeCloud"),
            _row("r3", "西瓜加速"),
        ]
        records = _payload_rows("飞兔") + _payload_rows("CoffeeCloud") + _payload_rows("西瓜加速")
        plan = self.plan(existing, records)
        self.assertEqual(sorted(item["record_id"] for item in plan), ["r1", "r2", "r3"])

    def test_empty_source_row_stays_writer_owned(self):
        """Rows without 来源 attribution keep the historical replace behavior."""
        existing = [{"record_id": "r0", "fields": {"测速路径": "订阅 Mihomo"}},
                    _row("r1", "飞兔")]
        plan = self.plan(existing, _payload_rows("飞兔"))
        self.assertEqual(sorted(item["record_id"] for item in plan), ["r0", "r1"])

    def test_foreign_source_stale_minutes_default_and_env(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(sync.FOREIGN_SOURCE_STALE_MINUTES_ENV, None)
            self.assertEqual(sync.foreign_source_stale_minutes(), 7 * 24 * 60)
        with mock.patch.dict(os.environ,
                             {sync.FOREIGN_SOURCE_STALE_MINUTES_ENV: "2880"}):
            self.assertEqual(sync.foreign_source_stale_minutes(), 2880)
        with mock.patch.dict(os.environ,
                             {sync.FOREIGN_SOURCE_STALE_MINUTES_ENV: "junk"}):
            self.assertEqual(sync.foreign_source_stale_minutes(), 7 * 24 * 60)


if __name__ == "__main__":
    unittest.main()
