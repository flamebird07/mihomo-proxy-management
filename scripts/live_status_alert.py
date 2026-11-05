#!/usr/bin/env python3
"""Alert-level evaluator for live_status_collect.py output (DIAN-16).

Pure evaluation: reads a collector JSON document (schema v1) plus an optional
previous state dict, produces an alert level, a one-line Chinese summary and
the full reason list. Never writes to Feishu, never touches the mihomo API,
never modifies anything except its own state file. Exit code is always 0.

Result shape::

    {
      "level": "正常" | "降级" | "严重",
      "level_code": 0 | 1 | 2,
      "summary": str,                 # 中文一句话（最多合并最高级前 2 条）
      "reasons": [{"code": str, "level": str, "level_code": int, "text": str}],
      "next_state": dict              # 连续轮次计数等，写回状态文件
    }

State file: JSON, default C:\\mihomo\\state\\live-status-alert-state.json
(configurable via --state-file). Written atomically (temp file + os.replace).
A missing or corrupt state file is treated as a first round — never fatal.

Timestamps: aware ISO strings are used as-is; naive ones are assumed to be
Beijing local time (sync-feishu-bitable writes ``datetime.now().isoformat()``
on a +08:00 machine). All displayed times are rendered in Beijing time.

Alert texts never contain secrets, subscription URLs or node passwords;
node names, task names, result codes and timestamps may appear.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING_TZ = timezone(timedelta(hours=8))
DEFAULT_STATE_FILE = r"C:\mihomo\state\live-status-alert-state.json"
GPT_GROUP = "GPT"
NOW_UNUSABLE_GROUPS = ("GROKBOT", "GROKAPI", "US-FAST")
TASK_NAMES = ("MihomoFeishuBitableSync", "MihomoXiguaIpPool", "MihomoNinjaGrokFeishuSync")

LEVEL_NAMES = {0: "正常", 1: "降级", 2: "严重"}

# All thresholds are configurable; these are the defaults required by DIAN-16.
DEFAULT_THRESHOLDS = {
    # c) GPT usable==0 becomes 严重 after this many consecutive rounds
    "gpt_zero_severe_rounds": 2,
    # f) 0 < usable <= this count -> 降级
    "gpt_low_usable_count": 1,
    # d) sync consecutive_failures >= this -> 严重
    "sync_consecutive_failures": 3,
    # d) sync last_success older than this many hours -> 严重
    "sync_success_severe_hours": 3.0,
    # i) sync last_success older than this many minutes -> 降级
    "sync_success_degraded_minutes": 60.0,
    # h) ippool updated_at older than this many minutes -> 降级
    "ippool_stale_minutes": 30.0,
    # k) stale member share strictly above this ratio -> 降级
    "stale_member_ratio": 0.5,
    # j) SCHED_S_* success/running result codes (0x41300..0x4132F) are not alarms
    "task_running_code_min": 267008,
    "task_running_code_max": 267055,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_iso(text):
    """Parse an ISO-8601 timestamp (>6 fractional digits trimmed). None on failure."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = re.sub(r"(\.\d{6})\d+", r"\1", text.strip())
    try:
        parsed = datetime.fromisoformat(t)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # naive stamps in this ecosystem are Beijing local time
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed


def _beijing_text(moment):
    """Render an aware datetime as Beijing-time text."""
    return moment.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _age_minutes(moment, now):
    return (now - moment).total_seconds() / 60.0


