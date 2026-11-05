import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_status_alert.py"
spec = importlib.util.spec_from_file_location("live_status_alert", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BEIJING_TZ = timezone(timedelta(hours=8))
FAKE_SECRET = "SUPER-SECRET-TOKEN-DO-NOT-LEAK"
FAKE_SUB_URL = "https://sub.example.com/link?token=abc123"
NOW = datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)


def utcnow():
    return datetime.now(timezone.utc)


def ago_iso(minutes, base=NOW, tz=timezone.utc):
    return (base - timedelta(minutes=minutes)).astimezone(tz).isoformat()


def member(name, usable=True, stale=False):
    return {"name": name, "last_delay_ms": 120 if usable else 0,
            "last_time": ago_iso(1), "usable_by_history": usable, "stale": stale}


def group(members, now=None):
    usable = sum(1 for m in members if m["usable_by_history"])
    return {"type": "Fallback", "now": now if now is not None else members[0]["name"],
            "member_count": len(members), "members": [m["name"] for m in members],
            "member_status": members, "usable_count_by_history": usable}


def make_collected(base=None, **overrides):
    """A fully healthy schema-v1 document relative to base; override any section."""
    if base is None:
        base = NOW
    gpt_members = [member("美国1｜AI通用"), member("美国2｜AI通用")]
    doc = {
        "schema_version": 1,
        "collected_at": {"utc": base.isoformat(timespec="seconds"),
                          "beijing": base.astimezone(BEIJING_TZ).isoformat(timespec="seconds")},
        "ok": True,
        "api": {"base": "http://127.0.0.1:9090", "reachable": True, "errors": []},
        "groups": {
            "GPT": group(gpt_members),
            "US-FAST": group([member("美国3｜快"), member("美国4｜快")]),
            "GROKBOT": group([member("机器人1"), member("机器人2")]),
            "GROKAPI": group([member("接口1"), member("接口2")]),
        },
        "usable_count_note": "口径=history",
        "process": {"mihomo_pid": 4321, "mihomo_start_time": ago_iso(600, base),
                     "boot_time": ago_iso(10000, base)},
        "state_files": {
            "ippool": {"path": "C:\\mihomo\\state\\xigua-ippool\\ippool.json",
                        "host_count": 2, "healthy_ip_count": 3, "ip_total": 5,
                        "updated_at": ago_iso(5, base)},
            "feishu_gpt_history": {"path": "x", "version": 1, "node_count": 4},
            "feishu_bitable_sync_state": {
                "path": "y",
                "summary": {"last_result": "ok", "last_success_time": ago_iso(10, base),
                             "consecutive_failures": 0}},
        },
        "scheduled_tasks": {
            name: {"exists": True, "state": "Ready", "last_run_time": ago_iso(3, base),
                    "last_task_result": 0, "next_run_time": ago_iso(-27, base)}
            for name in mod.TASK_NAMES
        },
        "freshness_window_minutes": 15.0,
        "errors": [],
    }
    doc.update(overrides)
    return doc


def codes(result):
    return [r["code"] for r in result["reasons"]]


def zero_gpt_doc(base, members=("美国1｜AI通用", "美国2｜AI通用")):
    """A doc collected at ``base`` whose GPT group has no usable member."""
    doc = make_collected(base=base)
    doc["groups"]["GPT"] = group([member(name, usable=False) for name in members])
    return doc


def evaluate(doc, prev_state=None, thresholds=None, now=NOW):
    return mod.evaluate(doc, prev_state=prev_state, thresholds=thresholds, now=now)


