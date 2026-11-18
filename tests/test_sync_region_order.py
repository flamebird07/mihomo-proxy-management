"""Tests for the geo ordering / per-source accounting in scripts/sync-feishu-bitable.py.

Covered contracts:
  * interleave_by_source round-robins uneven sources, keeps per-source order,
    handles empty input and empty/unknown source buckets
  * order_names_for_ipinfo puts delay-alive leaves first, dead ones last,
    and never drops a name
  * lookup_regions_isolated: budget exhaustion leaves `remain_budget` counted
    per source, skip_dead on/off, and default-off still attempts dead leaves
  * by_source log fields + `ipinfo done planned/attempted/skipped/remain`
  * --ipinfo-skip-dead default is off (opt-in switch, --no- still works)
  * Stage 2B: dead-leaf time sub-budget / max-probe cap (`dead_budget` log),
    pass2 re-probe of alive+unknown leaves while budget remains,
    meta/info row filtering in build_pool, stale mihomo-feishu-* temp dir
    cleanup, --ipinfo-budget-s / --ipinfo-dead-max passthrough defaults

No network: probe_direct_geo / select_proxy / fetch_geo_via_mixed / urlopen
are stubbed.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "sync-feishu-bitable.py")

spec = importlib.util.spec_from_file_location("sync_feishu_bitable", SCRIPT)
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)


class RecordingLog:
    """Captures log lines instead of touching the real log file."""

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("INFO", message))

    def warn(self, message):
        self.lines.append(("WARN", message))

    def error(self, message):
        self.lines.append(("ERROR", message))

    def text(self):
        return "\n".join(message for _level, message in self.lines)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _ticking_clock(step=1.0):
    """Each call advances by `step`; started = 0, then 1, 2, 3 ..."""
    ticks = {"n": 0}

    def clock():
        value = ticks["n"] * step
        ticks["n"] += 1
        return value

    return clock


def _by_source_lines(log):
    """Parse `ipinfo by_source source=X total=.. tried=..` into {source: {field: int}}."""
    parsed = {}
    for _level, line in log.lines:
        if not line.startswith("ipinfo by_source source="):
            continue
        tokens = line.split()
        source = tokens[2].split("=", 1)[1]
        fields = {}
        for token in tokens[3:]:
            key, _, value = token.partition("=")
            fields[key] = int(value)
        parsed[source] = fields
    return parsed


class LookupHarness(unittest.TestCase):
    """Runs lookup_regions_isolated against stubs."""

    def run_lookup(self, names, source_of=None, delays=None, geo_for=None,
                   skip_dead=False, budget_s=1000.0, clock=None, select_ok=None,
                   geo_seq=None, dead_max_probes=0, dead_budget_ratio=0.2):
        log = RecordingLog()
        geo_for = geo_for or {}
        geo_seq = geo_seq or {}
        select_ok = select_ok or {}
        state = {"now": None, "calls": {}}

        def fake_select(_api, _group, name, _token=None):
            state["now"] = name
            return select_ok.get(name, True)

        def fake_geo(_mixed_port, timeout_s=None):
            name = state["now"]
            state["calls"][name] = state["calls"].get(name, 0) + 1
            seq = geo_seq.get(name)
            if seq:
                idx = min(state["calls"][name] - 1, len(seq) - 1)
                return seq[idx]
            return geo_for.get(name, "未知")

        def fake_urlopen(_url, timeout=None):
            return _FakeResponse({"now": state["now"]})

        patches = [
            patch.object(sync, "probe_direct_geo", lambda timeout_s=None: ""),
            patch.object(sync, "select_proxy", fake_select),
            patch.object(sync, "fetch_geo_via_mixed", fake_geo),
            patch.object(sync.urllib.request, "urlopen", fake_urlopen),
        ]
        if clock is not None:
            patches.append(patch.object(sync.time, "monotonic", clock))
        for item in patches:
            item.start()
        try:
            regions = sync.lookup_regions_isolated(
                "http://127.0.0.1:1", 7890, names, log,
                source_of=source_of, skip_dead=skip_dead, delays=delays,
                budget_s=budget_s, dead_budget_ratio=dead_budget_ratio,
                dead_max_probes=dead_max_probes,
            )
        finally:
            for item in reversed(patches):
                item.stop()
        return regions, log, state


class InterleaveBySourceTests(unittest.TestCase):
    def test_uneven_three_sources_round_robin(self):
        items = [
            ("a1", "A"), ("a2", "A"), ("a3", "A"), ("a4", "A"),
            ("b1", "B"), ("b2", "B"),
            ("c1", "C"),
        ]
        self.assertEqual(
            sync.interleave_by_source(items),
            ["a1", "b1", "c1", "a2", "b2", "a3", "a4"],
        )

    def test_bare_names_with_source_of_mapping(self):
        names = ["a1", "a2", "b1", "c1", "c2"]
        source_of = {"a1": "A", "a2": "A", "b1": "B", "c1": "C", "c2": "C"}
        self.assertEqual(
            sync.interleave_by_source(names, source_of),
            ["a1", "b1", "c1", "a2", "c2"],
        )

    def test_keeps_per_source_order(self):
        names = ["a1", "a2", "a3", "b1", "b2"]
        source_of = {"a1": "A", "a2": "A", "a3": "A", "b1": "B", "b2": "B"}
        result = sync.interleave_by_source(names, source_of)
        self.assertEqual([n for n in result if n.startswith("a")], ["a1", "a2", "a3"])
        self.assertEqual([n for n in result if n.startswith("b")], ["b1", "b2"])

    def test_empty_input(self):
        self.assertEqual(sync.interleave_by_source([]), [])
        self.assertEqual(sync.interleave_by_source([], {}), [])

    def test_empty_source_bucket(self):
        # Empty source name (or missing mapping) is its own bucket, not dropped.
        names = ["a1", "x1", "b1"]
        source_of = {"a1": "A", "b1": "B"}
        result = sync.interleave_by_source(names, source_of)
        self.assertEqual(sorted(result), ["a1", "b1", "x1"])
        self.assertEqual(result, ["a1", "x1", "b1"])

    def test_single_source(self):
        self.assertEqual(
            sync.interleave_by_source(["a1", "a2"], {"a1": "A", "a2": "A"}),
            ["a1", "a2"],
        )


class OrderNamesForIpinfoTests(unittest.TestCase):
    SOURCE_OF = {"a1": "A", "a2": "A", "b1": "B", "c1": "CoffeeCloud", "c2": "CoffeeCloud"}

    def test_all_dead_keeps_every_name(self):
        names = ["a1", "a2", "b1", "c1", "c2"]
        delays = {name: None for name in names}
        ordered = sync.order_names_for_ipinfo(names, delays, self.SOURCE_OF)
        self.assertEqual(sorted(ordered), sorted(names))
        self.assertEqual(ordered, sync.interleave_by_source(names, self.SOURCE_OF))

    def test_all_alive_keeps_every_name(self):
        names = ["a1", "a2", "b1", "c1", "c2"]
        delays = {name: 120 for name in names}
        ordered = sync.order_names_for_ipinfo(names, delays, self.SOURCE_OF)
        self.assertEqual(sorted(ordered), sorted(names))

    def test_only_coffecloud_alive_goes_first(self):
        names = ["a1", "a2", "b1", "c1", "c2"]
        delays = {"a1": None, "a2": None, "b1": None, "c1": 88, "c2": 99}
        ordered = sync.order_names_for_ipinfo(names, delays, self.SOURCE_OF)
        self.assertEqual(ordered[:2], ["c1", "c2"])
        self.assertEqual(sorted(ordered[2:]), ["a1", "a2", "b1"])

    def test_zero_delay_counts_as_dead(self):
        names = ["a1", "b1"]
        delays = {"a1": 0, "b1": 50}
        self.assertEqual(
            sync.order_names_for_ipinfo(names, delays, self.SOURCE_OF), ["b1", "a1"]
        )


class LookupRegionsIsolatedTests(LookupHarness):
    NAMES = ["a1", "a2", "b1", "c1", "c2"]
    SOURCE_OF = {"a1": "A", "a2": "A", "b1": "B", "c1": "CoffeeCloud", "c2": "CoffeeCloud"}
    GEO = {
        "a1": "新加坡/1.1.1.1",
        "a2": "日本/2.2.2.2",
        "b1": "美国/3.3.3.3",
        "c1": "香港/4.4.4.4",
        "c2": "韩国/5.5.5.5",
    }

    def test_budget_exhaustion_counts_remain_per_source(self):
        regions, log, _state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, geo_for=self.GEO,
            budget_s=2.0, clock=_ticking_clock(),
        )
        # started=0, attempts see 1 and 2 (<= 2.0), the third check sees 3 > 2.0.
        self.assertEqual(regions["a1"], self.GEO["a1"])
        self.assertEqual(regions["a2"], self.GEO["a2"])
        for name in ("b1", "c1", "c2"):
            self.assertEqual(regions[name], "未知")

        by_source = _by_source_lines(log)
        self.assertEqual(by_source["A"]["remain_budget"], 0)
        self.assertEqual(by_source["B"]["remain_budget"], 1)
        self.assertEqual(by_source["CoffeeCloud"]["remain_budget"], 2)
        self.assertEqual(by_source["A"]["tried"], 2)
        self.assertEqual(by_source["B"]["tried"], 0)

        text = log.text()
        self.assertIn("ipinfo budget_exhausted attempted=2", text)
        self.assertIn("ipinfo remain_by_source B=1 CoffeeCloud=2", text)

    def test_budget_exhausted_immediately_marks_all_unknown(self):
        regions, log, _state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, geo_for=self.GEO, budget_s=-1.0,
        )
        self.assertTrue(all(regions[name] == "未知" for name in self.NAMES))
        self.assertIn("ipinfo done planned=5 attempted=0 ok=0 skipped=0 remain=5", log.text())

    def test_skip_dead_on_never_attempts_dead_leaves(self):
        delays = {"a1": 100, "a2": None, "b1": 120, "c1": None, "c2": 130}
        regions, log, _state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, delays=delays, geo_for=self.GEO,
            skip_dead=True,
        )
        self.assertEqual(regions["a2"], "未知")
        self.assertEqual(regions["c1"], "未知")
        self.assertEqual(regions["a1"], self.GEO["a1"])
        self.assertEqual(regions["c2"], self.GEO["c2"])

        by_source = _by_source_lines(log)
        self.assertEqual(by_source["A"]["skipped_dead"], 1)
        self.assertEqual(by_source["A"]["tried"], 1)
        self.assertEqual(by_source["CoffeeCloud"]["skipped_dead"], 1)
        self.assertEqual(by_source["CoffeeCloud"]["tried"], 1)
        self.assertIn("ipinfo skip_dead count=2 pending=3", log.text())
        self.assertIn("skipped=2", log.text())

    def test_skip_dead_off_by_default_attempts_dead_leaves(self):
        delays = {"a1": 100, "a2": None, "b1": 120, "c1": None, "c2": 130}
        regions, log, _state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, delays=delays, geo_for=self.GEO,
        )
        # Default is off: a Grok delay failure must not skip the geo probe.
        self.assertEqual(regions["a2"], self.GEO["a2"])
        self.assertEqual(regions["c1"], self.GEO["c1"])

        by_source = _by_source_lines(log)
        self.assertEqual(by_source["A"]["skipped_dead"], 0)
        self.assertEqual(by_source["A"]["tried"], 2)
        self.assertEqual(by_source["CoffeeCloud"]["tried"], 2)
        self.assertIn("ipinfo done planned=5 attempted=5", log.text())
        self.assertNotIn("ipinfo skip_dead", log.text())

    def test_by_source_fields(self):
        select_ok = {"b1": False}
        regions, log, _state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, geo_for=self.GEO, select_ok=select_ok,
        )
        self.assertEqual(regions["b1"], "未知")
        by_source = _by_source_lines(log)
        self.assertEqual(
            by_source["A"],
            {"total": 2, "tried": 2, "ok": 2, "failed_after_try": 0,
             "skipped_dead": 0, "remain_budget": 0, "final_unknown": 0},
        )
        self.assertEqual(
            by_source["B"],
            {"total": 1, "tried": 1, "ok": 0, "failed_after_try": 1,
             "skipped_dead": 0, "remain_budget": 0, "final_unknown": 1},
        )
        self.assertEqual(
            by_source["CoffeeCloud"],
            {"total": 2, "tried": 2, "ok": 2, "failed_after_try": 0,
             "skipped_dead": 0, "remain_budget": 0, "final_unknown": 0},
        )
        self.assertIn("ipinfo done planned=5 attempted=5 ok=4 skipped=0 remain=0", log.text())

    def test_final_unknown_reflects_diversity_blanking(self):
        # Identical geo for every leaf trips validate_region_diversity, which
        # blanks the whole table; final_unknown must report that, not the ok count.
        same = {name: "新加坡/9.9.9.9" for name in self.NAMES}
        regions, log, _state = self.run_lookup(self.NAMES, self.SOURCE_OF, geo_for=same)
        self.assertTrue(all(regions[name] == "未知" for name in self.NAMES))
        by_source = _by_source_lines(log)
        self.assertEqual(by_source["A"]["final_unknown"], 2)
        self.assertEqual(by_source["B"]["final_unknown"], 1)
        self.assertEqual(by_source["CoffeeCloud"]["final_unknown"], 2)
        self.assertIn("diversity_fail", log.text())


class SkipDeadDefaultTests(unittest.TestCase):
    def test_cli_default_is_off(self):
        self.assertFalse(sync.parse_args([]).ipinfo_skip_dead)
        self.assertFalse(sync.parse_args(["--no-ipinfo-skip-dead"]).ipinfo_skip_dead)
        self.assertTrue(sync.parse_args(["--ipinfo-skip-dead"]).ipinfo_skip_dead)

    def test_test_isolated_default_is_off(self):
        import inspect

        params = inspect.signature(sync.test_isolated).parameters
        self.assertFalse(params["ipinfo_skip_dead"].default)

    def test_lookup_default_is_off(self):
        import inspect

        params = inspect.signature(sync.lookup_regions_isolated).parameters
        self.assertFalse(params["skip_dead"].default)


class DeadBudgetTests(LookupHarness):
    """Delay-dead leaves: short timeout + ≤20% sub-budget (+ optional max N)."""

    NAMES = ["a1", "d1", "d2", "d3"]
    SOURCE_OF = {"a1": "A", "d1": "A", "d2": "A", "d3": "A"}
    GEO = {
        "a1": "新加坡/1.1.1.1",
        "d1": "日本/2.2.2.2",
        "d2": "美国/3.3.3.3",
        "d3": "香港/4.4.4.4",
    }
    DELAYS = {"a1": 50, "d1": None, "d2": 0, "d3": None}

    def _dead_log_line(self, log):
        lines = [m for _lvl, m in log.lines if "ipinfo dead_budget" in m]
        self.assertTrue(lines, "dead_budget summary line missing")
        return lines[0]

    def test_dead_time_sub_budget_stops_later_dead_probes(self):
        # Ticking clock: each monotonic() burns 1s. budget=10 → dead cap=2s.
        # Per dead probe the clock is called: header + before + after = 3 ticks
        # but only (after-before)=2s is counted as dead time... verify with the
        # actual call pattern: d1 consumes ticks 3,4 → 1.0s; d2 ticks 6,7 → 1.0s;
        # after d2, dead_spent=2.0 >= cap → d3 skipped as dead_budget, not probed.
        regions, log, state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, delays=self.DELAYS, geo_for=self.GEO,
            budget_s=10.0, clock=_ticking_clock(),
        )
        self.assertEqual(regions["a1"], self.GEO["a1"])
        self.assertEqual(regions["d1"], self.GEO["d1"])
        self.assertEqual(regions["d2"], self.GEO["d2"])
        self.assertEqual(regions["d3"], "未知")
        self.assertNotIn("d3", state["calls"], "d3 must not be probed after cap")
        line = self._dead_log_line(log)
        self.assertIn("probed=2", line)
        self.assertIn("skipped=1", line)
        text = log.text()
        self.assertIn("dead_skipped=1", text)
        # skipped_dead (the flag path) must stay distinct from dead_budget.
        by_source = _by_source_lines(log)
        self.assertEqual(by_source["A"]["skipped_dead"], 0)

    def test_dead_max_probes_caps_attempt_count(self):
        regions, log, state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, delays=self.DELAYS, geo_for=self.GEO,
            dead_max_probes=1,
        )
        self.assertEqual(regions["d1"], self.GEO["d1"])
        self.assertEqual(regions["d2"], "未知")
        self.assertEqual(regions["d3"], "未知")
        self.assertEqual(state["calls"].get("d2"), None)
        line = self._dead_log_line(log)
        self.assertIn("probed=1", line)
        self.assertIn("skipped=2", line)

    def test_alive_nodes_ignore_dead_budget(self):
        delays = {name: 100 for name in self.NAMES}
        regions, log, state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, delays=delays, geo_for=self.GEO,
            dead_max_probes=1,
        )
        for name in self.NAMES:
            self.assertEqual(regions[name], self.GEO[name])
        self.assertIn("probed=0", self._dead_log_line(log))


class Pass2Tests(LookupHarness):
    """After pass1, alive-but-unknown leaves are re-probed while budget remains."""

    NAMES = ["a1", "b1", "d1"]
    SOURCE_OF = {"a1": "A", "b1": "B", "d1": "A"}
    GEO = {"a1": "美国/3.3.3.3", "b1": "日本/2.2.2.2", "d1": "香港/4.4.4.4"}
    DELAYS = {"a1": 50, "b1": 60, "d1": None}

    def test_pass2_recovers_alive_unknown(self):
        regions, log, state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, delays=self.DELAYS,
            geo_seq={"a1": ["未知", "美国/3.3.3.3"]}, geo_for=self.GEO,
        )
        self.assertEqual(regions["a1"], "美国/3.3.3.3")
        self.assertEqual(state["calls"]["a1"], 2, "a1 probed twice (pass1 fail + pass2)")
        self.assertIn("ipinfo pass2 targets=1 tried=1 ok=1", log.text())
        self.assertIn("pass2_tried=1 pass2_ok=1", log.text())

    def test_pass2_excludes_dead_and_already_ok(self):
        regions, log, state = self.run_lookup(
            self.NAMES, self.SOURCE_OF, delays=self.DELAYS,
            geo_seq={"d1": ["未知", "never"]}, geo_for={k: v for k, v in self.GEO.items() if k != "d1"},
        )
        # d1 is dead+unknown: pass2 must not re-probe it (delay>0 gate).
        self.assertEqual(state["calls"].get("d1"), 1)
        self.assertEqual(state["calls"].get("b1"), 1)
        self.assertIn("ipinfo pass2 targets=0 tried=0 ok=0", log.text())

    def test_pass2_skipped_when_budget_exhausted(self):
        names = ["a1", "a2", "a3"]
        source_of = {n: "A" for n in names}
        delays = {n: 50 for n in names}
        geo = {"a1": "未知", "a2": "日本/2.2.2.2", "a3": "美国/3.3.3.3"}
        regions, log, state = self.run_lookup(
            names, source_of, delays=delays, geo_for=geo,
            budget_s=2.0, clock=_ticking_clock(),
        )
        # pass1: a1 probed (unknown), a2 probed, a3 hits budget_exhausted.
        self.assertIn("ipinfo budget_exhausted attempted=2", log.text())
        # pass2 gate: no wall-clock left → nothing re-probed.
        self.assertEqual(state["calls"].get("a1"), 1)
        self.assertIn("ipinfo pass2 targets=2 tried=0 ok=0", log.text())


class MetaFilterTests(unittest.TestCase):
    INFO_NAMES = [
        "📱剩余流量：128GB",
        "流量重置时间",
        "到期时间: 2026-11-01",
        "官网 www.example.com",
        "Traffic 50GB Left",
        "Expire 2026/12/31",
        "Reset Quota Here",
        "Official Website",
    ]
    NODE_NAMES = [
        "🇺🇸 美国-01",
        "CoffeeCloud-JP-优化",
        "TW-HS-3",
        "Singapore 02",
    ]

    def test_info_rows_flagged(self):
        for name in self.INFO_NAMES:
            self.assertTrue(sync.is_meta_info_name(name), name)

    def test_real_nodes_not_flagged(self):
        for name in self.NODE_NAMES:
            self.assertFalse(sync.is_meta_info_name(name), name)

    def test_empty_name_not_flagged(self):
        self.assertFalse(sync.is_meta_info_name(""))

    def test_build_pool_drops_meta_rows(self):
        src = sync.SourceResult(3, "CoffeeCloud")
        src.nodes = [
            {"name": "🇺🇸 美国-01", "type": "ss", "server": "h1", "port": 1},
            {"name": "剩余流量：128GB", "type": "ss", "server": "h2", "port": 2},
            {"name": "Expire 2026", "type": "ss", "server": "h3", "port": 3},
            {"name": "JP-02", "type": "ss", "server": "h4", "port": 4},
        ]
        log = RecordingLog()
        pool = sync.build_pool([src], log=log)
        names = [node["name"] for node, _ in pool]
        self.assertEqual(names, ["🇺🇸 美国-01", "JP-02"])
        self.assertIn("pool meta_skipped count=2", log.text())

    def test_build_pool_without_log_still_filters(self):
        src = sync.SourceResult(3, "CoffeeCloud")
        src.nodes = [{"name": "官网入口", "type": "ss", "server": "h", "port": 1}]
        self.assertEqual(sync.build_pool([src]), [])


class CorpseCleanupTests(unittest.TestCase):
    def test_find_stale_dirs_only_matching_and_old(self):
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as root:
            old_dir = Path(root) / "mihomo-feishu-old"
            fresh_dir = Path(root) / "mihomo-feishu-fresh"
            other_dir = Path(root) / "not-mihomo"
            for d in (old_dir, fresh_dir, other_dir):
                d.mkdir()
            stale = sync.find_stale_corpse_dirs(
                [root], max_age_s=1800.0, now=time.time(),
            )
            self.assertEqual(stale, [])
            # age them artificially
            past = time.time() - 7200
            os.utime(old_dir, (past, past))
            stale = sync.find_stale_corpse_dirs([root], max_age_s=1800.0)
            self.assertEqual([p.name for p in stale], ["mihomo-feishu-old"])

    def test_cleanup_removes_stale_keeps_production(self):
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as root:
            old_dir = Path(root) / "mihomo-feishu-old"
            fresh_dir = Path(root) / "mihomo-feishu-fresh"
            prod = Path(root) / "mihomo-production"
            for d in (old_dir, fresh_dir, prod):
                d.mkdir()
                (d / "config.yaml").write_text("x", encoding="utf-8")
            # Stale + proven-dead owner (nonexistent pid) may be deleted.
            # Process count is always 0; the kill helper no longer exists.
            (old_dir / "owner.json").write_text(
                json.dumps({"pid": 1_999_999_999, "create_time": 1.0}),
                encoding="utf-8",
            )
            past = time.time() - 7200
            os.utime(old_dir, (past, past))
            log = RecordingLog()
            procs, dirs = sync.cleanup_temp_corpses(log, temp_roots=[root])
            self.assertEqual(procs, 0)
            self.assertEqual(dirs, 1)
            self.assertFalse(old_dir.exists())
            self.assertTrue(fresh_dir.exists())
            self.assertTrue(prod.exists())
            self.assertFalse(hasattr(sync, "_kill_corpse_processes"))


class Stage2BFlagTests(unittest.TestCase):
    def test_ipinfo_budget_s_default_and_override(self):
        self.assertEqual(sync.parse_args([]).ipinfo_budget_s, sync.IPINFO_TOTAL_BUDGET_S)
        self.assertEqual(sync.parse_args(["--ipinfo-budget-s", "120"]).ipinfo_budget_s, 120.0)

    def test_ipinfo_dead_max_default(self):
        self.assertEqual(sync.parse_args([]).ipinfo_dead_max, 0)
        self.assertEqual(sync.parse_args(["--ipinfo-dead-max", "8"]).ipinfo_dead_max, 8)

    def test_dead_constants(self):
        self.assertEqual(sync.IPINFO_DEAD_TIMEOUT_S, 2.0)
        self.assertEqual(sync.IPINFO_DEAD_BUDGET_RATIO, 0.2)

    def test_test_isolated_pass_through_defaults(self):
        import inspect

        params = inspect.signature(sync.test_isolated).parameters
        self.assertEqual(params["ipinfo_budget_s"].default, sync.IPINFO_TOTAL_BUDGET_S)
        self.assertEqual(params["ipinfo_dead_max"].default, 0)


if __name__ == "__main__":
    unittest.main()
