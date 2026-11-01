"""Tests for the switch threshold / debounce logic in scripts/proxy-auto-switch.py.

Covers the regression table of the design (section 6.1) plus the state
migration (4.1), the node history rules (2.1) and the logger fix (4.2).

No network: decide_group and the state helpers are pure; the main() tests stub
api_json / request_delay / time.sleep and point every path at a temp dir.
"""

import contextlib
import copy
import datetime as dt
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "proxy-auto-switch.py")

spec = importlib.util.spec_from_file_location("proxy_auto_switch", SCRIPT)
pas = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pas
spec.loader.exec_module(pas)

NOW = dt.datetime(2026, 11, 1, 12, 0, 0)
US1, US2, US3, US4 = "美国 01", "美国 02", "美国 03", "美国 04"
SG1, SG2, JP1 = "新加坡 01", "新加坡 02", "日本 01"
MEMBERS = [US1, US2, US3, SG1, JP1]
PARAMS = pas.SwitchParams()
ISO = pas.iso
_DEFAULT = object()


def real_iso(minutes_ago=0):
    """main() stamps with the real wall clock, so history has to match it."""
    return (dt.datetime.now() - dt.timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds")


def node_stats(series, now=NOW):
    """{name: [oldest..newest delay-or-None]} -> decide_group node_stats."""
    nodes, delays = {}, {}
    for name, values in series.items():
        samples = []
        for index, ms in enumerate(values):
            ts = (now - dt.timedelta(minutes=len(values) - 1 - index)).isoformat(
                timespec="seconds")
            samples.append([ts, ms])
        nodes[name] = {"samples": samples, "fail_streak": 0, "last_ok_ts": None}
        delays[name] = values[-1]
    return pas.build_node_stats(nodes, delays)


def gstate(current=US1, since_min=240, cand=None, cand_streak=0, since_raw=_DEFAULT,
           now=NOW):
    since = ((now - dt.timedelta(minutes=since_min)).isoformat(timespec="seconds")
             if since_raw is _DEFAULT else since_raw)
    return {
        "current": current,
        "since": since,
        "cand": cand,
        "cand_streak": cand_streak,
        "last_switch_ts": None,
        "last_switch_reason": None,
        "switch_ts_24h": [],
    }


ALIVE_EVERYWHERE = {
    US1: [1500, 1500, 1500],
    US2: [1000, 1010, 990],
    US3: [2000, 2000, 2000],
    SG1: [800, 800, 800],
    SG2: [1400, 1400, 1400],
    JP1: [700, 700, 700],
}


def with_overrides(**overrides):
    series = {name: list(values) for name, values in ALIVE_EVERYWHERE.items()}
    series.update(overrides)
    return series


# (name, group, current, members, series, group_state kwargs, target, reason)
DECISION_CASES = [
    (
        "噪声内保持->不换(hold_margin)",
        "GPT", US1, MEMBERS,
        with_overrides(**{US1: [1500, 1480, 1520], US2: [1400, 1420, 1390]}),
        dict(), None, "hold_margin",
    ),
    (
        "margin+streak+dwell 全满足才换",
        "GPT", US1, MEMBERS,
        with_overrides(),
        dict(cand=US2, cand_streak=1), US2, "switch_margin",
    ),
    (
        "候选换人 streak 清零",
        "GPT", US1, MEMBERS,
        with_overrides(),
        dict(cand=US3, cand_streak=5), None, "hold_streak",
    ),
    (
        "驻留未满不换",
        "GPT", US1, MEMBERS,
        with_overrides(),
        dict(cand=US2, cand_streak=1, since_min=30), None, "hold_dwell",
    ),
    (
        "pct 门槛优先于 ms 门槛(3000ms 档需 750ms)",
        "GPT", US1, MEMBERS,
        with_overrides(**{US1: [3000, 3000, 3000], US2: [2400, 2400, 2400],
                          US3: [2800, 2800, 2800]}),
        dict(cand=US2, cand_streak=1), None, "hold_margin",
    ),
    (
        "候选本轮死掉 -> 不换",
        "GPT", US1, MEMBERS,
        with_overrides(**{US2: [1000, 1010, None], US3: [1450, 1450, 1450]}),
        dict(cand=US2, cand_streak=1), None, "hold_margin",
    ),
    (
        "当前节点本轮探测全失败 -> 强制切",
        "GPT", US1, MEMBERS,
        with_overrides(**{US1: [1500, 1500, None]}),
        dict(), US2, "switch_dead",
    ),
    (
        "当前节点不在 members -> 强制切",
        "GPT", "MUSE-AUTO", MEMBERS,
        with_overrides(),
        dict(), US2, "switch_missing",
    ),
    (
        "当前节点为 null -> 强制切",
        "GPT", None, MEMBERS,
        with_overrides(),
        dict(), US2, "switch_missing",
    ),
    (
        "当前节点为 DIRECT -> 强制切",
        "GPT", "DIRECT", MEMBERS,
        with_overrides(),
        dict(), US2, "switch_missing",
    ),
    (
        "强制切换仍守 A/B/C 档位优先级(US 慢也先选 US)",
        "GPT", US1, MEMBERS,
        with_overrides(**{US1: [1500, None, None], US2: [1900, 1900, 1900],
                          US3: [None, None, None]}),
        dict(), US2, "switch_dead",
    ),
    (
        "全组无存活但全局有 -> hold_no_alive_member",
        "GPT", US1, [US1, US3],
        with_overrides(**{US1: [1500, 1500, None], US3: [2000, 2000, None]}),
        dict(), None, "hold_no_alive_member",
    ),
    (
        "升档：US 只活了 1 轮 -> 不动",
        "GPT", SG1, [US1, SG1, JP1],
        with_overrides(**{SG1: [800, 800, 800], US1: [None, None, 1500]}),
        dict(current=SG1), None, "hold_cand_unstable",
    ),
    (
        "升档：US 连续 2 轮存活 -> 立即升",
        "GPT", SG1, [US1, SG1, JP1],
        with_overrides(**{SG1: [800, 800, 800], US1: [1510, 1500, 1500]}),
        dict(current=SG1), US1, "switch_upgrade",
    ),
    (
        "降档：US 全挂、当前 US 已死 -> 立即切 SG",
        "GPT", US1, MEMBERS,
        with_overrides(**{US1: [1500, None, None], US2: [None, None, None],
                          US3: [None, None, None]}),
        dict(), SG1, "switch_downgrade",
    ),
    (
        "MUSE：B/C 档（只剩 SG）不动",
        "MUSE", US1, [US1, SG1],
        with_overrides(**{US1: [1500, None, None]}),
        dict(), None, "hold_muse_frozen",
    ),
    (
        "MUSE：当前 US 死但组内有 US -> 同档强制换",
        "MUSE", US1, [US1, US2, SG1],
        with_overrides(**{US1: [1500, None, None]}),
        dict(), US2, "switch_dead",
    ),
    (
        "MUSE：同档 US 换优仍按门槛",
        "MUSE", US1, [US1, US2],
        with_overrides(**{US1: [1500, 1500, 1500], US2: [1000, 1010, 990]}),
        dict(cand=US2, cand_streak=1), US2, "switch_margin",
    ),
    (
        "MUSE：dead 且组内只剩 SG/其他 -> 冻结不动（不降档）",
        "MUSE", US1, [US1, SG1, JP1],
        with_overrides(**{US1: [1500, None, None]}),
        dict(), None, "hold_muse_frozen",
    ),
    (
        "MUSE：current 不在 members 且只剩 SG/其他 -> 强制恢复选最优",
        "MUSE", "MUSE-AUTO", [US1, SG1, JP1],
        with_overrides(**{US1: [1500, None, None]}),
        dict(current="MUSE-AUTO"), SG1, "switch_missing",
    ),
    (
        "MUSE：current 为 null 且只剩 SG -> 强制恢复选最优",
        "MUSE", None, [US1, SG1, JP1],
        with_overrides(**{US1: [1500, None, None], JP1: [None, None, None]}),
        dict(current=None), SG1, "switch_missing",
    ),
    (
        "MUSE：current 为 null 且组内全无存活 -> 无目标可切",
        "MUSE", None, [SG1, JP1],
        with_overrides(**{SG1: [800, None, None], JP1: [700, None, None]}),
        dict(current=None), None, "hold_no_alive_member",
    ),
    (
        "MUSE：活在 SG 档、组内有更快 SG -> 一律不动",
        "MUSE", SG1, [US1, SG1, SG2, JP1],
        with_overrides(**{US1: [1500, None, None], SG1: [1400, 1400, 1400],
                          SG2: [800, 800, 800]}),
        dict(current=SG1, cand=SG2, cand_streak=5), None, "hold_muse_frozen",
    ),
    (
        "MUSE：活在 SG 档、US 连续 2 轮存活 -> 仍允许升档回 US",
        "MUSE", SG1, [US1, SG1, SG2],
        with_overrides(**{SG1: [800, 800, 800], US1: [1510, 1500, 1500]}),
        dict(current=SG1), US1, "switch_upgrade",
    ),
    (
        "MUSE：current 是组外的 US 而组内只有 SG/其他 -> 强制切最优",
        "MUSE", US1, [SG1, JP1],
        with_overrides(),
        dict(), SG1, "switch_missing",
    ),
    (
        "MUSE：已在其他档且只剩 SG -> 不升不降",
        "MUSE", JP1, [JP1, SG1, US1],
        with_overrides(**{US1: [1500, None, None], JP1: [700, 700, 700]}),
        dict(current=JP1), None, "hold_ok",
    ),
    (
        "全探测失败 -> 冻结",
        "GPT", US1, MEMBERS,
        {name: [1000, 1000, None] for name in ALIVE_EVERYWHERE},
        dict(), None, "hold_probe_suspect",
    ),
    (
        "三组不互相跟随：US-FAST 组内无更快 US -> 保持",
        "US-FAST", US2, MEMBERS,
        with_overrides(),
        dict(current=US2), None, "hold_ok",
    ),
]


# (name, group, current, members, series, group_state kwargs, target, reason)
# -> all cases run through decide_group with no network at all.
class DecideGroupTests(unittest.TestCase):
    def test_regression_table(self):
        for name, group, current, members, series, gs_kwargs, target, reason in DECISION_CASES:
            with self.subTest(case=name):
                stats = node_stats(series)
                det = {}
                got_target, got_reason = pas.decide_group(
                    group, current, members, stats, gstate(**gs_kwargs), NOW, PARAMS, det,
                )
                self.assertEqual(got_target, target, name)
                self.assertEqual(got_reason, reason, name)
                self.assertIn(det["decision"], {"hold", "switch", "freeze"})
                for field in ("cur_ms", "cur_smooth", "best", "best_ms", "best_smooth",
                              "need_margin_ms", "cand", "cand_streak", "dwell_min"):
                    self.assertIn(field, det)

    def test_muse_frozen_dead_flag_only_when_current_dead(self):
        stats = node_stats(with_overrides(**{US1: [1500, None, None]}))
        det = {}
        self.assertEqual(
            pas.decide_group("MUSE", US1, [US1, SG1], stats, gstate(), NOW, PARAMS, det),
            (None, "hold_muse_frozen"),
        )
        self.assertTrue(det["muse_frozen_dead"])

        det = {}
        pas.decide_group("MUSE", None, [US1, SG1], stats, gstate(), NOW, PARAMS, det)
        self.assertFalse(det["muse_frozen_dead"])

    def test_candidate_streak_grows_only_for_same_candidate(self):
        stats = node_stats(with_overrides())
        first = {}
        pas.decide_group("GPT", US1, MEMBERS, stats, gstate(), NOW, PARAMS, first)
        self.assertEqual((first["cand"], first["cand_streak"]), (US2, 1))
        self.assertEqual(first["reason"], "hold_streak")

        second = {}
        target, reason = pas.decide_group(
            "GPT", US1, MEMBERS, stats,
            gstate(cand=first["cand"], cand_streak=first["cand_streak"]), NOW, PARAMS, second,
        )
        self.assertEqual((second["cand"], second["cand_streak"]), (US2, 2))
        self.assertEqual((target, reason), (US2, "switch_margin"))

    def test_no_history_first_round_never_switches_for_margin(self):
        """A migrated state has no node history: only forced switches may fire."""
        stats = node_stats({US1: [1500], US2: [900]})
        det = {}
        target, reason = pas.decide_group(
            "GPT", US1, [US1, US2], stats, gstate(since_min=600), NOW, PARAMS, det,
        )
        self.assertIsNone(target)
        self.assertEqual(reason, "hold_cand_unstable")

    def test_need_margin_uses_max_of_ms_and_pct(self):
        self.assertEqual(pas.need_margin_ms(1000, PARAMS), 300)
        self.assertEqual(pas.need_margin_ms(3000, PARAMS), 750)
        self.assertIsNone(pas.need_margin_ms(None, PARAMS))


class SmoothingTests(unittest.TestCase):
    def test_median_of_last_five_valid_samples(self):
        node = {"samples": [[ISO(NOW - dt.timedelta(minutes=6 - i)), ms]
                            for i, ms in enumerate([1, 2, 3, 4, 5, 6])]}
        self.assertEqual(pas.valid_samples(node, NOW), [2, 3, 4, 5, 6])
        self.assertEqual(pas.smoothed_ms(node, NOW), 4)
        self.assertEqual(pas.smoothed_ms({"samples": []}, NOW), None)

    def test_old_samples_dropped_and_nulls_ignored(self):
        node = {"samples": [
            [ISO(NOW - dt.timedelta(hours=7)), 100],
            [ISO(NOW - dt.timedelta(minutes=5)), None],
            [ISO(NOW - dt.timedelta(minutes=4)), 1200],
            [ISO(NOW - dt.timedelta(minutes=3)), 1300],
        ]}
        self.assertEqual(pas.valid_samples(node, NOW), [1200, 1300])
        self.assertEqual(pas.smoothed_ms(node, NOW), 1250)

    def test_smoothed_falls_back_to_this_round_delay(self):
        stats = node_stats({US1: [None]})
        self.assertEqual(stats[US1]["delay"], None)
        stats = node_stats({US1: [1500]})
        self.assertEqual(pas.smoothed_of(US1, stats, NOW), 1500)

    def test_pick_best_ties_break_by_name(self):
        stats = node_stats({US2: [1000, 1000], US1: [1000, 1000]})
        self.assertEqual(pas.pick_best([US2, US1], stats, NOW), US1)


class MigrateStateTests(unittest.TestCase):
    def test_v1_state_migrates(self):
        v1 = {
            "mode": "A",
            "selected": {"GPT": US3, "MUSE": US1},
            "planned": {"GPT": US3},
            "alive_us": 3, "alive_sg": 1, "alive_any": 4,
            "alerted_mode": "A_recovered",
            "updated": ISO(NOW - dt.timedelta(minutes=45)),
        }
        state = pas.migrate_state(v1, {"GPT": US1, "MUSE": US2, "US-FAST": US3}, NOW)
        self.assertEqual(state["version"], pas.STATE_VERSION)
        self.assertEqual(state["groups"]["GPT"]["current"], US3)
        self.assertEqual(state["groups"]["MUSE"]["current"], US1)
        self.assertEqual(state["groups"]["US-FAST"]["current"], US3)
        self.assertEqual(state["groups"]["GPT"]["since"], v1["updated"])
        self.assertEqual(state["groups"]["GPT"]["cand_streak"], 0)
        self.assertEqual(state["nodes"], {})
        self.assertEqual(state["all_dead_streak"], 0)
        self.assertEqual(state["alerted_mode"], "A_recovered")
        self.assertLessEqual(pas.dwell_minutes(state["groups"]["GPT"], NOW), 46)

    def test_v1_without_selected_uses_controller_now(self):
        state = pas.migrate_state({}, {"GPT": US2, "MUSE": US1, "US-FAST": US1}, NOW)
        self.assertEqual(state["groups"]["GPT"]["current"], US2)
        self.assertEqual(state["groups"]["GPT"]["since"], ISO(NOW))

    def test_corrupt_state_is_cold_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json at all")
            self.assertEqual(pas.load_state(path), {})
            state = pas.migrate_state(pas.load_state(path), {"GPT": US1, "MUSE": US2,
                                                             "US-FAST": US3}, NOW)
            self.assertEqual(state["version"], pas.STATE_VERSION)
            self.assertEqual(state["groups"]["MUSE"]["current"], US2)
            self.assertEqual(state["nodes"], {})

    def test_v2_state_keeps_history(self):
        v2 = {
            "version": 2,
            "groups": {
                "GPT": {"current": US1, "since": ISO(NOW), "cand": US2, "cand_streak": 1,
                        "last_switch_ts": ISO(NOW), "last_switch_reason": "switch_dead",
                        "switch_ts_24h": [ISO(NOW)]},
            },
            "nodes": {US1: {"samples": [[ISO(NOW), 1200]], "fail_streak": 0,
                            "last_ok_ts": ISO(NOW)}},
            "all_dead_streak": 1,
        }
        state = pas.migrate_state(v2, {"GPT": US1, "MUSE": US1, "US-FAST": US1}, NOW)
        self.assertEqual(state["groups"]["GPT"]["current"], US1)
        self.assertEqual(state["groups"]["GPT"]["cand_streak"], 1)
        self.assertEqual(state["groups"]["GPT"]["switch_ts_24h"], [ISO(NOW)])
        self.assertEqual(state["groups"]["MUSE"]["current"], US1)
        self.assertIn(US1, state["nodes"])
        self.assertEqual(state["all_dead_streak"], 1)

    def test_controller_now_mismatch_resets_dwell_and_streak(self):
        """An external manual switch must not inherit the old since/cand."""
        v2 = {
            "groups": {
                "GPT": {"current": US1, "since": ISO(NOW - dt.timedelta(hours=6)),
                        "cand": US2, "cand_streak": 5,
                        "switch_ts_24h": [ISO(NOW - dt.timedelta(hours=1))]},
            },
        }
        state = pas.migrate_state(v2, {"GPT": US3, "MUSE": US1, "US-FAST": US1}, NOW)
        gpt = state["groups"]["GPT"]
        self.assertEqual(gpt["current"], US3)
        self.assertEqual(gpt["since"], ISO(NOW))
        self.assertIsNone(gpt["cand"])
        self.assertEqual(gpt["cand_streak"], 0)
        self.assertEqual(gpt["switch_ts_24h"], [ISO(NOW - dt.timedelta(hours=1))])

    def test_bad_since_resets_to_now(self):
        for bad in (None, "", "not-a-time", 12345, "2026-13-45T99:00:00"):
            with self.subTest(since=bad):
                state = pas.migrate_state(
                    {"groups": {"GPT": {"current": US1, "since": bad}}},
                    {"GPT": US1, "MUSE": US1, "US-FAST": US1}, NOW)
                self.assertEqual(state["groups"]["GPT"]["since"], ISO(NOW))
                self.assertEqual(pas.dwell_minutes(state["groups"]["GPT"], NOW), 0)

    def test_wrong_typed_fields_degrade_per_field(self):
        """Valid JSON with the wrong types must not raise and must not crash."""
        bad = {
            "mode": 5,
            "alive_us": "many", "alive_sg": None, "alive_any": ["x"],
            "all_dead_streak": -3, "round_ts": 12, "updated": "whenever",
            "selected": [US1], "planned": "nope", "groups": "nope", "nodes": 7,
            "alerted_mode": 42,
        }
        state = pas.migrate_state(bad, {"GPT": US1, "MUSE": US2, "US-FAST": US3}, NOW)
        self.assertEqual(state["version"], pas.STATE_VERSION)
        self.assertIsNone(state["mode"])
        self.assertEqual(
            (state["alive_us"], state["alive_sg"], state["alive_any"]), (0, 0, 0))
        self.assertEqual(state["all_dead_streak"], 0)
        self.assertIsNone(state["alerted_mode"])
        self.assertEqual(state["selected"], {})
        self.assertEqual(state["planned"], {})
        self.assertEqual(state["nodes"], {})
        self.assertEqual(state["groups"]["GPT"], {
            "current": US1, "since": ISO(NOW), "cand": None, "cand_streak": 0,
            "last_switch_ts": None, "last_switch_reason": None, "switch_ts_24h": [],
        })

    def test_wrong_typed_group_entries_degrade_per_field(self):
        bad = {
            "groups": {
                "GPT": {"current": 7, "since": ISO(NOW), "cand": ["US"],
                        "cand_streak": "lots", "last_switch_ts": "sometime",
                        "switch_ts_24h": "2026-11-01T11:00:00"},
                "MUSE": [US1],
            },
            "nodes": {US1: {"samples": "1500"}, US2: "nope", 3: {"samples": []}},
        }
        state = pas.migrate_state(bad, {"GPT": US1, "MUSE": US2, "US-FAST": US3}, NOW)
        self.assertEqual(state["groups"]["GPT"]["current"], US1)
        self.assertEqual(state["groups"]["GPT"]["since"], ISO(NOW))
        self.assertIsNone(state["groups"]["GPT"]["cand"])
        self.assertEqual(state["groups"]["GPT"]["cand_streak"], 0)
        self.assertIsNone(state["groups"]["GPT"]["last_switch_ts"])
        self.assertEqual(state["groups"]["GPT"]["switch_ts_24h"], [])
        self.assertEqual(state["groups"]["MUSE"]["current"], US2)
        self.assertEqual(state["nodes"], {US1: {"samples": "1500"}})
        # The unusable node history is dropped by the next append, not by a raise.
        nodes = pas.update_nodes(state["nodes"], {US1: 1000}, ISO(NOW), NOW)
        self.assertEqual(nodes[US1]["samples"], [[ISO(NOW), 1000]])

    def test_tz_aware_timestamps_are_normalised_not_trusted(self):
        """A tz-aware stamp must never be compared against the naive wall clock."""
        aware = NOW.astimezone().isoformat(timespec="seconds")
        self.assertEqual(pas.parse_ts(aware), NOW)
        self.assertIsNone(pas.parse_ts(aware).tzinfo)
        self.assertEqual(pas.parse_ts("2026-11-01T04:00:00Z"),
                         pas.parse_ts("2026-11-01T04:00:00+00:00"))
        self.assertEqual(pas.recent_ts([aware, aware[:-6] + "+09:00", 5, "bad"], NOW, 24)[0],
                         aware)
        state = pas.migrate_state(
            {"groups": {"GPT": {"current": US1, "since": aware, "cand": US2,
                                "cand_streak": 1, "switch_ts_24h": [aware]}},
             "updated": aware},
            {"GPT": US1, "MUSE": US1, "US-FAST": US1}, NOW)
        gpt = state["groups"]["GPT"]
        self.assertEqual(gpt["since"], aware)
        self.assertEqual(gpt["switch_ts_24h"], [aware])
        self.assertEqual(pas.dwell_minutes(gpt, NOW), 0)

    def test_decide_group_survives_garbage_state(self):
        """No probe result may turn a broken state field into a crash."""
        stats = node_stats(with_overrides())
        for bad_state in (
            {"since": "2026-11-01T12:00:00+08:00", "current": US1, "cand_streak": "x"},
            {"since": 9999, "cand": None, "cand_streak": None},
            {}, None,
        ):
            with self.subTest(state=bad_state):
                target, reason = pas.decide_group(
                    "GPT", US1, MEMBERS, stats, bad_state, NOW, PARAMS, {})
                self.assertIn(reason, {
                    "hold_ok", "hold_margin", "hold_streak", "hold_dwell",
                    "hold_cand_unstable", "switch_margin", "switch_dead"})
                self.assertTrue(target is None or isinstance(target, str))

    def test_unknown_since_is_not_a_full_dwell(self):
        """A missing/unparseable `since` must hold, only forced switches may cut."""
        stats = node_stats(with_overrides(**{US1: [3000, 3000, 3000],
                                             US2: [1000, 1000, 1000]}))
        for bad in ("", "garbage", None, 12345, [ISO(NOW)]):
            with self.subTest(since=bad):
                det = {}
                target, reason = pas.decide_group(
                    "GPT", US1, MEMBERS, stats,
                    gstate(cand=US2, cand_streak=5, since_raw=bad), NOW, PARAMS, det)
                self.assertIsNone(target)
                self.assertEqual(reason, "hold_dwell")
                self.assertIsNone(det["dwell_min"])


class NodeHistoryTests(unittest.TestCase):
    def test_one_sample_per_round_and_cap(self):
        nodes = {}
        for i in range(10):
            when = NOW + dt.timedelta(minutes=i)
            nodes = pas.update_nodes(nodes, {US1: 1000 + i}, ISO(when), when)
        samples = nodes[US1]["samples"]
        self.assertEqual(len(samples), pas.SAMPLE_KEEP)
        self.assertEqual([ms for _ts, ms in samples][-2:], [1008, 1009])

    def test_retry_replaces_same_round_sample(self):
        ts = ISO(NOW)
        nodes = pas.update_nodes({}, {US1: None}, ts, NOW)
        self.assertEqual(nodes[US1]["samples"], [[ts, None]])
        self.assertEqual(nodes[US1]["fail_streak"], 1)
        nodes = pas.update_nodes(nodes, {US1: 1400}, ts, NOW)
        self.assertEqual(nodes[US1]["samples"], [[ts, 1400]])
        self.assertEqual(nodes[US1]["fail_streak"], 0)
        self.assertEqual(nodes[US1]["last_ok_ts"], ts)

    def test_nodes_unseen_for_24h_are_pruned(self):
        old = ISO(NOW - dt.timedelta(hours=25))
        nodes = {
            "gone": {"samples": [[old, 1000]], "fail_streak": 0, "last_ok_ts": old},
            US1: {"samples": [[old, 1000]], "fail_streak": 0, "last_ok_ts": old},
        }
        pruned = pas.update_nodes(nodes, {US1: 1100}, ISO(NOW), NOW)
        self.assertNotIn("gone", pruned)
        self.assertEqual(len(pruned[US1]["samples"]), 2)


class AdvanceGroupStateTests(unittest.TestCase):
    def test_real_switch_resets_since_and_records_reason(self):
        state = pas.advance_group_state(gstate(current=US1), US2, NOW, switch_reason="switch_dead")
        self.assertEqual(state["current"], US2)
        self.assertEqual(state["since"], ISO(NOW))
        self.assertEqual(state["last_switch_ts"], ISO(NOW))
        self.assertEqual(state["last_switch_reason"], "switch_dead")
        self.assertEqual(state["switch_ts_24h"], [ISO(NOW)])
        self.assertIsNone(state["cand"])

    def test_external_switch_is_visible(self):
        state = pas.advance_group_state(gstate(current=US1), "手动选的", NOW)
        self.assertEqual(state["last_switch_reason"], "external")

    def test_candidate_without_switch_accumulates(self):
        base = gstate(cand=US2, cand_streak=1)
        state = pas.advance_group_state(base, US1, NOW, proposed=US2)
        self.assertEqual((state["cand"], state["cand_streak"]), (US2, 2))
        self.assertEqual(state["current"], US1)
        state = pas.advance_group_state(base, US1, NOW, proposed=US3)
        self.assertEqual((state["cand"], state["cand_streak"]), (US3, 1))
        state = pas.advance_group_state(base, US1, NOW, proposed=None)
        self.assertEqual((state["cand"], state["cand_streak"]), (None, 0))

    def test_switch_ts_24h_drops_entries_older_than_24h(self):
        base = gstate(current=US1)
        base["switch_ts_24h"] = [ISO(NOW - dt.timedelta(hours=30)), ISO(NOW - dt.timedelta(hours=2))]
        state = pas.advance_group_state(base, US2, NOW)
        self.assertEqual(len(state["switch_ts_24h"]), 2)
        self.assertNotIn(ISO(NOW - dt.timedelta(hours=30)), state["switch_ts_24h"])


class MainHarness(unittest.TestCase):
    """Runs main() against stubbed controller/probe calls and temp paths."""

    PROXIES = {
        "GPT": {"type": "selector", "now": US1,
                "all": [US1, US2, US3, SG1, JP1, "DIRECT"]},
        "MUSE": {"type": "selector", "now": US1, "all": [US1, "MUSE-AUTO", SG1]},
        "MUSE-AUTO": {"type": "selector", "now": US1, "all": [US1, SG1]},
        "US-FAST": {"type": "selector", "now": US2, "all": [US1, US2, US3, SG1]},
        US1: {"type": "ss", "history": []},
        US2: {"type": "ss", "history": []},
        US3: {"type": "ss", "history": []},
        SG1: {"type": "ss", "history": []},
        JP1: {"type": "ss", "history": []},
    }
    DELAYS = {US1: 1500, US2: 1400, US3: 1200, SG1: 900, JP1: 800}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {
            name: os.path.join(self.tmp.name, filename)
            for name, filename in (
                ("state", "state.json"), ("log", "proxy-failover.log"),
                ("rounds", "rounds.jsonl"), ("alert", "alert.log"),
                ("target", "alert-target.json"), ("env", "env.txt"),
                ("secret", "secret.env"),
            )
        }
        self.puts = []

    def write_state(self, payload):
        if isinstance(payload, str):
            raw = payload
        else:
            raw = json.dumps(payload, ensure_ascii=False)
        with open(self.paths["state"], "w", encoding="utf-8") as handle:
            handle.write(raw)

    def read_state(self):
        with open(self.paths["state"], encoding="utf-8") as handle:
            return json.load(handle)

    def read_rounds(self):
        with open(self.paths["rounds"], encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def fake_api_json(self, method, url, body=None, token=None, timeout=15):
        if method == "PUT":
            group = url.rsplit("/", 1)[-1]
            self.puts.append((group, (body or {}).get("name")))
            return {}
        return {"proxies": self.PROXIES}

    def run_main(self, argv=(), delays=None, decision_mode=None, state=None, probe=None):
        if state is not None:
            self.write_state(state)
        delays = delays if delays is not None else self.DELAYS
        if probe is None:
            probe = lambda api, name, *rest, **kw: delays.get(name)
        argv = list(argv)
        if decision_mode:
            argv += ["--decision-mode", decision_mode]
        args = [
            "--controller", "http://127.0.0.1:9",
            "--state-file", self.paths["state"],
            "--log-file", self.paths["log"],
            "--rounds-jsonl", self.paths["rounds"],
            "--alert-log", self.paths["alert"],
            "--alert-target", self.paths["target"],
            "--env-file", self.paths["env"],
            "--secret-env", self.paths["secret"],
        ] + argv
        stdout = io.StringIO()
        with patch.object(pas, "api_json", self.fake_api_json), \
                patch.object(pas, "request_delay", probe), \
                patch.object(pas.time, "sleep", lambda _s: None), \
                contextlib.redirect_stdout(stdout):
            code = pas.main(args)
        return code, stdout.getvalue()

    def log_text(self):
        try:
            with open(self.paths["log"], encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""


class LegacyModeTests(MainHarness):
    def test_legacy_matches_plan_switches_and_state_shape(self):
        groups = {g: self.PROXIES[g] for g in pas.SWITCH_GROUPS}
        expected = pas.plan_switches("A", groups, self.DELAYS,
                                     {US1, US2, US3}, {SG1}, {US1, US2, US3, SG1, JP1})
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.puts, [
            (g, target) for g, target in expected.items()
            if target and target != self.PROXIES[g]["now"]
        ])
        self.assertEqual(sorted(self.puts), [("GPT", US3), ("US-FAST", US3)])
        state = self.read_state()
        self.assertEqual(
            sorted(state),
            ["alerted_mode", "alive_any", "alive_sg", "alive_us", "mode", "planned",
             "selected", "updated"],
        )
        self.assertNotIn("version", state)
        self.assertEqual(state["planned"], {g: (expected[g] or None) for g in pas.SWITCH_GROUPS})
        self.assertIn("PLAN\tGPT\t%s\t->\t%s\tdelay=1200" % (US1, US3), out)
        self.assertIn("PLAN\tMUSE\talready\t%s" % US1, out)
        self.assertIn("RESULT\tmode=A\talive_us=3\talive_sg=1\talive_any=5", out)
        self.assertFalse(os.path.exists(self.paths["rounds"]))

    def test_default_cli_is_legacy(self):
        self.assertEqual(pas.parse_args([]).decision_mode, "legacy")
        self.assertEqual(pas.parse_args(["--legacy-fastest"]).decision_mode, "legacy")
        self.assertEqual(pas.parse_args(["--decision-mode", "hysteresis",
                                         "--legacy-fastest"]).decision_mode, "legacy")

    def test_legacy_cli_defaults(self):
        args = pas.parse_args([])
        self.assertEqual(
            (args.switch_margin_ms, args.switch_margin_pct, args.switch_streak,
             args.min_dwell_min, args.retry_current),
            (300, 25, 2, 90, 2),
        )
        self.assertEqual(args.rounds_jsonl, pas.DEFAULT_ROUNDS_JSONL)

    def test_legacy_fastest_alias_runs_old_behaviour(self):
        code, _out = self.run_main(["--legacy-fastest"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.puts), [("GPT", US3), ("US-FAST", US3)])
        self.assertNotIn("version", self.read_state())


class ShadowModeTests(MainHarness):
    def test_shadow_executes_legacy_but_records_new_decision(self):
        code, _out = self.run_main(decision_mode="shadow")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.puts), [("GPT", US3), ("US-FAST", US3)])
        rows = self.read_rounds()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["group"] for row in rows}, set(pas.SWITCH_GROUPS))
        for row in rows:
            self.assertEqual(row["exec_mode"], "shadow")
            self.assertIn(row["decision"], {"hold", "switch", "freeze"})
        state = self.read_state()
        self.assertEqual(state["version"], pas.STATE_VERSION)
        # Execution followed the legacy plan (fastest US), MUSE untouched.
        self.assertEqual(state["planned"], {"GPT": US3, "MUSE": US1, "US-FAST": US3})
        self.assertEqual(state["selected"], {"GPT": US1, "MUSE": US1, "US-FAST": US2})
        self.assertIn(US1, state["nodes"])
        # The new decision was only recorded: 300ms of gain is inside the margin.
        self.assertEqual(state["decisions"]["GPT"], {"target": None, "reason": "hold_margin"})
        self.assertEqual(rows[0]["executed"], US3)

    def test_hysteresis_dry_run_writes_nothing(self):
        code, out = self.run_main(["--dry-run"], decision_mode="hysteresis")
        self.assertEqual(code, 0)
        self.assertEqual(self.puts, [])
        self.assertFalse(os.path.exists(self.paths["state"]))
        self.assertFalse(os.path.exists(self.paths["rounds"]))
        self.assertFalse(os.path.exists(self.paths["alert"]))
        # Nothing is fast enough to clear the margin in a first round: no PUT line.
        self.assertIn("PLAN\tGPT\tunchanged\tnow=%s" % US1, out)
        self.assertNotIn("DRY-RUN\t", out)

    def test_hysteresis_dry_run_executes_forced_switch(self):
        delays = dict(self.DELAYS)
        for name in (US1, US2, US3):
            delays[name] = None
        code, out = self.run_main(["--dry-run"], decision_mode="hysteresis", delays=delays)
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN\tGPT\t->\t%s" % SG1, out)
        self.assertIn("DRY-RUN\tUS-FAST\t->\t%s" % SG1, out)
        self.assertNotIn("DRY-RUN\tMUSE", out)
        self.assertFalse(os.path.exists(self.paths["state"]))
        self.assertFalse(os.path.exists(self.paths["rounds"]))

    def test_hysteresis_dry_run_reports_reason(self):
        _code, out = self.run_main(["--dry-run"], decision_mode="hysteresis")
        self.assertIn("decide group=MUSE", self.log_text())
        self.assertIn("reason=", self.log_text())
        self.assertIn("RESULT\tmode=A", out)

    def test_hysteresis_writes_version_2_state(self):
        code, _out = self.run_main(decision_mode="hysteresis")
        self.assertEqual(code, 0)
        state = self.read_state()
        for key in ("version", "round_ts", "all_dead_streak", "groups", "nodes",
                    "mode", "selected", "planned", "alive_us", "alive_sg", "alive_any",
                    "alerted_mode", "updated"):
            self.assertIn(key, state)
        for group in pas.SWITCH_GROUPS:
            entry = state["groups"][group]
            for key in ("current", "since", "cand", "cand_streak", "last_switch_ts",
                        "last_switch_reason", "switch_ts_24h"):
                self.assertIn(key, entry)