class RuleTests(unittest.TestCase):
    def test_all_healthy_is_normal(self):
        result = evaluate(make_collected())
        self.assertEqual(result["level"], "正常")
        self.assertEqual(result["level_code"], 0)
        self.assertEqual(result["reasons"], [])
        self.assertIn("各组正常", result["summary"])
        self.assertIn("美国1｜AI通用", result["summary"])
        self.assertIn("可用 2 个", result["summary"])

    # a) api unreachable
    def test_api_unreachable_is_severe(self):
        doc = make_collected()
        doc["api"]["reachable"] = False
        result = evaluate(doc)
        self.assertEqual(result["level_code"], 2)
        self.assertIn("api_unreachable", codes(result))

    # b) mihomo process missing
    def test_process_missing_is_severe(self):
        for pid in (None, 0, "1234"):
            doc = make_collected()
            doc["process"]["mihomo_pid"] = pid
            result = evaluate(doc)
            self.assertIn("mihomo_process_missing", codes(result), f"pid={pid!r}")
            self.assertEqual(result["level_code"], 2)
        result = evaluate(make_collected())
        self.assertNotIn("mihomo_process_missing", codes(result))

    # c) GPT zero usable, consecutive rounds
    def test_gpt_zero_first_round_is_degraded_then_severe(self):
        # one round per distinct collected_at
        r1 = evaluate(zero_gpt_doc(NOW - timedelta(minutes=20)))  # no prev_state -> first round
        self.assertIn("gpt_zero_usable_first_round", codes(r1))
        self.assertEqual(r1["level_code"], 1)
        self.assertIn("待确认", r1["summary"])
        self.assertEqual(r1["next_state"]["gpt_zero_usable_rounds"], 1)

        r2 = evaluate(zero_gpt_doc(NOW - timedelta(minutes=10)), prev_state=r1["next_state"])
        self.assertIn("gpt_zero_usable_persistent", codes(r2))
        self.assertEqual(r2["level_code"], 2)
        self.assertEqual(r2["next_state"]["gpt_zero_usable_rounds"], 2)

        r3 = evaluate(zero_gpt_doc(NOW), prev_state=r2["next_state"])
        self.assertIn("gpt_zero_usable_persistent", codes(r3))
        self.assertEqual(r3["next_state"]["gpt_zero_usable_rounds"], 3)

    def test_gpt_zero_counter_resets_on_recovery(self):
        r1 = evaluate(zero_gpt_doc(NOW - timedelta(minutes=20), members=("美国1｜AI通用",)))
        self.assertEqual(r1["next_state"]["gpt_zero_usable_rounds"], 1)
        r2 = evaluate(make_collected(base=NOW - timedelta(minutes=10)), prev_state=r1["next_state"])
        self.assertEqual(r2["next_state"]["gpt_zero_usable_rounds"], 0)
        self.assertEqual(r2["level_code"], 0)
        # after recovery, a fresh zero round is degraded again, not severe
        r3 = evaluate(zero_gpt_doc(NOW, members=("美国1｜AI通用",)), prev_state=r2["next_state"])
        self.assertIn("gpt_zero_usable_first_round", codes(r3))
        self.assertEqual(r3["level_code"], 1)

    def test_gpt_group_unreadable_keeps_counter(self):
        doc = make_collected()
        doc["groups"]["GPT"] = None
        prev = {"gpt_zero_usable_rounds": 1}
        result = evaluate(doc, prev_state=prev)
        self.assertEqual(result["next_state"]["gpt_zero_usable_rounds"], 1)
        self.assertNotIn("gpt_zero_usable_persistent", codes(result))
        self.assertNotIn("gpt_zero_usable_first_round", codes(result))

    # d) sync failures
    def test_sync_consecutive_failures_severe(self):
        doc = make_collected()
        doc["state_files"]["feishu_bitable_sync_state"]["summary"]["consecutive_failures"] = 3
        result = evaluate(doc)
        self.assertIn("sync_consecutive_failures", codes(result))
        self.assertEqual(result["level_code"], 2)
        doc["state_files"]["feishu_bitable_sync_state"]["summary"]["consecutive_failures"] = 2
        self.assertNotIn("sync_consecutive_failures", codes(evaluate(doc)))

    def test_sync_last_success_over_three_hours_severe(self):
        doc = make_collected()
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = ago_iso(181)
        result = evaluate(doc)
        self.assertIn("sync_success_too_old", codes(result))
        self.assertEqual(result["level_code"], 2)
        self.assertNotIn("sync_success_stale", codes(result))  # subsumed, no double fire
        self.assertIn("北京时间", result["summary"])

    # i) sync stale (degraded band)
    def test_sync_last_success_over_sixty_minutes_degraded(self):
        doc = make_collected()
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = ago_iso(120)
        result = evaluate(doc)
        self.assertIn("sync_success_stale", codes(result))
        self.assertEqual(result["level_code"], 1)

    def test_sync_state_missing_does_not_fire(self):
        doc = make_collected()
        doc["state_files"]["feishu_bitable_sync_state"] = None
        result = evaluate(doc)
        self.assertEqual(result["level_code"], 0)

    def test_sync_naive_timestamp_is_read_as_beijing(self):
        doc = make_collected()
        naive_recent = (NOW.astimezone(BEIJING_TZ) - timedelta(minutes=10)).replace(tzinfo=None)
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = naive_recent.isoformat()
        self.assertEqual(evaluate(doc)["level_code"], 0)
        naive_old = (NOW.astimezone(BEIJING_TZ) - timedelta(hours=4)).replace(tzinfo=None)
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = naive_old.isoformat()
        self.assertIn("sync_success_too_old", codes(evaluate(doc)))

    # e) collect failed
    def test_collect_failed_is_severe(self):
        doc = make_collected(ok=False)
        doc["api"]["reachable"] = False
        doc["groups"] = {name: None for name in ("GPT", "US-FAST", "GROKBOT", "GROKAPI")}
        result = evaluate(doc)
        self.assertIn("collect_failed", codes(result))
        self.assertEqual(result["level_code"], 2)

    # f) GPT single usable
    def test_gpt_single_usable_lists_node_name(self):
        doc = make_collected()
        doc["groups"]["GPT"] = group([member("美国1｜AI通用"),
                                       member("美国2｜AI通用", usable=False)])
        result = evaluate(doc)
        self.assertIn("gpt_single_usable", codes(result))
        self.assertEqual(result["level_code"], 1)
        text = next(r["text"] for r in result["reasons"] if r["code"] == "gpt_single_usable")
        self.assertIn("GPT 仅 1 个节点可用：美国1｜AI通用", text)

    # g) selected member unusable in other groups
    def test_group_now_unusable_fires_only_when_others_usable(self):
        doc = make_collected()
        doc["groups"]["GROKBOT"] = group([member("机器人1", usable=False),
                                           member("机器人2")], now="机器人1")
        result = evaluate(doc)
        self.assertIn("group_now_unusable", codes(result))
        text = next(r["text"] for r in result["reasons"] if r["code"] == "group_now_unusable")
        self.assertIn("GROKBOT", text)
        self.assertIn("机器人1", text)
        self.assertIn("机器人2", text)
        # now is usable -> no fire
        doc2 = make_collected()
        self.assertNotIn("group_now_unusable", codes(evaluate(doc2)))
        # nobody usable -> g does not fire (no "别的成员可用")
        doc3 = make_collected()
        doc3["groups"]["US-FAST"] = group([member("美国3｜快", usable=False),
                                            member("美国4｜快", usable=False)], now="美国3｜快")
        self.assertNotIn("group_now_unusable", codes(evaluate(doc3)))

    # h) ippool
    def test_ippool_stale_and_empty(self):
        doc = make_collected()
        doc["state_files"]["ippool"]["updated_at"] = ago_iso(45)
        self.assertIn("ippool_stale", codes(evaluate(doc)))
        doc = make_collected()
        doc["state_files"]["ippool"]["healthy_ip_count"] = 0
        self.assertIn("ippool_no_healthy_ip", codes(evaluate(doc)))
        doc = make_collected()
        doc["state_files"]["ippool"]["updated_at"] = None
        self.assertIn("ippool_updated_at_missing", codes(evaluate(doc)))
        doc = make_collected()
        doc["state_files"]["ippool"]["updated_at"] = "not-a-time"
        self.assertIn("ippool_updated_at_unparsable", codes(evaluate(doc)))
        doc = make_collected()
        doc["state_files"]["ippool"] = None
        result = evaluate(doc)
        self.assertEqual(result["level_code"], 0)  # missing pool file: no h) fire

    def test_ippool_fresh_and_healthy_no_fire(self):
        self.assertNotIn("ippool_stale", codes(evaluate(make_collected())))
        self.assertNotIn("ippool_no_healthy_ip", codes(evaluate(make_collected())))

    # j) scheduled tasks
    def test_task_bad_result_code_fires_with_name_and_code(self):
        doc = make_collected()
        doc["scheduled_tasks"]["MihomoNinjaGrokFeishuSync"]["last_task_result"] = 1
        result = evaluate(doc)
        self.assertIn("task_last_result_bad", codes(result))
        text = next(r["text"] for r in result["reasons"] if r["code"] == "task_last_result_bad")
        self.assertIn("MihomoNinjaGrokFeishuSync", text)
        self.assertIn("1", text)

    def test_task_running_codes_do_not_fire(self):
        for code in (0, 267008, 267009, 267011, 267014, 267055):
            doc = make_collected()
            for name in mod.TASK_NAMES:
                doc["scheduled_tasks"][name]["last_task_result"] = code
            self.assertNotIn("task_last_result_bad", codes(evaluate(doc)), f"code={code}")

    def test_task_missing_or_null_result_does_not_fire(self):
        doc = make_collected()
        doc["scheduled_tasks"]["MihomoXiguaIpPool"] = None
        doc["scheduled_tasks"]["MihomoFeishuBitableSync"]["last_task_result"] = None
        self.assertNotIn("task_last_result_bad", codes(evaluate(doc)))

    # k) stale members
    def test_stale_member_majority_fires(self):
        doc = make_collected()
        # 4 groups x 2 members = 8; make 5 stale (> half)
        for gname in ("GPT", "US-FAST"):
            for m in doc["groups"][gname]["member_status"]:
                m["stale"] = True
        doc["groups"]["GROKBOT"]["member_status"][0]["stale"] = True
        result = evaluate(doc)
        self.assertIn("members_stale_majority", codes(result))

    def test_stale_member_exactly_half_does_not_fire(self):
        doc = make_collected()
        for gname in ("GPT", "US-FAST"):
            for m in doc["groups"][gname]["member_status"]:
                m["stale"] = True
        self.assertNotIn("members_stale_majority", codes(evaluate(doc)))

    # --- merging / summary -------------------------------------------------

    def test_multiple_rules_take_highest_level_and_merge_top_two(self):
        doc = make_collected(ok=False)
        doc["api"]["reachable"] = False
        doc["process"]["mihomo_pid"] = None
        doc["state_files"]["ippool"]["healthy_ip_count"] = 0
        result = evaluate(doc)
        self.assertEqual(result["level_code"], 2)
        severe_texts = [r["text"] for r in result["reasons"] if r["level_code"] == 2]
        self.assertGreaterEqual(len(severe_texts), 3)
        self.assertEqual(result["summary"], "；".join(severe_texts[:2]))
        # every rule that fired is listed
        for expected in ("collect_failed", "api_unreachable",
                          "mihomo_process_missing", "ippool_no_healthy_ip"):
            self.assertIn(expected, codes(result))

    def test_degraded_only_summary_merges_top_two_degraded(self):
        doc = make_collected()
        doc["groups"]["GPT"] = group([member("美国1｜AI通用"),
                                       member("美国2｜AI通用", usable=False)])
        doc["state_files"]["ippool"]["updated_at"] = ago_iso(45)
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = ago_iso(120)
        result = evaluate(doc)
        self.assertEqual(result["level_code"], 1)
        degraded_texts = [r["text"] for r in result["reasons"] if r["level_code"] == 1]
        self.assertEqual(len(degraded_texts), 3)
        self.assertEqual(result["summary"], "；".join(degraded_texts[:2]))