def _as_number(value):
    """int/float (not bool) -> float, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _usable_names(group):
    """Member names judged usable by history, in member order."""
    if not isinstance(group, dict):
        return []
    return [m.get("name") for m in group.get("member_status") or []
            if isinstance(m, dict) and m.get("usable_by_history") is True
            and isinstance(m.get("name"), str)]


def _reason(code, level_code, text):
    return {"code": code, "level": LEVEL_NAMES[level_code],
            "level_code": level_code, "text": text}


# ---------------------------------------------------------------------------
# rule evaluation (each appends 0..n reasons; pure w.r.t. collected)
# ---------------------------------------------------------------------------

def _rule_collect_and_api(collected, reasons):
    # e) collection itself failed
    if collected.get("ok") is not True:
        reasons.append(_reason("collect_failed", 2, "状态采集失败（ok=false），本轮数据不完整"))
    api = collected.get("api")
    # a) mihomo API unreachable
    if not isinstance(api, dict) or api.get("reachable") is not True:
        reasons.append(_reason("api_unreachable", 2, "mihomo API 不通（控制器不可达）"))


def _rule_process(collected, reasons):
    # b) mihomo process missing
    process = collected.get("process")
    pid = process.get("mihomo_pid") if isinstance(process, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        reasons.append(_reason("mihomo_process_missing", 2, "mihomo 进程不存在（未找到 mihomo_pid）"))


def _rule_gpt(collected, prev_state, thresholds, reasons, next_state):
    # c) / f) GPT usable-count rules with a consecutive-round counter
    groups = collected.get("groups")
    gpt = groups.get(GPT_GROUP) if isinstance(groups, dict) else None
    prev_rounds = prev_state.get("gpt_zero_usable_rounds")
    prev_rounds = int(prev_rounds) if isinstance(prev_rounds, int) and not isinstance(
        prev_rounds, bool) and prev_rounds >= 0 else 0
    if not isinstance(gpt, dict):
        # Unreadable GPT group: keep the counter unchanged (rules a/e already fire).
        next_state["gpt_zero_usable_rounds"] = prev_rounds
        return
    usable = gpt.get("usable_count_by_history")
    usable = int(usable) if isinstance(usable, int) and not isinstance(usable, bool) else None
    if usable is None:
        next_state["gpt_zero_usable_rounds"] = prev_rounds
        return
    if usable == 0:
        rounds = prev_rounds + 1
        next_state["gpt_zero_usable_rounds"] = rounds
        severe_after = thresholds["gpt_zero_severe_rounds"]
        if rounds >= severe_after:
            reasons.append(_reason(
                "gpt_zero_usable_persistent", 2,
                f"GPT 组无可用节点（history 口径），已连续 {rounds} 轮（≥{severe_after} 轮）"))
        else:
            reasons.append(_reason(
                "gpt_zero_usable_first_round", 1, "GPT 组暂无可用节点(待确认)"))
    else:
        next_state["gpt_zero_usable_rounds"] = 0
        if usable <= thresholds["gpt_low_usable_count"]:
            names = "、".join(_usable_names(gpt)) or "未知节点"
            reasons.append(_reason(
                "gpt_single_usable", 1, f"GPT 仅 {usable} 个节点可用：{names}"))


def _rule_sync(collected, thresholds, now, reasons):
    # d) / i) feishu bitable sync state
    state_files = collected.get("state_files")
    entry = state_files.get("feishu_bitable_sync_state") if isinstance(state_files, dict) else None
    summary = entry.get("summary") if isinstance(entry, dict) else None
    if not isinstance(summary, dict):
        return
    severe_age_fired = False

    failures = _as_number(summary.get("consecutive_failures"))
    if failures is not None and failures >= thresholds["sync_consecutive_failures"]:
        reasons.append(_reason(
            "sync_consecutive_failures", 2,
            f"飞书多维表格同步连续失败 {int(failures)} 次（≥{int(thresholds['sync_consecutive_failures'])} 次）"))

    last_success = _parse_iso(summary.get("last_success_time"))
    if last_success is not None:
        age_min = _age_minutes(last_success, now)
        severe_min = thresholds["sync_success_severe_hours"] * 60.0
        degraded_min = thresholds["sync_success_degraded_minutes"]
        if age_min > severe_min:
            severe_age_fired = True
            reasons.append(_reason(
                "sync_success_too_old", 2,
                f"飞书同步上次成功距今 {age_min / 60.0:.1f} 小时（>{thresholds['sync_success_severe_hours']:.0f} 小时），"
                f"上次成功：北京时间 {_beijing_text(last_success)}"))
        elif age_min > degraded_min:
            reasons.append(_reason(
                "sync_success_stale", 1,
                f"飞书同步上次成功距今 {age_min:.0f} 分钟（>{degraded_min:.0f} 分钟），"
                f"上次成功：北京时间 {_beijing_text(last_success)}"))
    return severe_age_fired


def _rule_group_now(collected, reasons):
    # g) GROKBOT / GROKAPI / US-FAST: selected member unusable while others are usable
    groups = collected.get("groups")
    if not isinstance(groups, dict):
        return
    for name in NOW_UNUSABLE_GROUPS:
        group = groups.get(name)
        if not isinstance(group, dict):
            continue
        now_member = group.get("now")
        if not isinstance(now_member, str) or not now_member:
            continue
        usable = _usable_names(group)
        if usable and now_member not in usable:
            reasons.append(_reason(
                "group_now_unusable", 1,
                f"{name} 当前选择「{now_member}」不可用（history 口径），组内可用：{'、'.join(usable)}"))


def _rule_ippool(collected, thresholds, now, reasons):
    # h) IP pool stale or empty
    state_files = collected.get("state_files")
    entry = state_files.get("ippool") if isinstance(state_files, dict) else None
    if not isinstance(entry, dict):
        return
    healthy = _as_number(entry.get("healthy_ip_count"))
    if healthy is not None and healthy == 0:
        reasons.append(_reason("ippool_no_healthy_ip", 1, "IP 池健康 IP 数为 0"))
    updated_raw = entry.get("updated_at")
    if updated_raw is None or (isinstance(updated_raw, str) and not updated_raw.strip()):
        reasons.append(_reason("ippool_updated_at_missing", 1, "IP 池 updated_at 缺失，无法确认更新时间"))
        return
    updated = _parse_iso(updated_raw)
    if updated is None:
        reasons.append(_reason("ippool_updated_at_unparsable", 1, "IP 池 updated_at 无法解析，无法确认更新时间"))
        return
    age_min = _age_minutes(updated, now)
    if age_min > thresholds["ippool_stale_minutes"]:
        reasons.append(_reason(
            "ippool_stale", 1,
            f"IP 池超过 {thresholds['ippool_stale_minutes']:.0f} 分钟未更新（距今 {age_min:.0f} 分钟，"
            f"上次更新：北京时间 {_beijing_text(updated)}）"))


def _rule_tasks(collected, thresholds, reasons):
    # j) scheduled task last result code bad (running/SCHED_S_* codes are not alarms)
    tasks = collected.get("scheduled_tasks")
    if not isinstance(tasks, dict):
        return
    lo = thresholds["task_running_code_min"]
    hi = thresholds["task_running_code_max"]
    for name in TASK_NAMES:
        task = tasks.get(name)
        if not isinstance(task, dict):
            continue
        code = _as_number(task.get("last_task_result"))
        if code is None:
            continue
        code_int = int(code)
        if code_int == 0 or lo <= code_int <= hi:
            continue
        reasons.append(_reason(
            "task_last_result_bad", 1,
            f"计划任务 {name} 上次结果码 {code_int}（非 0 且非运行中状态码）"))


def _rule_stale_members(collected, thresholds, reasons):
    # k) majority of members stale across all readable groups
    groups = collected.get("groups")
    if not isinstance(groups, dict):
        return
    total = 0
    stale = 0
    for group in groups.values():
        if not isinstance(group, dict):
            continue
        for member in group.get("member_status") or []:
            if not isinstance(member, dict):
                continue
            total += 1
            if member.get("stale") is True:
                stale += 1
    if total == 0:
        return
    ratio = stale / total
    if ratio > thresholds["stale_member_ratio"]:
        reasons.append(_reason(
            "members_stale_majority", 1,
            f"数据陈旧：成员 stale 占比 {ratio:.0%}（{stale}/{total}，超过一半）"))


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def _normal_summary(collected):
    groups = collected.get("groups")
    gpt = groups.get(GPT_GROUP) if isinstance(groups, dict) else None
    if isinstance(gpt, dict) and isinstance(gpt.get("now"), str):
        usable = gpt.get("usable_count_by_history")
        if isinstance(usable, int) and not isinstance(usable, bool):
            return f"各组正常：GPT 当前 {gpt['now']}，可用 {usable} 个"
    return "各组正常"


def evaluate(collected, prev_state=None, thresholds=None, now=None):
    """Evaluate one collected document. Pure function; never raises on bad input.

    ``collected``: dict from live_status_collect.py (missing/None tolerated).
    ``prev_state``: previous ``next_state`` dict, or None for a first round.
    ``thresholds``: partial override of DEFAULT_THRESHOLDS.
    ``now``: aware datetime (naive is assumed Beijing time); defaults to the
    current moment.
    """
    if not isinstance(collected, dict):
        collected = {}
    if not isinstance(prev_state, dict):
        prev_state = {}
    merged = dict(DEFAULT_THRESHOLDS)
    if isinstance(thresholds, dict):
        merged.update({k: v for k, v in thresholds.items() if k in DEFAULT_THRESHOLDS})
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, datetime) and now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING_TZ)

    reasons = []
    next_state = {}
    _rule_collect_and_api(collected, reasons)
    _rule_process(collected, reasons)
    _rule_gpt(collected, prev_state, merged, reasons, next_state)
    _rule_sync(collected, merged, now, reasons)
    _rule_group_now(collected, reasons)
    _rule_ippool(collected, merged, now, reasons)
    _rule_tasks(collected, merged, reasons)
    _rule_stale_members(collected, merged, reasons)

    level_code = max((r["level_code"] for r in reasons), default=0)
    if level_code == 0:
        summary = _normal_summary(collected)
    else:
        top = [r["text"] for r in reasons if r["level_code"] == level_code][:2]
        summary = "；".join(top)

    next_state.update({
        "last_level": LEVEL_NAMES[level_code],
        "last_level_code": level_code,
        "last_summary": summary,
        "last_reason_count": len(reasons),
        "evaluated_at_beijing": _beijing_text(now),
    })
    return {
        "level": LEVEL_NAMES[level_code],
        "level_code": level_code,
        "summary": summary,
        "reasons": reasons,
        "next_state": next_state,
    }


# ---------------------------------------------------------------------------
# state file (atomic write, corrupt-file tolerant)
# ---------------------------------------------------------------------------

def load_state(path):
    """Read the previous state dict. Missing/corrupt/unreadable -> None (first round)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    except UnicodeDecodeError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def save_state(path, state):
    """Atomically write the state dict as JSON. Creates the parent dir if needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_input(spec):
    """Return the parsed input document, or None with an error string."""
    try:
        if spec == "-":
            text = sys.stdin.read()
        else:
            text = Path(spec).read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"input unreadable: {type(exc).__name__}"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "input is not valid JSON"
    if not isinstance(data, dict):
        return None, "input JSON is not an object"
    return data, None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate live_status_collect.py JSON into an alert level (exit code always 0).")
    parser.add_argument("--input", required=True,
                        help="path to the collected JSON, or '-' for stdin")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                        help=f"alert state file (default {DEFAULT_STATE_FILE})")
    parser.add_argument("--out", default=None, help="optional output file path (UTF-8)")
    parser.add_argument("--json", action="store_true",
                        help="print the result JSON to stdout (default when --out is absent)")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    collected, input_error = _read_input(args.input)
    prev_state = load_state(args.state_file)

    if collected is None:
        # Unreadable/invalid input is itself a severe condition; counters are
        # carried over untouched so a bad round cannot reset persistence.
        result = {
            "level": LEVEL_NAMES[2],
            "level_code": 2,
            "summary": f"告警输入不可用（{input_error}），按采集失败处理",
            "reasons": [_reason("input_invalid", 2, f"告警输入不可用：{input_error}")],
            "next_state": dict(prev_state or {}),
        }
        result["next_state"].update({
            "last_level": result["level"],
            "last_level_code": 2,
            "last_summary": result["summary"],
            "evaluated_at_beijing": _beijing_text(datetime.now(timezone.utc)),
        })
    else:
        result = evaluate(collected, prev_state=prev_state)

    try:
        save_state(args.state_file, result["next_state"])
    except OSError:
        pass  # a failed state write must never change the exit code

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        try:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass
    if args.json or not args.out:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