class ProbeSuspectTests(MainHarness):
    def test_first_all_dead_round_freezes_then_alerts(self):
        dead = {name: None for name in self.DELAYS}
        _code, _out = self.run_main(decision_mode="hysteresis", delays=dead)
        self.assertEqual(self.puts, [])
        self.assertFalse(os.path.exists(self.paths["alert"]))
        state = self.read_state()
        self.assertEqual(state["all_dead_streak"], 1)

        _code, _out = self.run_main(decision_mode="hysteresis", delays=dead,
                                    state=state)
        self.assertEqual(self.read_state()["all_dead_streak"], 2)
        self.assertTrue(os.path.exists(self.paths["alert"]))
        with open(self.paths["alert"], encoding="utf-8") as handle:
            self.assertIn("美国+新加坡全挂", handle.read())

    def test_recovered_round_resets_streak(self):
        state = {"version": 2, "mode": "C", "all_dead_streak": 3,
                 "selected": {}, "alerted_mode": "C"}
        _code, _out = self.run_main(decision_mode="hysteresis", state=state)
        self.assertEqual(self.read_state()["all_dead_streak"], 0)


class CurrentRetryTests(MainHarness):
    def test_dead_current_is_reprobed_before_forced_switch(self):
        delays = dict(self.DELAYS)
        delays[US1] = None
        hits = {"n": 0}

        def probe(api, name, *rest, **kw):
            if name == US1:
                hits["n"] += 1
                return 1450 if hits["n"] > 1 else None
            return delays.get(name)

        code, _out = self.run_main(decision_mode="hysteresis", delays=delays, probe=probe)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(hits["n"], 2)
        self.assertIn("retry-current", self.log_text())
        # A single timeout must not push MUSE down to Singapore.
        self.assertEqual(self.puts, [])
        rows = {row["group"]: row for row in self.read_rounds()}
        self.assertNotEqual(rows["MUSE"]["reason"], "switch_downgrade")
        self.assertEqual(rows["MUSE"]["executed"], None)

    def test_dead_current_forces_muse_only_when_retries_also_fail(self):
        delays = dict(self.DELAYS)
        delays[US1] = None
        code, _out = self.run_main(decision_mode="hysteresis", delays=delays)
        self.assertEqual(code, 0)
        self.assertEqual(self.puts, [("GPT", US3)])
        rows = {row["group"]: row for row in self.read_rounds()}
        self.assertEqual(rows["MUSE"]["reason"], "hold_muse_frozen")
        self.assertIn("muse_frozen_dead", self.log_text())
        self.assertIn("still_dead", self.log_text())

    def test_legacy_never_reprobes(self):
        delays = dict(self.DELAYS)
        delays[US1] = None
        hits = {"n": 0}

        def probe(api, name, *rest, **kw):
            if name == US1:
                hits["n"] += 1
            return delays.get(name)

        code, _out = self.run_main(decision_mode="legacy", delays=delays, probe=probe)
        self.assertEqual(code, 0)
        self.assertEqual(hits["n"], 1)
        self.assertNotIn("retry-current", self.log_text())
        # Legacy mode C-branch: MUSE stays, GPT/US-FAST take the fastest US.
        self.assertEqual(sorted(self.puts), [("GPT", US3), ("US-FAST", US3)])