class RobustnessTests(unittest.TestCase):
    def test_empty_and_none_documents_do_not_crash(self):
        for bad in ({}, None, [], "x", 42):
            result = mod.evaluate(bad, now=NOW)
            self.assertEqual(result["level_code"], 2)
            self.assertIn("collect_failed", codes(result))

    def test_missing_sections_and_null_fields_do_not_crash(self):
        doc = make_collected()
        doc["groups"] = None
        doc["process"] = None
        doc["state_files"] = None
        doc["scheduled_tasks"] = None
        result = evaluate(doc)
        self.assertIsInstance(result["level_code"], int)
        doc2 = make_collected()
        doc2["groups"]["GPT"]["usable_count_by_history"] = "0"  # wrong type
        doc2["groups"]["GPT"]["member_status"] = [None, {}, "junk"]
        result2 = evaluate(doc2)
        self.assertIn(result2["level_code"], (0, 1, 2))

    def test_corrupt_prev_state_treated_as_first_round(self):
        doc = make_collected()
        doc["groups"]["GPT"] = group([member("美国1｜AI通用", usable=False)])
        for bad_prev in ({"gpt_zero_usable_rounds": "junk"},
                          {"gpt_zero_usable_rounds": -5},
                          {"gpt_zero_usable_rounds": True},
                          "not-a-dict"):
            result = evaluate(doc, prev_state=bad_prev)
            self.assertIn("gpt_zero_usable_first_round", codes(result), repr(bad_prev))
            self.assertEqual(result["next_state"]["gpt_zero_usable_rounds"], 1)

    def test_texts_never_contain_secret_style_strings(self):
        doc = make_collected(ok=False)
        doc["api"] = {"base": FAKE_SUB_URL, "reachable": False,
                       "errors": [FAKE_SECRET, f"Bearer {FAKE_SECRET}"]}
        doc["errors"] = [FAKE_SECRET]
        doc["usable_count_note"] = FAKE_SUB_URL
        doc["process"]["mihomo_pid"] = None
        doc["state_files"]["ippool"]["healthy_ip_count"] = 0
        doc["groups"]["GPT"] = group([member("美国1｜AI通用", usable=False)])
        result = evaluate(doc, prev_state={"gpt_zero_usable_rounds": 5})
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(FAKE_SECRET, blob)
        self.assertNotIn("sub.example.com", blob)
        self.assertNotIn("Bearer", blob)

    def test_thresholds_configurable(self):
        doc = make_collected()
        doc["groups"]["GPT"] = group([member("美国1｜AI通用", usable=False)])
        # severe on the very first zero round when threshold is 1
        result = evaluate(doc, thresholds={"gpt_zero_severe_rounds": 1})
        self.assertIn("gpt_zero_usable_persistent", codes(result))
        # wider ippool window keeps a 45-min-old pool quiet
        doc2 = make_collected()
        doc2["state_files"]["ippool"]["updated_at"] = ago_iso(45)
        self.assertNotIn("ippool_stale", codes(evaluate(doc2, thresholds={"ippool_stale_minutes": 120})))
        # gpt_low_usable_count=2 makes a 2-usable GPT group degraded
        result2 = evaluate(make_collected(), thresholds={"gpt_low_usable_count": 2})
        self.assertIn("gpt_single_usable", codes(result2))
        text = next(r["text"] for r in result2["reasons"] if r["code"] == "gpt_single_usable")
        self.assertIn("GPT 仅 2 个节点可用：美国1｜AI通用、美国2｜AI通用", text)
        # unknown threshold keys are ignored
        self.assertEqual(evaluate(make_collected(), thresholds={"bogus": 1})["level_code"], 0)

    def test_next_state_carries_beijing_timestamp(self):
        result = evaluate(make_collected())
        self.assertEqual(result["next_state"]["evaluated_at_beijing"], "2026-11-05 18:00:00")
        self.assertEqual(result["next_state"]["last_level"], "正常")