class RetryReclassifiesRoundTests(MainHarness):
    """Design 2.2/3.5: the round is classified from the post-retry probe."""

    def probe_us1_recovers(self, api, name, *rest, **kw):
        """Everything times out on the first pass; only US1 comes back on retry."""
        if name == US1:
            self.hits["us1"] = self.hits.get("us1", 0) + 1
            return 1400 if self.hits["us1"] > 1 else None
        return None

    def setUp(self):
        super().setUp()
        self.hits = {}

    def test_retry_recovered_node_is_not_counted_as_all_dead(self):
        dead = {name: None for name in self.DELAYS}
        code, out = self.run_main(decision_mode="hysteresis", delays=dead,
                                  probe=self.probe_us1_recovers)
        self.assertEqual(code, 0)
        self.assertIn("RESULT\tmode=A\talive_us=1\talive_sg=0\talive_any=1", out)
        state = self.read_state()
        self.assertEqual(state["mode"], "A")
        self.assertEqual(state["all_dead_streak"], 0)
        self.assertEqual(state["alive_any"], 1)
        self.assertFalse(os.path.exists(self.paths["alert"]))
        rows = {row["group"]: row for row in self.read_rounds()}
        self.assertEqual(rows["GPT"]["alive_any"], 1)
        self.assertEqual(rows["GPT"]["mode"], "A")
        # The current US node is alive after the retry, so nothing is forced.
        self.assertEqual(rows["GPT"]["reason"], "hold_ok")
        self.assertEqual(rows["MUSE"]["reason"], "hold_ok")
        self.assertEqual(self.puts, [("US-FAST", US1)])

    def test_two_recovered_rounds_still_never_alert(self):
        dead = {name: None for name in self.DELAYS}
        _code, _out = self.run_main(decision_mode="hysteresis", delays=dead,
                                    probe=self.probe_us1_recovers)
        _code, _out = self.run_main(decision_mode="hysteresis", delays=dead,
                                    probe=self.probe_us1_recovers,
                                    state=self.read_state())
        self.assertEqual(self.read_state()["all_dead_streak"], 0)
        self.assertFalse(os.path.exists(self.paths["alert"]))

    def test_genuinely_all_dead_round_still_counts(self):
        dead = {name: None for name in self.DELAYS}
        _code, _out = self.run_main(decision_mode="hysteresis", delays=dead)
        state = self.read_state()
        self.assertEqual(state["mode"], "C")
        self.assertEqual(state["all_dead_streak"], 1)


class MuseFrozenModeTests(MainHarness):
    """Review item 2: in B/C the MUSE group must not move at all."""

    def muse_on_sg_proxies(self):
        leaf = {"type": "ss", "history": []}
        return {
            **MainHarness.PROXIES,
            "MUSE": {"type": "selector", "now": SG1, "all": [US1, SG1, SG2]},
            SG2: dict(leaf),
        }

    def test_muse_on_sg_does_not_trade_within_tier(self):
        self.PROXIES = self.muse_on_sg_proxies()
        delays = {US1: None, US2: None, US3: None, JP1: None, SG1: 1500, SG2: 800}
        wide = real_iso(120)
        since = real_iso(150)
        state = {
            "version": 2, "mode": "B", "alerted_mode": "B",
            "groups": {"MUSE": {"current": SG1, "since": since, "cand": SG2,
                                "cand_streak": 5, "switch_ts_24h": []}},
            "nodes": {SG1: {"samples": [[wide, 1500], [since, 1500]], "fail_streak": 0,
                            "last_ok_ts": since},
                      SG2: {"samples": [[wide, 800], [since, 800]], "fail_streak": 0,
                            "last_ok_ts": since}},
        }
        code, _out = self.run_main(decision_mode="hysteresis", delays=delays, state=state)
        self.assertEqual(code, 0)
        # margin/streak/dwell would all allow the switch: MUSE still must not move.
        rows = {row["group"]: row for row in self.read_rounds()}
        self.assertEqual(rows["MUSE"]["reason"], "hold_muse_frozen")
        self.assertNotIn(("MUSE", SG2), self.puts)
        self.assertEqual(self.puts, [("GPT", SG1), ("US-FAST", SG1)])
        written = self.read_state()
        # The stale same-tier candidate is cleared: frozen MUSE cannot bank it.
        self.assertIsNone(written["groups"]["MUSE"]["cand"])
        self.assertEqual(written["groups"]["MUSE"]["cand_streak"], 0)

    def test_muse_missing_current_recovers_to_best_available(self):
        leaf = {"type": "ss", "history": []}
        self.PROXIES = {
            **MainHarness.PROXIES,
            "MUSE": {"type": "selector", "now": "MUSE-AUTO", "all": [US1, SG1]},
            SG2: dict(leaf),
        }
        delays = {US1: None, US2: None, US3: None, JP1: None, SG1: 1500, SG2: 800}
        code, _out = self.run_main(decision_mode="hysteresis", delays=delays)
        self.assertEqual(code, 0)
        rows = {row["group"]: row for row in self.read_rounds()}
        self.assertEqual(rows["MUSE"]["reason"], "switch_missing")
        self.assertIn(("MUSE", SG1), self.puts)