class CollectedAtDedupTests(unittest.TestCase):
    """The GPT zero-usable counter advances once per distinct collected_at."""

    def test_same_collected_at_repeat_does_not_increment(self):
        doc = zero_gpt_doc(NOW)
        r1 = evaluate(doc)
        self.assertEqual(r1["next_state"]["gpt_zero_usable_rounds"], 1)
        state = r1["next_state"]
        for _ in range(5):
            repeat = evaluate(doc, prev_state=state)
            self.assertEqual(repeat["next_state"]["gpt_zero_usable_rounds"], 1)
            self.assertIn("gpt_zero_usable_first_round", codes(repeat))
            self.assertNotIn("gpt_zero_usable_persistent", codes(repeat))
            self.assertEqual(repeat["level_code"], 1)
            state = repeat["next_state"]

    def test_new_collected_at_increments(self):
        state = None
        for minutes in (30, 20, 10, 0):
            result = evaluate(zero_gpt_doc(NOW - timedelta(minutes=minutes)), prev_state=state)
            state = result["next_state"]
        self.assertEqual(state["gpt_zero_usable_rounds"], 4)

    def test_utc_and_beijing_forms_of_one_instant_are_the_same_round(self):
        doc = zero_gpt_doc(NOW)
        r1 = evaluate(doc)
        # same instant expressed only through the beijing member
        clone = zero_gpt_doc(NOW)
        clone["collected_at"] = {"beijing": NOW.astimezone(BEIJING_TZ).isoformat()}
        r2 = evaluate(clone, prev_state=r1["next_state"])
        self.assertEqual(r2["next_state"]["gpt_zero_usable_rounds"], 1)
        self.assertNotIn("gpt_zero_usable_persistent", codes(r2))

    def test_recovery_still_clears_on_a_repeated_round(self):
        r1 = evaluate(zero_gpt_doc(NOW))
        healthy = make_collected(base=NOW)  # identical collected_at, usable again
        r2 = evaluate(healthy, prev_state=r1["next_state"])
        self.assertEqual(r2["next_state"]["gpt_zero_usable_rounds"], 0)
        self.assertEqual(r2["level_code"], 0)

    def test_missing_or_unparsable_collected_at_counts_every_round(self):
        for bad in (None, {}, "not-a-time", {"utc": None, "beijing": ""}, 42):
            doc = zero_gpt_doc(NOW)
            doc["collected_at"] = bad
            r1 = evaluate(doc)
            r2 = evaluate(doc, prev_state=r1["next_state"])
            r3 = evaluate(doc, prev_state=r2["next_state"])
            self.assertEqual([r["next_state"]["gpt_zero_usable_rounds"] for r in (r1, r2, r3)],
                             [1, 2, 3], repr(bad))
            self.assertIn("gpt_zero_usable_persistent", codes(r2), repr(bad))
            self.assertIsNone(r3["next_state"]["last_collected_at"], repr(bad))

    def test_state_records_last_collected_at_in_beijing(self):
        result = evaluate(zero_gpt_doc(NOW))
        self.assertEqual(result["next_state"]["last_collected_at"],
                         NOW.astimezone(BEIJING_TZ).isoformat())
        # a state written by the older version has no key: still a fresh round
        legacy = {"gpt_zero_usable_rounds": 1}
        self.assertEqual(evaluate(zero_gpt_doc(NOW), prev_state=legacy)
                         ["next_state"]["gpt_zero_usable_rounds"], 2)

    def test_data_age_still_uses_wall_clock_on_a_repeated_round(self):
        # a repeated round must not freeze the age-based rules either
        doc = zero_gpt_doc(NOW)
        doc["state_files"]["feishu_bitable_sync_state"]["summary"][
            "last_success_time"] = ago_iso(181)
        r1 = evaluate(doc)
        r2 = evaluate(doc, prev_state=r1["next_state"])
        for result in (r1, r2):
            self.assertIn("sync_success_too_old", codes(result))


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state" / "live-status-alert-state.json"

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        mod.save_state(self.path, {"gpt_zero_usable_rounds": 2})
        self.assertTrue(self.path.is_file())
        self.assertEqual(mod.load_state(self.path), {"gpt_zero_usable_rounds": 2})
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [])

    def test_save_overwrites_existing(self):
        mod.save_state(self.path, {"a": 1})
        mod.save_state(self.path, {"b": 2})
        self.assertEqual(mod.load_state(self.path), {"b": 2})

    def test_load_missing_or_corrupt_returns_none(self):
        self.assertIsNone(mod.load_state(self.path))  # missing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(mod.load_state(self.path))  # corrupt
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(mod.load_state(self.path))  # not a dict
        self.path.write_bytes(b"\xff\xfe\x00bad-bytes")
        self.assertIsNone(mod.load_state(self.path))  # undecodable

    def test_corrupt_state_file_is_repaired_by_next_save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{corrupt", encoding="utf-8")
        mod.save_state(self.path, {"gpt_zero_usable_rounds": 0})
        self.assertEqual(mod.load_state(self.path), {"gpt_zero_usable_rounds": 0})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_file = self.root / "alert-state.json"
        self.input_file = self.root / "collected.json"

    def run_main(self, argv, stdin_text=None):
        with mock.patch.object(sys, "stdout", new=io.StringIO()) as out:
            if stdin_text is not None:
                with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
                    code = mod.main(argv)
            else:
                code = mod.main(argv)
        return code, out.getvalue()

    def write_input(self, doc):
        self.input_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(self.input_file)

    def fresh_doc(self, **overrides):
        # CLI runs against the real clock, so ages must be relative to real now
        return make_collected(base=utcnow(), **overrides)

    def test_cli_file_input_json_stdout_exit_zero(self):
        path = self.write_input(self.fresh_doc())
        code, out = self.run_main(["--input", path, "--state-file", str(self.state_file), "--json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["level"], "正常")
        self.assertTrue(self.state_file.is_file())

    def test_cli_stdin_input(self):
        code, out = self.run_main(
            ["--input", "-", "--state-file", str(self.state_file), "--json"],
            stdin_text=json.dumps(self.fresh_doc(), ensure_ascii=False))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["level_code"], 0)

    def test_cli_out_file(self):
        path = self.write_input(self.fresh_doc())
        out_path = self.root / "alert.json"
        code, stdout_text = self.run_main(
            ["--input", path, "--state-file", str(self.state_file), "--out", str(out_path)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout_text, "")  # --out without --json prints nothing
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8"))["level"], "正常")

    def test_cli_invalid_input_is_severe_and_exit_zero(self):
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, out = self.run_main(["--input", str(bad), "--state-file", str(self.state_file), "--json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["level_code"], 2)
        self.assertIn("input_invalid", [r["code"] for r in result["reasons"]])
        missing = self.root / "nope.json"
        code2, out2 = self.run_main(["--input", str(missing),
                                      "--state-file", str(self.state_file), "--json"])
        self.assertEqual(code2, 0)
        self.assertEqual(json.loads(out2)["level_code"], 2)

    def test_cli_state_persists_across_runs(self):
        # each run evaluates a NEW collection (distinct collected_at); re-running
        # the same document must not advance the counter (see dedup tests below)
        argv = ["--input", str(self.input_file), "--state-file", str(self.state_file), "--json"]
        self.write_input(zero_gpt_doc(utcnow(), members=("美国1｜AI通用",)))
        _, out1 = self.run_main(argv)
        self.assertEqual(json.loads(out1)["level_code"], 1)  # first round: 待确认
        self.write_input(zero_gpt_doc(utcnow() + timedelta(seconds=60),
                                      members=("美国1｜AI通用",)))
        _, out2 = self.run_main(argv)
        result2 = json.loads(out2)
        self.assertEqual(result2["level_code"], 2)  # second round: severe
        self.assertIn("gpt_zero_usable_persistent", [r["code"] for r in result2["reasons"]])
        # recovery resets the counter on disk
        self.write_input(make_collected(base=utcnow() + timedelta(seconds=120)))
        _, out3 = self.run_main(argv)
        self.assertEqual(json.loads(out3)["level_code"], 0)
        self.assertEqual(mod.load_state(self.state_file)["gpt_zero_usable_rounds"], 0)

    def test_cli_corrupt_state_file_starts_fresh(self):
        self.state_file.write_text("{{{corrupt", encoding="utf-8")
        zero_doc = self.fresh_doc()
        zero_doc["groups"]["GPT"] = group([member("美国1｜AI通用", usable=False)])
        path = self.write_input(zero_doc)
        code, out = self.run_main(["--input", path, "--state-file", str(self.state_file), "--json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertIn("gpt_zero_usable_first_round", [r["code"] for r in result["reasons"]])

    def test_cli_never_writes_outside_given_paths(self):
        path = self.write_input(self.fresh_doc())
        before = set(os.listdir(self.root))
        self.run_main(["--input", path, "--state-file", str(self.state_file), "--json"])
        after = set(os.listdir(self.root))
        self.assertEqual(after - before, {self.state_file.name})


if __name__ == "__main__":
    unittest.main()