class GroupStateAlignmentTests(MainHarness):
    """Review item 4: reconcile with the controller before deciding."""

    def test_external_manual_switch_resets_streak_and_dwell(self):
        self.PROXIES = {**MainHarness.PROXIES,
                        "GPT": {"type": "selector", "now": US2,
                                "all": [US1, US2, US3, SG1, JP1, "DIRECT"]}}
        delays = {US1: 1500, US2: 2000, US3: 900, SG1: 1900, JP1: 1800}
        wide = real_iso(120)
        since = real_iso(200)
        state = {
            "version": 2, "mode": "A",
            "groups": {"GPT": {"current": US1, "since": since, "cand": US3,
                               "cand_streak": 5, "switch_ts_24h": []}},
            "nodes": {US1: {"samples": [[wide, 1500], [since, 1500]]},
                      US2: {"samples": [[wide, 2000], [since, 2000]]},
                      US3: {"samples": [[wide, 900], [since, 900]]}},
        }
        code, _out = self.run_main(decision_mode="hysteresis", delays=delays, state=state)
        self.assertEqual(code, 0)
        rows = {row["group"]: row for row in self.read_rounds()}
        # The stale streak of 5 may not be inherited by a node nobody chose.
        self.assertEqual(rows["GPT"]["reason"], "hold_streak")
        self.assertEqual(rows["GPT"]["cand_streak"], 1)
        self.assertEqual(rows["GPT"]["dwell_min"], 0)
        self.assertNotIn("GPT", [group for group, _n in self.puts])
        written = self.read_state()
        self.assertEqual(written["groups"]["GPT"]["current"], US2)
        self.assertEqual(written["groups"]["GPT"]["cand_streak"], 1)

    def test_unparseable_since_blocks_margin_switch(self):
        delays = {US1: 2000, US2: 900, US3: 1500, SG1: 1900, JP1: 1800}
        wide = real_iso(120)
        state = {
            "version": 2, "mode": "A",
            "groups": {"GPT": {"current": US1, "since": "not-a-timestamp", "cand": US2,
                               "cand_streak": 5, "switch_ts_24h": "nope"}},
            "nodes": {US1: {"samples": [[wide, 2000], [ISO(NOW), 2000]]},
                      US2: {"samples": [[wide, 900], [ISO(NOW), 900]]}},
        }
        code, _out = self.run_main(decision_mode="hysteresis", delays=delays, state=state)
        self.assertEqual(code, 0)
        rows = {row["group"]: row for row in self.read_rounds()}
        self.assertEqual(rows["GPT"]["reason"], "hold_dwell")
        self.assertNotIn("GPT", [group for group, _n in self.puts])


class CorruptStateMainTests(MainHarness):
    """Review item 3: a well-formed JSON file with wrong types must not crash."""

    def test_hysteresis_survives_wrong_typed_state(self):
        bad = {
            "version": "two", "mode": 5, "alive_us": "many", "all_dead_streak": "three",
            "groups": ["GPT"], "nodes": 12, "selected": "US", "updated": "sometime",
            "alerted_mode": ["C"],
        }
        code, out = self.run_main(decision_mode="hysteresis", state=bad)
        self.assertEqual(code, 0)
        self.assertIn("RESULT\tmode=A", out)
        state = self.read_state()
        self.assertEqual(state["version"], pas.STATE_VERSION)
        self.assertEqual(state["all_dead_streak"], 0)
        for group in pas.SWITCH_GROUPS:
            self.assertEqual(state["groups"][group]["cand_streak"], 0)
            self.assertIsInstance(state["groups"][group]["switch_ts_24h"], list)

    def test_hysteresis_survives_wrong_typed_group_fields(self):
        bad = {
            "groups": {"GPT": {"current": US1, "since": 42, "cand": 7,
                               "cand_streak": "x", "switch_ts_24h": {"a": 1}}},
            "nodes": {US1: {"samples": 1500}, "junk": "x"},
        }
        code, _out = self.run_main(decision_mode="hysteresis", state=bad)
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertEqual(state["groups"]["GPT"]["cand_streak"], 0)
        self.assertIsNone(state["groups"]["GPT"]["cand"])
        self.assertEqual(state["groups"]["GPT"]["switch_ts_24h"], [])
        self.assertIsInstance(state["nodes"][US1]["samples"], list)
        self.assertNotIn("junk", state["nodes"])

    def test_save_state_uses_unique_temporary_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            leftover = path + ".tmp"
            with open(leftover, "w", encoding="utf-8") as handle:
                handle.write("someone-elses-partial-write")
            pas.save_state(path, {"version": 2})
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")),
                             {"version": 2})
            # A foreign file at the historic fixed name is neither reused nor eaten.
            self.assertEqual(Path(leftover).read_text(encoding="utf-8"),
                             "someone-elses-partial-write")
            self.assertEqual(sorted(os.listdir(tmp)), ["state.json", "state.json.tmp"])
            os.unlink(leftover)
            pas.save_state(path, {"version": 3})
            self.assertEqual(sorted(os.listdir(tmp)), ["state.json"])

class LegacyOracleTests(unittest.TestCase):
    """Review item 5: the legacy path must behave like the pre-debounce script.

    The oracle is the committed version of the script (`git show HEAD:...`).
    Both versions run against the same stub controller and the same probe
    results; PUT order, PLAN/RESULT/NOW stdout, the written state (minus
    `updated`) and the alert decision must match exactly.
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.abspath(os.path.join(HERE, os.pardir))
        proc = subprocess.run(
            ["git", "-C", root, "show", "HEAD:scripts/proxy-auto-switch.py"],
            capture_output=True,
        )
        if proc.returncode != 0:
            cls.oracle = None
            cls.skip_detail = proc.stderr.decode("utf-8", "replace")[:200]
            return
        cls._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls._tmp.name, "legacy_oracle.py")
        with open(path, "wb") as handle:
            handle.write(proc.stdout)
        spec = importlib.util.spec_from_file_location("legacy_proxy_auto_switch", path)
        cls._name = spec.name
        cls.oracle = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.oracle
        spec.loader.exec_module(cls.oracle)

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(getattr(cls, "_name", "legacy_proxy_auto_switch"), None)
        if getattr(cls, "_tmp", None) is not None:
            cls._tmp.cleanup()
            cls._tmp = None

    PROXIES = {
        "GPT": {"type": "selector", "now": US1, "all": [US1, US2, US3, SG1, JP1, "DIRECT"]},
        "MUSE": {"type": "selector", "now": US1, "all": [US1, SG1]},
        "US-FAST": {"type": "selector", "now": US2, "all": [US1, US2, US3, SG1]},
        US1: {"type": "ss", "history": []},
        US2: {"type": "ss", "history": []},
        US3: {"type": "ss", "history": []},
        SG1: {"type": "ss", "history": []},
        JP1: {"type": "ss", "history": []},
    }
    ALL_DELAYS = {US1: 1500, US2: 1400, US3: 1200, SG1: 900, JP1: 800}
    TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")

    def sandbox(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return {name: os.path.join(tmp.name, filename) for name, filename in (
            ("state", "state.json"), ("log", "proxy-failover.log"),
            ("alert", "alert.log"), ("env", "env.txt"), ("secret", "secret.env"),
            ("target", "alert-target.json"))}

    def run_once(self, module, paths, scenario):
        puts = []

        def fake_api_json(method, url, body=None, token=None, timeout=15):
            if scenario["controller_down"]:
                raise RuntimeError("connection refused")
            if method == "PUT":
                group = url.rsplit("/", 1)[-1]
                name = (body or {}).get("name")
                if group in scenario["put_fail"]:
                    raise RuntimeError("controller rejected")
                puts.append((group, name))
                if scenario["on_put"] is not None:
                    scenario["on_put"](paths, list(puts))
                return {}
            return {"proxies": copy.deepcopy(scenario["proxies"])}

        def probe(api, name, *rest, **kw):
            return scenario["delays"].get(name)

        if scenario["state"] is not None:
            raw = scenario["state"] if isinstance(scenario["state"], str) else \
                json.dumps(scenario["state"], ensure_ascii=False)
            with open(paths["state"], "w", encoding="utf-8") as handle:
                handle.write(raw)
        argv = [
            "--controller", "http://127.0.0.1:9",
            "--state-file", paths["state"],
            "--log-file", paths["log"],
            "--alert-log", paths["alert"],
            "--alert-target", paths["target"],
            "--env-file", paths["env"],
            "--secret-env", paths["secret"],
        ]
        if scenario["dry_run"]:
            argv.append("--dry-run")

        stdout = io.StringIO()
        patches = [patch.object(module, "api_json", fake_api_json),
                   patch.object(module, "request_delay", probe),
                   contextlib.redirect_stdout(stdout)]
        if getattr(module, "time", None) is not None:
            # The pre-debounce script has no retry sleep to stub out.
            patches.append(patch.object(module.time, "sleep", lambda _s: None))
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            code = module.main(argv)
        return code, stdout.getvalue(), puts, paths

    def normalize(self, result):
        code, out, puts, paths = result
        lines = [self.TS_RE.sub("<ts>", line) for line in out.splitlines()
                 if line.startswith(("PLAN", "RESULT", "NOW", "ERROR", "DRY-RUN"))]
        state = None
        if os.path.exists(paths["state"]):
            state = json.loads(Path(paths["state"]).read_text(encoding="utf-8"))
            state.pop("updated", None)
        alerts = []
        if os.path.exists(paths["alert"]):
            with open(paths["alert"], encoding="utf-8") as handle:
                alerts = [self.TS_RE.sub("<ts>", line.rstrip("\n")) for line in handle]
        rounds = os.path.exists(paths.get("rounds", os.path.join(":", "none")))
        return {"code": code, "stdout": lines, "puts": puts, "state": state,
                "alerts": alerts}

    def scenario(self, **overrides):
        base = {"proxies": self.PROXIES, "delays": self.ALL_DELAYS, "put_fail": set(),
                "state": None, "dry_run": False, "controller_down": False,
                "on_put": None}
        base.update(overrides)
        return base

    def compare(self, scenario):
        mine = self.normalize(self.run_once(pas, self.sandbox(), scenario))
        oracle = self.normalize(self.run_once(self.oracle, self.sandbox(), scenario))
        self.assertEqual(mine, oracle, json.dumps(
            {"new": mine, "old": oracle}, ensure_ascii=False, indent=2))
        return mine

    def test_legacy_matches_oracle(self):
        if self.oracle is None:
            self.skipTest("git HEAD script unavailable")
        us_dead = {**self.ALL_DELAYS, US1: None, US2: None, US3: None}
        sg_dead = {**us_dead, SG1: None}
        cases = {
            "mode A 全存活": self.scenario(),
            "mode A 上一轮 C -> 恢复告警": self.scenario(
                state={"mode": "C", "alerted_mode": "C", "selected": {}}),
            "mode A 已告警恢复 -> 不重复": self.scenario(
                state={"mode": "C", "alerted_mode": "A_recovered"}),
            "mode B 美国全挂": self.scenario(delays=us_dead),
            "mode B 已告警 -> 不重复": self.scenario(
                delays=us_dead, state={"mode": "B", "alerted_mode": "B"}),
            "mode B 但上一轮 state 类型全错": self.scenario(
                delays=us_dead, state='{"mode": 7, "alerted_mode": [], "updated": '
                                      '"2026-11-01T12:00:00+08:00"}'),
            "mode C 只剩日本": self.scenario(delays=sg_dead),
            "mode C dry-run": self.scenario(delays=sg_dead, dry_run=True),
            "mode A 但 GPT PUT 失败": self.scenario(put_fail={"GPT"}),
            "mode B dry-run": self.scenario(delays=us_dead, dry_run=True),
            "controller 不可达": self.scenario(controller_down=True),
            "缺少 US-FAST 分组": self.scenario(
                proxies={k: v for k, v in self.PROXIES.items() if k != "US-FAST"}),
            "损坏 state 文件": self.scenario(state="{not json"),
            "旧版 selected 优先于控制器": self.scenario(
                state={"mode": "B", "selected": {"GPT": US1}, "updated": ISO(NOW)}),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                got = self.compare(case)
                if not case["controller_down"] and "缺少" not in name:
                    self.assertEqual(got["code"], 0)
                    if not case["dry_run"]:
                        self.assertIsNotNone(got["state"])

    def test_legacy_reads_state_after_switching(self):
        """The alert decision uses the state as it is after the switches."""
        if self.oracle is None:
            self.skipTest("git HEAD script unavailable")

        def mark_alerted(paths, puts):
            with open(paths["state"], "w", encoding="utf-8") as handle:
                json.dump({"mode": "B", "alerted_mode": "B"}, handle)

        us_dead = {**self.ALL_DELAYS, US1: None, US2: None, US3: None}
        got = self.compare(self.scenario(delays=us_dead, on_put=mark_alerted))
        self.assertEqual(got["alerts"], [])
        self.assertEqual(got["state"]["alerted_mode"], "B")

    def test_legacy_state_shape_is_still_version_1(self):
        if self.oracle is None:
            self.skipTest("git HEAD script unavailable")
        got = self.compare(self.scenario())
        self.assertEqual(
            sorted(got["state"]),
            ["alerted_mode", "alive_any", "alive_sg", "alive_us", "mode", "planned",
             "selected"],
        )

    def test_legacy_alert_decision_matches_oracle(self):
        if self.oracle is None:
            self.skipTest("git HEAD script unavailable")
        us_dead = {**self.ALL_DELAYS, US1: None, US2: None, US3: None}
        got = self.compare(self.scenario(delays=us_dead))
        self.assertEqual(len(got["alerts"]), 1)
        self.assertIn("美国全挂，已切新加坡", got["alerts"][0])


class LoggerTests(unittest.TestCase):
    def test_write_failure_is_visible_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pas.Logger(os.path.join(tmp, "sub", "proxy-failover.log"))
            boom = io.StringIO()
            with patch.object(Path, "open", side_effect=OSError("denied")), \
                    contextlib.redirect_stderr(boom):
                log.info("hello")
            text = boom.getvalue()
            self.assertIn("hello", text)
            self.assertIn("log write failed", text)
            self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO hello")

    def test_successful_write_is_timestamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxy-failover.log")
            pas.Logger(path).warn("careful")
            with open(path, encoding="utf-8") as handle:
                line = handle.read().strip()
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} WARN careful$")

    def test_plan_and_now_lines_are_logged(self):
        class Recording(pas.Logger):
            def __init__(self, *args):
                super().__init__(*args)
                self.lines = []

            def _write(self, level, message):
                self.lines.append(f"{level} {message}")

        with tempfile.TemporaryDirectory() as tmp:
            recorder = Recording(os.path.join(tmp, "unused.log"))
            recorder.info("plan group=GPT unchanged now=美国 01 reason=hold_ok")
        self.assertEqual(len(recorder.lines), 1)
        self.assertIn("reason=hold_ok", recorder.lines[0])


class RoundsRowTests(MainHarness):
    def test_row_has_design_fields(self):
        _code, _out = self.run_main(decision_mode="hysteresis")
        row = self.read_rounds()[0]
        for field in ("ts", "group", "mode", "current", "cur_ms", "cur_smooth", "best",
                      "best_ms", "best_smooth", "need_margin_ms", "cand", "cand_streak",
                      "dwell_min", "decision", "reason", "alive_us", "alive_sg",
                      "alive_any"):
            self.assertIn(field, row)
        self.assertNotIn("token", json.dumps(row).lower())

    def test_no_secret_in_outputs(self):
        secret = "SECRET-DO-NOT-PRINT"
        with open(self.paths["secret"], "w", encoding="utf-8") as handle:
            handle.write(f"SECRET={secret}\n")
        _code, out = self.run_main(decision_mode="hysteresis")
        combined = out + self.log_text() + json.dumps(self.read_state()) + json.dumps(
            self.read_rounds())
        self.assertNotIn(secret, combined)


class LegacyRegressionHelpersTests(unittest.TestCase):
    def test_mode_and_group_semantics_unchanged(self):
        self.assertEqual(pas.decide_mode([US1], [SG1]), "A")
        self.assertEqual(pas.decide_mode([], [SG1]), "B")
        self.assertEqual(pas.decide_mode([], []), "C")

    def test_muse_untouched_in_b_and_c(self):
        groups = {g: MainHarness.PROXIES[g] for g in pas.SWITCH_GROUPS}
        plan = pas.plan_switches("B", groups, MainHarness.DELAYS, set(), {SG1}, {SG1})
        self.assertIsNone(plan["MUSE"])
        self.assertEqual(plan["GPT"], SG1)
        plan = pas.plan_switches("C", groups, MainHarness.DELAYS, set(), set(), {JP1})
        self.assertIsNone(plan["MUSE"])
        self.assertEqual(plan["GPT"], JP1)


if __name__ == "__main__":
    unittest.main()
