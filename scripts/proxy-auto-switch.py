#!/usr/bin/env python3
"""Proxy auto-failover for mihomo select groups GPT / MUSE / US-FAST.

Runs after sync-feishu-bitable.py in the half-hour pipeline. Measures ChatGPT
trace delay via the live controller, classifies US vs SG from node names
(same heuristics as sync-feishu-bitable.py), then applies:

  A  >=1 US alive  -> GPT, MUSE, US-FAST each to fastest US in that group's all
  B  no US, SG ok  -> GPT + US-FAST to fastest SG; MUSE unchanged
  C  US+SG dead    -> GPT + US-FAST drop to the whitelisted Japan node; MUSE
                     unchanged (still US only)

Candidate regions are hard-limited per group (GPT/US-FAST: US then SG then JP;
MUSE: US only), so no other region is ever used. GPT/MUSE/US-FAST are Fallback groups,
which the kernel re-selects every interval from the first alive member of the
list; measured on v1.19.28 a PUT here is a real pin: it holds while the node
passes its health check and is dropped on the first failed check, after which
the kernel takes over. A dead current node is therefore left to the kernel
(decide_group holds it instead of grab-cutting), and the script only re-pins to
a same-tier member that is clearly better -- best <= current*0.5 sustained over
two consecutive rounds.

Stability gate: a US/SG node only becomes a pin target after STABLE_ROUNDS (10)
consecutive alive rounds, counted from the `samples` history already kept in the
state file. While 日本06-×1 heads the GPT/US-FAST list this keeps a single
fake-alive US/SG sample from handing the group back and forth.

State file debounce: same mode does not re-alert. Alerts prefer Feishu IM when
FEISHU_RECEIVE_ID / FEISHU_CHAT_ID is set; otherwise append alert log.

--decision-mode picks the behaviour:
  legacy     fastest-node plan above, no thresholds (--legacy-fastest is an
             alias). State stays version 1, no rounds JSONL is written.
  shadow     legacy plan is executed, the thresholded decision is only recorded
             (log + rounds JSONL) so it can be compared for a shadow period.
  hysteresis (default) the thresholded decision of decide_group() is executed:
             smoothed medians, margin + candidate streak + dwell, tier
             promotion/demotion, freeze on an all-dead probe round.

Secrets (FEISHU_APP_SECRET, tokens, URLs) are never printed or logged.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import statistics
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# CY-GUS-DEV1: the unified GPT policy modules sit beside this script in
# production and in ../src in the offline bundle. MUSE/US-FAST keep the local
# decide_group path below; GPT is decided and executed only by this entry.
_HERE = Path(__file__).resolve().parent
if not (_HERE / "gpt_switch_entry.py").exists():
    sys.path.insert(0, str(_HERE.parent / "src"))
import gpt_switch_entry
import named_mutex_lock
from gpt_stability_policy import NodeSnapshot, PolicyConfig

DEFAULT_CONTROLLER = "http://127.0.0.1:9090"
DEFAULT_TARGET = "https://chatgpt.com/robots.txt"
DEFAULT_ENV_FILE = r"C:\Users\Administrator\.hermes\.env"
DEFAULT_STATE_FILE = r"C:\mihomo\state\proxy-failover-state.json"
DEFAULT_LOG_FILE = r"C:\mihomo\logs\proxy-failover.log"
DEFAULT_ALERT_LOG = r"C:\mihomo\logs\proxy-failover-alert.log"
DEFAULT_ALERT_TARGET = r"C:\mihomo\feishu-alert-target.json"
DEFAULT_ROUNDS_JSONL = r"C:\mihomo\logs\proxy-failover-rounds.jsonl"
SWITCH_GROUPS = ("GPT", "MUSE", "US-FAST")
SKIP_MEMBERS = {"DIRECT", "REJECT", "COMPATIBLE", "PASS", "GLOBAL"}
STATE_VERSION = 2
SAMPLE_KEEP = 12
SAMPLE_SMOOTH_MAX = 5
SAMPLE_MAX_AGE_H = 6
NODE_PRUNE_AGE_H = 24
RETRY_DELAY_S = 2.0
TIER_US, TIER_SG, TIER_JP, TIER_OTHER = 3, 2, 1, 0
# Rounds a US/SG node must be alive back to back (10 rounds at the ~30min task
# cadence) before it may be pinned to; a single sample is the fake-alive pattern
# that made `now` flip.
STABLE_ROUNDS = 10
# Third-tier pool for GPT/US-FAST, limited to the node measured alive 2026-11-03.
JP_TIER_NODES = {"日本06-×1"}
MISSING_SMOOTHED = 10 ** 12

SG_PATTERN = re.compile(r"新加坡|狮城|獅城|singapore|🇸🇬|\bSGP?\b", re.IGNORECASE)
US_PATTERN = re.compile(
    r"美国|美國|united states|\bUSA?\b|🇺🇸|los angeles|san jose|seattle|dallas|chicago|"
    r"new york|ashburn|silicon valley|phoenix|miami|atlanta|las vegas|portland|denver|"
    r"virginia|ohio|california|圣何塞|洛杉矶|西雅图|达拉斯|芝加哥|纽约|硅谷|凤凰城",
    re.IGNORECASE,
)
JP_PATTERN = re.compile(r"日本|japan|🇯🇵|tokyo|东京|東京", re.IGNORECASE)


def policy_region(name: str) -> str | None:
    """Region for the unified GPT policy.

    Japan is a normal region here, not the single-node whitelist that
    decide_group used: the unified 10-round gate applies to every region, and
    the source-3 bridge name gets no two-round exemption (unknown -> no move).
    """
    if SG_PATTERN.search(name):
        return "SG"
    if US_PATTERN.search(name):
        return "US"
    if name in JP_TIER_NODES or JP_PATTERN.search(name):
        return "JP"
    return None


class Logger:
    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, path: str, verbose: bool = False) -> None:
        self.path = Path(path)
        self.verbose = verbose
        self._ready = False

    def _fallback(self, line: str, exc: BaseException | None) -> None:
        """Never swallow a log failure: mirror the line to stderr."""
        suffix = f" (log write failed: {type(exc).__name__})" if exc else " (log setup failed)"
        print(line + suffix, file=sys.stderr, flush=True)

    def _open(self) -> None:
        if self._ready:
            return
        self._ready = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                backup = self.path.with_suffix(self.path.suffix + ".1")
                try:
                    if backup.exists():
                        backup.unlink()
                    self.path.rename(backup)
                except OSError as exc:
                    self._fallback(f"rotate failed for {self.path}", exc)
        except OSError as exc:
            self._fallback(f"log directory unavailable for {self.path}", exc)

    def _write(self, level: str, message: str) -> None:
        line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {level} {message}"
        self._open()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            self._fallback(line, exc)
        if self.verbose:
            print(line, file=sys.stderr)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)


def read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except (OSError, UnicodeError):
        pass
    return values


def source3_bridge_region() -> str | None:
    """Use fresh measured egress and the independent two-round admission gate."""
    try:
        state = json.loads(Path(r"C:\mihomo\source3-compatible\last-result.json").read_text(encoding="utf-8"))
        age = time.time() * 1000 - state["sync_ms"]
        if (not 0 <= age <= 20 * 60 * 1000 or not state.get("readback_verified")
                or state.get("selected") not in state.get("candidates", [])
                or state.get("selected_streak", 0) < 2):
            return None
        return {"美国": "US", "新加坡": "SG", "日本": "JP"}.get(state.get("selected_region"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def candidate_stable(name, stats):
    if name == "源3独立GPT":
        return source3_bridge_region() is not None
    return consecutive_alive(stats, STABLE_ROUNDS)


def region_of(name: str) -> str | None:
    if name == "源3独立GPT":
        return source3_bridge_region()
    if SG_PATTERN.search(name):
        return "SG"
    if US_PATTERN.search(name):
        return "US"
    return None


def api_json(method: str, url: str, body: dict | None = None, token: str | None = None,
             timeout: float = 15) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}


def request_delay(api: str, name: str, target: str, timeout_ms: int,
                  token: str | None = None) -> int | None:
    query = urllib.parse.urlencode({"url": target, "timeout": str(timeout_ms), "expected": "200"})
    endpoint = f"{api}/proxies/{urllib.parse.quote(name, safe='')}/delay?{query}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(endpoint, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=(timeout_ms / 1000) + 5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        value = int(payload.get("delay") or 0)
        # Mihomo can return a positive delay even when expected-status failed.
        # Its fallback decision uses the per-URL state, not the global alive bit.
        state_request = urllib.request.Request(
            f"{api}/proxies/{urllib.parse.quote(name, safe='')}", headers=headers)
        with urllib.request.urlopen(state_request, timeout=5) as response:
            state = json.loads(response.read().decode("utf-8", "replace"))
        health = (state.get("extra") or {}).get(target) or {}
        return value if value > 0 and health.get("alive") is True else None
    except Exception:
        return None


def load_state(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                           dir=str(target.parent))
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def pick_fastest(candidates: list[str], delays: dict[str, int | None]) -> str | None:
    scored = [(delays[n], n) for n in candidates if delays.get(n)]
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]))
    return scored[0][1]


def decide_mode(alive_us: list[str], alive_sg: list[str]) -> str:
    if alive_us:
        return "A"
    if alive_sg:
        return "B"
    return "C"


def allowed_tiers(group: str) -> set[int]:
    """US->SG->JP for GPT/US-FAST, US only for MUSE; no other region is ever used."""
    return {TIER_US} if group == "MUSE" else {TIER_US, TIER_SG, TIER_JP}


def plan_switches(mode: str, groups: dict[str, dict], delays: dict[str, int | None],
                  alive_us: set[str], alive_sg: set[str],
                  alive_any: set[str]) -> dict[str, str | None]:
    """Return desired node per group. None means leave unchanged."""
    plan: dict[str, str | None] = {g: None for g in SWITCH_GROUPS}

    def pool_for(group: str, alive: set[str]) -> list[str]:
        entry = groups.get(group) or {}
        if (entry.get("type") or "").lower() not in {"selector", "fallback"}:
            return []
        members = set(entry.get("all") or [])
        allowed = allowed_tiers(group)
        return [n for n in alive if n in members and tier_of(n) in allowed]

    def us_pick(group: str) -> str | None:
        return pick_fastest(pool_for(group, alive_us), delays)

    def sg_pick(group: str) -> str | None:
        return pick_fastest(pool_for(group, alive_sg), delays)

    if mode == "A":
        for g in SWITCH_GROUPS:
            plan[g] = us_pick(g)
    elif mode == "B":
        # MUSE never leaves the US tier, so it stays put on a US outage.
        plan["GPT"] = sg_pick("GPT")
        plan["US-FAST"] = sg_pick("US-FAST")
        plan["MUSE"] = None
    else:
        # Mode C: US+SG are both down. GPT/US-FAST drop to the Japan tier
        # (pool_for already excludes HK/TW and every other region); MUSE never
        # leaves the US tier, so it stays put and the alert fires.
        for g in SWITCH_GROUPS:
            plan[g] = None if g == "MUSE" else pick_fastest(pool_for(g, alive_any), delays)
    return plan


@dataclass
class SwitchParams:
    """Thresholds for the hysteresis decision (design section 2)."""

    switch_margin_ms: int = 300
    switch_margin_pct: int = 50
    switch_streak: int = 2
    min_dwell_min: int = 60
    retry_current: int = 2


def iso(ts: dt.datetime) -> str:
    return ts.isoformat(timespec="seconds")


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def as_int(value: Any, default: int = 0) -> int:
    """State fields can be any JSON type: never let them raise."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value else default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def parse_ts(value: Any) -> dt.datetime | None:
    """Parse a stored timestamp to naive local time; anything else is None.

    Comparing a tz-aware datetime against the naive wall clock used everywhere
    here raises TypeError, so tz-aware values are normalised instead of trusted.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is not None:
        try:
            when = when.astimezone().replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    return when


def keep_ts(value: Any, fallback: str | None = None) -> str | None:
    """Return the original string when it parses, else `fallback`."""
    original = as_str(value)
    if original is not None and parse_ts(original) is not None:
        return original
    return fallback


def recent_ts(values: Any, now: dt.datetime, max_age_h: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        when = parse_ts(value)
        if when is not None and 0 <= (now - when).total_seconds() <= max_age_h * 3600:
            out.append(value)
    return out


def tier_of(name: Any) -> int:
    region = region_of(name) if isinstance(name, str) else None
    if region == "US":
        return TIER_US
    if region == "SG":
        return TIER_SG
    if region == "JP" or (isinstance(name, str) and name in JP_TIER_NODES):
        return TIER_JP
    return TIER_OTHER


def node_samples(node: Any) -> list[list[Any]]:
    if not isinstance(node, dict) or not isinstance(node.get("samples"), list):
        return []
    samples = []
    for item in node["samples"]:
        if isinstance(item, list) and len(item) == 2:
            samples.append([item[0], item[1]])
    return samples


def _is_ms(value: Any) -> bool:
    return isinstance(value, int) and value > 0


def valid_samples(node: Any, now: dt.datetime | None = None) -> list[int]:
    """Recent <=5 valid samples inside the smoothing window (design 2.1)."""
    values = []
    for _ts, ms in node_samples(node):
        if not _is_ms(ms):
            continue
        when = parse_ts(_ts)
        if when is None:
            continue
        if now is not None and (now - when).total_seconds() > SAMPLE_MAX_AGE_H * 3600:
            continue
        values.append(ms)
    return values[-SAMPLE_SMOOTH_MAX:]


def smoothed_ms(node: Any, now: dt.datetime | None = None) -> int | None:
    values = valid_samples(node, now)
    if not values:
        return None
    return int(statistics.median(values))


def smoothed_of(name: Any, node_stats: dict, now: dt.datetime | None = None) -> int | None:
    node = node_stats.get(name) if isinstance(name, str) else None
    value = smoothed_ms(node, now)
    if value is None and node and _is_ms(node.get("delay")):
        return node["delay"]
    return value


def consecutive_alive(node: Any, rounds: int = 2) -> bool:
    recent = node_samples(node)[-rounds:]
    return len(recent) == rounds and all(_is_ms(ms) for _ts, ms in recent)


def rank_key(name: str, node_stats: dict, now: dt.datetime) -> tuple[int, str]:
    value = smoothed_of(name, node_stats, now)
    return (MISSING_SMOOTHED if value is None else value, name)


def pick_best(names: Any, node_stats: dict, now: dt.datetime) -> str | None:
    items = list(names or [])
    if not items:
        return None
    return sorted(items, key=lambda n: rank_key(n, node_stats, now))[0]


def need_margin_ms(cur_smooth: int | None, params: SwitchParams) -> int | None:
    if cur_smooth is None:
        return None
    return max(params.switch_margin_ms, cur_smooth * params.switch_margin_pct // 100)


def dwell_minutes(group_state: dict, now: dt.datetime) -> int | None:
    """Minutes since `since`; None when the field is missing or unparseable."""
    since = parse_ts((group_state or {}).get("since"))
    if since is None:
        return None
    return max(0, int((now - since).total_seconds() // 60))


def next_candidate_state(group_state: dict, proposed: str | None) -> tuple[str | None, int]:
    """Same candidate keeps growing the streak; a different candidate resets it."""
    if proposed is None:
        return None, 0
    if (group_state or {}).get("cand") == proposed:
        return proposed, as_int((group_state or {}).get("cand_streak"), 0) + 1
    return proposed, 1


def decide_group(group: str, current: str | None, members: Any, node_stats: dict,
                 group_state: dict, now: dt.datetime, params: SwitchParams,
                 details: dict | None = None,
                 engine: str = "selector") -> tuple[str | None, str]:
    """Pure per-group decision (design 3.1). No network, no state mutation.

    `node_stats[name]` carries this round's `delay`/`alive_round` plus the shared
    sample history; `group_state` carries this group's current/since/cand streak.
    `details` is filled with the round-record fields of design 4.3. `engine` is
    the controller-reported group type: on a fallback group a dead current node
    is left to the kernel, on a selector the script still has to rescue it.
    """
    det = details if details is not None else {}
    members = set(m or "" for m in (members or []))
    allowed = allowed_tiers(group)
    members = {n for n in members if tier_of(n) in allowed}
    node_stats = as_dict(node_stats)
    gs = as_dict(group_state)
    cur_node = as_dict(node_stats.get(current)) if current else {}

    cur_ms = cur_node.get("delay")
    cur_smooth = smoothed_of(current, node_stats, now) if current else None
    det.update({
        "current": current,
        "cur_ms": cur_ms,
        "cur_smooth": cur_smooth,
        "dwell_min": dwell_minutes(gs, now),
        "best": None,
        "best_ms": cur_ms,
        "best_smooth": cur_smooth,
        "need_margin_ms": None,
        "cand": None,
        "cand_streak": 0,
        "muse_frozen_dead": False,
    })

    def hold(reason: str, decision: str = "hold") -> tuple[str | None, str]:
        det["decision"] = decision
        det["reason"] = reason
        return None, reason

    def go(target: str, reason: str) -> tuple[str, str]:
        det["decision"] = "switch"
        det["reason"] = reason
        return target, reason

    def note(target: str) -> None:
        det["best"] = target
        det["best_ms"] = as_dict(node_stats.get(target)).get("delay")
        det["best_smooth"] = smoothed_of(target, node_stats, now)

    alive_all = {n for n, stat in node_stats.items() if as_dict(stat).get("alive_round")}
    if not alive_all:
        return hold("hold_probe_suspect", "freeze")

    alive = {n for n in alive_all if n in members}
    cur_valid = bool(current) and current in members
    cur_alive = cur_valid and bool(cur_node.get("alive_round"))
    cur_tier = tier_of(current) if cur_valid else 0

    if not cur_alive:
        if engine == "fallback" and cur_valid:
            # Dead-but-known node on a fallback group: the kernel health check
            # owns the failover, the script does not grab-cut it.
            return hold("hold_dead_delegate_fallback")
        # Design 3.3/3.4: a dead MUSE only accepts a US peer, while a missing or
        # null MUSE is a forced recovery and takes the best tier available.
        if cur_valid and group == "MUSE":
            pool = {n for n in alive if tier_of(n) == TIER_US}
        else:
            pool = set(alive)
        if not pool:
            if cur_valid and group == "MUSE":
                det["muse_frozen_dead"] = True
                return hold("hold_muse_frozen")
            return hold("hold_no_alive_member")
        # A/B/C semantics survive the forced path: only the best tier available,
        # and a US/SG node joins that tier only after STABLE_ROUNDS alive rounds.
        staged = {n for n in pool
                  if tier_of(n) <= TIER_JP
                  or candidate_stable(n, node_stats.get(n))}
        if not staged:
            staged = pool
        top = max(tier_of(n) for n in staged)
        pool = {n for n in staged if tier_of(n) == top}
        target = pick_best(pool, node_stats, now)
        note(target)
        target_tier = tier_of(target)
        if cur_valid and target_tier < cur_tier:
            return go(target, "switch_downgrade")
        if cur_valid and target_tier > cur_tier:
            return go(target, "switch_upgrade")
        return go(target, "switch_missing" if not cur_valid else "switch_dead")

    higher = {n for n in alive
              if tier_of(n) > cur_tier and (group != "MUSE" or tier_of(n) == TIER_US)}
    if higher:
        top = max(tier_of(n) for n in higher)
        higher = {n for n in higher if tier_of(n) == top}
        # Pinning away from the Japan node needs a US/SG member that has been
        # alive STABLE_ROUNDS rounds running, so one fake-alive sample cannot
        # drag GPT/US-FAST back onto a dead path.
        stable = {n for n in higher
                  if tier_of(n) <= TIER_JP
                  or candidate_stable(n, node_stats.get(n))}
        if not stable:
            note(pick_best(higher, node_stats, now))
            return hold("hold_cand_unstable")
        target = pick_best(stable, node_stats, now)
        note(target)
        return go(target, "switch_upgrade")

    same = {n for n in alive if tier_of(n) == cur_tier}
    same.add(current)
    best = pick_best(same, node_stats, now)
    note(best)
    if best is None or best == current:
        return hold("hold_ok")
    if group == "MUSE" and cur_tier != TIER_US:
        # Design G4: while MUSE sits on SG/other it never re-optimises inside
        # that tier; only a stable US recovery (upgrade above) may move it.
        return hold("hold_muse_frozen")

    cand_smooth = smoothed_of(best, node_stats, now)
    need = need_margin_ms(det["cur_smooth"], params)
    det["need_margin_ms"] = need
    if need is None or cand_smooth is None or det["cur_smooth"] - cand_smooth < need:
        return hold("hold_margin")

    det["cand"], det["cand_streak"] = next_candidate_state(gs, best)
    if not candidate_stable(best, node_stats.get(best)):
        return hold("hold_cand_unstable")
    if det["cand_streak"] < params.switch_streak:
        return hold("hold_streak")
    dwell = det["dwell_min"]
    # An unknown `since` is not a full dwell: only a forced condition may cut it.
    if dwell is None or dwell < params.min_dwell_min:
        return hold("hold_dwell")
    return go(best, "switch_margin")


def migrate_state(raw: Any, group_nows: dict[str, str | None],
                  now: dt.datetime) -> dict:
    """Return a version:2 state skeleton, keeping history and migrating v1.

    Every field is coerced independently: a state file that is valid JSON but
    holds the wrong type degrades to the cold-start value for that field only,
    and never raises. Per-group history is also aligned with what the
    controller runs right now, so an external manual switch resets dwell and
    candidate streak instead of inheriting them.
    """
    raw = as_dict(raw)
    ts = iso(now)
    old_groups = as_dict(raw.get("groups"))
    selected = as_dict(raw.get("selected"))
    controller_nows = as_dict(group_nows)
    updated = keep_ts(raw.get("updated"))
    groups_out: dict[str, dict] = {}
    for g in SWITCH_GROUPS:
        real = as_str(controller_nows.get(g))
        old = as_dict(old_groups.get(g))
        if old:
            current = as_str(old.get("current"))
            since = keep_ts(old.get("since"))
            cand = as_str(old.get("cand"))
            cand_streak = max(0, as_int(old.get("cand_streak"), 0))
            if current is None or (real is not None and current != real):
                current, since, cand, cand_streak = real, ts, None, 0
            groups_out[g] = {
                "current": current,
                "since": since or ts,
                "cand": cand,
                "cand_streak": cand_streak,
                "last_switch_ts": keep_ts(old.get("last_switch_ts")),
                "last_switch_reason": as_str(old.get("last_switch_reason")),
                "switch_ts_24h": recent_ts(old.get("switch_ts_24h"), now, 24),
            }
        else:
            groups_out[g] = {
                "current": as_str(selected.get(g)) or real,
                "since": updated or ts,
                "cand": None,
                "cand_streak": 0,
                "last_switch_ts": None,
                "last_switch_reason": None,
                "switch_ts_24h": [],
            }
    return {
        "version": STATE_VERSION,
        "mode": as_str(raw.get("mode")),
        "selected": selected,
        "planned": as_dict(raw.get("planned")),
        "alive_us": max(0, as_int(raw.get("alive_us"))),
        "alive_sg": max(0, as_int(raw.get("alive_sg"))),
        "alive_any": max(0, as_int(raw.get("alive_any"))),
        "alerted_mode": as_str(raw.get("alerted_mode")),
        "updated": updated,
        "round_ts": keep_ts(raw.get("round_ts")),
        "all_dead_streak": max(0, as_int(raw.get("all_dead_streak"))),
        "groups": groups_out,
        "nodes": {n: dict(v) for n, v in as_dict(raw.get("nodes")).items()
                  if isinstance(n, str) and isinstance(v, dict)},
    }


def update_nodes(nodes: dict, delays: dict[str, int | None], round_ts: str,
                 now: dt.datetime) -> dict:
    """Append this round's one sample per probed node; drop 24h-unseen nodes."""
    out = {n: dict(v) for n, v in as_dict(nodes).items() if isinstance(v, dict)}
    for name, ms in as_dict(delays).items():
        node = dict(out.get(name) or {})
        samples = [s for s in node_samples(node) if s[0] != round_ts]
        samples.append([round_ts, ms if _is_ms(ms) else None])
        node["samples"] = samples[-SAMPLE_KEEP:]
        if _is_ms(ms):
            node["fail_streak"] = 0
            node["last_ok_ts"] = round_ts
        else:
            node["fail_streak"] = max(0, as_int(node.get("fail_streak"), 0)) + 1
        out[name] = node
    for name in list(out):
        samples = node_samples(out[name])
        when = parse_ts(samples[-1][0]) if samples else None
        if when is None or (now - when).total_seconds() > NODE_PRUNE_AGE_H * 3600:
            out.pop(name, None)
    return out


def build_node_stats(nodes: dict, delays: dict[str, int | None]) -> dict:
    stats: dict[str, dict] = {}
    for name in set(as_dict(nodes)) | set(as_dict(delays)):
        node = dict(as_dict(nodes).get(name) or {})
        ms = as_dict(delays).get(name)
        node["delay"] = ms if _is_ms(ms) else None
        node["alive_round"] = node["delay"] is not None
        stats[name] = node
    return stats


def advance_group_state(group_state: dict, real_current: str | None, now: dt.datetime,
                        proposed: str | None = None, switch_reason: str | None = None) -> dict:
    """Reconcile per-group history with what the controller actually runs."""
    out = dict(group_state or {})
    ts = iso(now)
    if out.get("current") != real_current:
        history = recent_ts(out.get("switch_ts_24h"), now, 24) + [ts]
        out["switch_ts_24h"] = history[-20:]
        out["last_switch_ts"] = ts
        out["last_switch_reason"] = switch_reason or "external"
        out["since"] = ts
        out["current"] = real_current
        out["cand"] = None
        out["cand_streak"] = 0
        return out
    if not out.get("since"):
        out["since"] = ts
    if proposed:
        out["cand"], out["cand_streak"] = next_candidate_state(out, proposed)
    else:
        out["cand"] = None
        out["cand_streak"] = 0
    return out


def reprobe_dead_currents(api: str, names: list[str], delays: dict[str, int | None],
                          target: str, timeout_ms: int, token: str | None,
                          retries: int, log: Logger) -> dict[str, int]:
    """Design 2.2: a single timeout must not mark the current node dead."""
    recovered: dict[str, int] = {}
    for name in names:
        if delays.get(name):
            continue
        for attempt in range(max(0, retries)):
            time.sleep(RETRY_DELAY_S)
            ms = request_delay(api, name, target, timeout_ms, token)
            if ms:
                recovered[name] = ms
                log.info(f"retry-current node={name} attempt={attempt + 1} ok delay={ms}")
                break
        else:
            log.warn(f"retry-current node={name} attempts={max(0, retries)} still_dead")
    return recovered


def write_round_rows(path: str, rows: list[dict], log: Logger) -> None:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warn(f"rounds jsonl write failed class={type(exc).__name__.lower()}")


def group_leaf_members(proxies: dict[str, dict], group: str) -> list[str]:
    return collect_candidates(proxies, {group: (proxies.get(group) or {})})


def switch_group(api: str, group: str, name: str, token: str | None, dry_run: bool,
                 log: Logger) -> bool:
    if dry_run:
        log.info(f"dry-run would switch group={group} -> {name}")
        print(f"DRY-RUN\t{group}\t->\t{name}")
        return True
    body = {"name": name}
    url = f"{api}/proxies/{urllib.parse.quote(group, safe='')}"
    try:
        api_json("PUT", url, body=body, token=token, timeout=8)
        log.info(f"switched group={group} -> {name}")
        return True
    except Exception as exc:
        log.warn(f"switch failed group={group} class={type(exc).__name__.lower()}")
        return False


def resolve_receive_id(env: dict[str, str], target_file: str) -> tuple[str | None, str | None]:
    """Return (receive_id, receive_id_type) without inventing ids."""
    for key, rtype in (
        ("FEISHU_RECEIVE_ID", None),
        ("FEISHU_CHAT_ID", "chat_id"),
        ("FEISHU_OPEN_ID", "open_id"),
        ("FEISHU_USER_ID", "user_id"),
    ):
        value = (env.get(key) or "").strip()
        if value:
            if rtype:
                return value, rtype
            if value.startswith("ou_"):
                return value, "open_id"
            if value.startswith("oc_"):
                return value, "chat_id"
            if value.startswith("on_"):
                return value, "union_id"
            return value, "chat_id"
    try:
        data = json.loads(Path(target_file).read_text(encoding="utf-8"))
        rid = (data.get("receive_id") or data.get("chat_id") or "").strip()
        rtype = (data.get("receive_id_type") or data.get("id_type") or "").strip()
        if rid:
            if not rtype:
                rtype = "chat_id" if rid.startswith("oc_") else "open_id"
            return rid, rtype
    except (OSError, ValueError):
        pass
    return None, None


def feishu_token(app_id: str, app_secret: str) -> str | None:
    try:
        payload = api_json(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
            timeout=20,
        )
        if payload.get("code") == 0 and payload.get("tenant_access_token"):
            return payload["tenant_access_token"]
    except Exception:
        return None
    return None


def send_feishu_im(app_id: str, app_secret: str, receive_id: str, receive_id_type: str,
                   text: str, log: Logger) -> bool:
    token = feishu_token(app_id, app_secret)
    if not token:
        log.warn("alert feishu token_error")
        return False
    body = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    url = (
        "https://open.feishu.cn/open-apis/im/v1/messages?"
        + urllib.parse.urlencode({"receive_id_type": receive_id_type})
    )
    try:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if payload.get("code") == 0:
            log.info("alert feishu im ok")
            return True
        log.warn(f"alert feishu im api_error code={payload.get('code')}")
        return False
    except Exception as exc:
        log.warn(f"alert feishu im failed class={type(exc).__name__.lower()}")
        return False


def append_alert_log(path: str, text: str, log: Logger) -> None:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{dt.datetime.now().isoformat(timespec='seconds')}\t{text}\n")
        log.info("alert written to log")
    except OSError:
        log.warn("alert log write failed")


def build_alert_text(mode: str, prev_mode: str | None, selected: dict[str, str | None],
                     alive_us: int, alive_sg: int, alive_any: int) -> str:
    if mode == "A" and prev_mode in {"B", "C"}:
        head = "【代理告警】已恢复美国"
    elif mode == "B":
        head = "【代理告警】美国全挂，已切新加坡"
    elif mode == "C":
        head = "【代理告警】美国+新加坡全挂，GPT/US-FAST 降到日本档，MUSE 不动"
    else:
        head = f"【代理告警】状态 {mode}"
    parts = [
        head,
        f"mode={mode}",
        f"alive_us={alive_us}",
        f"alive_sg={alive_sg}",
        f"alive_any={alive_any}",
    ]
    for g in SWITCH_GROUPS:
        parts.append(f"{g}={selected.get(g) or '(unchanged)'}")
    return " | ".join(parts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mihomo GPT/MUSE/US-FAST auto failover")
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--timeout-ms", type=int, default=7000)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    parser.add_argument("--alert-log", default=DEFAULT_ALERT_LOG)
    parser.add_argument("--alert-target", default=DEFAULT_ALERT_TARGET)
    parser.add_argument("--secret-env", default=r"C:\mihomo-proxy-management\config\subscription.env",
                        help="Optional file with controller SECRET")
    parser.add_argument("--decision-mode", choices=("legacy", "shadow", "hysteresis"),
                        default="hysteresis",
                        help="legacy: old fastest-node behaviour; "
                             "shadow: execute legacy, record new decision only; "
                             "hysteresis: execute the thresholded decision (default)")
    parser.add_argument("--legacy-fastest", dest="decision_mode", action="store_const",
                        const="legacy", help="Alias for --decision-mode legacy (rollback)")
    parser.add_argument("--rounds-jsonl", default=DEFAULT_ROUNDS_JSONL,
                        help="Per-round per-group decision record (not written on --dry-run)")
    parser.add_argument("--switch-margin-ms", type=int, default=300)
    parser.add_argument("--switch-margin-pct", type=int, default=50)
    parser.add_argument("--switch-streak", type=int, default=2)
    parser.add_argument("--min-dwell-min", type=int, default=60)
    parser.add_argument("--retry-current", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def collect_candidates(proxies: dict[str, dict], groups: dict[str, dict]) -> list[str]:
    names: set[str] = set()
    # Only skip other proxy-groups (Selector/URLTest/...), not leaf proxies.
    # Every leaf also appears in GET /proxies keys, so do NOT use proxies.keys().
    group_types = {"selector", "urltest", "fallback", "loadbalance", "relay"}
    for g in SWITCH_GROUPS:
        for member in groups.get(g, {}).get("all") or []:
            if not isinstance(member, str):
                continue
            if member in SKIP_MEMBERS:
                continue
            meta = proxies.get(member) or {}
            ptype = (meta.get("type") or "").lower()
            if ptype in group_types:
                continue
            # Unknown / missing meta: still try delay (API returns null if not a proxy)
            names.add(member)
    return sorted(names)


class GptEntryController:
    """Adapts this round's probe results to the GPT entry protocol.

    The first ``probe`` serves the already measured round (no second sweep);
    ``fresh=True`` is the entry's current-node retry and measures again.
    """

    def __init__(self, api: str, token: str | None, delays: dict[str, int | None],
                 round_ts: str, target: str, timeout_ms: int, dry_run: bool,
                 log: Logger) -> None:
        self.api = api
        self.token = token
        self.delays = dict(delays)
        self.round_ts = round_ts
        self.target = target
        self.timeout_ms = timeout_ms
        self.dry_run = dry_run
        self.log = log
        self._pretend: str | None = None

    def read_group(self, group: str):
        payload = api_json("GET", f"{self.api}/proxies", token=self.token, timeout=8)
        entry = (payload.get("proxies") or {}).get(group) or {}
        return gpt_switch_entry.GroupView(
            current=entry.get("now"),
            engine=(entry.get("type") or "").lower(),
            members=tuple(entry.get("all") or ()),
        )

    def probe(self, names, fresh: bool = False):
        nodes = []
        for name in names:
            if fresh:
                ms = request_delay(self.api, name, self.target, self.timeout_ms, self.token)
                self.delays[name] = ms
            else:
                ms = self.delays.get(name)
            nodes.append(NodeSnapshot(
                name=name,
                region=policy_region(name),
                alive=ms is not None,
                delay_ms=ms,
                measured_at=self.round_ts,
                readback_ok=True,
                source="round",
            ))
        return gpt_switch_entry.ProbeRound(nodes=tuple(nodes), round_ok=True)

    def put(self, group: str, name: str) -> None:
        if self.dry_run:
            self.log.info(f"dry-run would switch group={group} -> {name}")
            self._pretend = name
            return
        api_json("PUT", f"{self.api}/proxies/{urllib.parse.quote(group, safe='')}",
                 body={"name": name}, token=self.token, timeout=8)

    def selected(self, group: str) -> str | None:
        if self._pretend is not None:
            return self._pretend
        payload = api_json("GET", f"{self.api}/proxies", token=self.token, timeout=8)
        return ((payload.get("proxies") or {}).get(group) or {}).get("now")


class _MemoryStore:
    """Dry-run store: keeps the round in memory so --dry-run writes nothing."""

    def __init__(self) -> None:
        self.payload: dict = {}

    def load(self) -> dict:
        return self.payload

    def save(self, payload: dict) -> None:
        self.payload = payload


def _run_gpt_entry(args, api: str, token: str | None, delays: dict[str, int | None],
                   round_ts: str, log: Logger):
    """Run the only GPT execution path; never lets an exception abort the round."""
    controller = GptEntryController(
        api, token, delays, round_ts, args.target, args.timeout_ms, args.dry_run, log)
    if args.dry_run:
        store = _MemoryStore()
    else:
        store = gpt_switch_entry.JsonStore(
            str(Path(args.state_file).with_name("gpt-stability-policy.json")))
    try:
        result = gpt_switch_entry.run_once(
            "GPT", controller, store, dt.datetime.now,
            named_mutex_lock.named_mutex_lock_factory, PolicyConfig())
    except Exception as exc:
        log.error(f"gpt-entry failed class={type(exc).__name__.lower()}")
        return gpt_switch_entry.EntryResult(
            "GPT", "unknown", "unknown", None, None, "entry_failed", False, None)
    log.info(
        f"gpt-entry result={result.result} action={result.action} "
        f"reason={result.reason} current={result.current} target={result.target} "
        f"put={result.put} readback={result.readback}"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = parse_args(argv)
    log = Logger(args.log_file, verbose=args.verbose)
    log.info("failover start")
    is_legacy = args.decision_mode == "legacy"
    if not is_legacy:
        log.info(f"decision-mode={args.decision_mode}")

    params = SwitchParams(
        switch_margin_ms=args.switch_margin_ms,
        switch_margin_pct=args.switch_margin_pct,
        switch_streak=args.switch_streak,
        min_dwell_min=args.min_dwell_min,
        retry_current=args.retry_current,
    )

    secret_env = read_env_file(args.secret_env)
    token = (secret_env.get("SECRET") or "").strip() or None
    api = args.controller.rstrip("/")

    try:
        payload = api_json("GET", f"{api}/proxies", token=token, timeout=8)
    except Exception as exc:
        log.error(f"controller unreachable class={type(exc).__name__.lower()}")
        print("ERROR\tcontroller_unreachable")
        return 1

    proxies = payload.get("proxies") or {}
    groups: dict[str, dict] = {}
    for g in SWITCH_GROUPS:
        entry = proxies.get(g)
        if not entry:
            log.error(f"missing group={g}")
            print(f"ERROR\tmissing_group\t{g}")
            return 1
        groups[g] = entry
        gtype = (entry.get("type") or "").lower()
        if gtype not in {"selector", "fallback"}:
            log.info(f"skip fallback group={g} type={entry.get('type')} "
                     "(not Selector/Fallback)")

    candidates = collect_candidates(proxies, groups)
    log.info(f"candidates={len(candidates)}")

    delays: dict[str, int | None] = {}
    if candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(
                pool.map(
                    lambda n: (n, request_delay(api, n, args.target, args.timeout_ms, token)),
                    candidates,
                )
            )
        delays = dict(results)

    now = dt.datetime.now()
    round_ts = iso(now)

    # Legacy never re-probes, so its rollback path stays identical to the old
    # script. shadow/hysteresis re-probe the current nodes first (design 2.2)
    # and only then classify the round: the mode, alive_* counts and the
    # all-dead streak must reflect the final probe result, otherwise a round
    # where everything timed out but a retry recovered a node would be counted
    # as all-dead and could raise a C alert on the second such round (design 3.5).
    probe: dict[str, int | None] = dict(delays)
    if not is_legacy:
        dead_currents = sorted({
            groups[g].get("now") for g in SWITCH_GROUPS
            if groups[g].get("now") and not delays.get(groups[g].get("now"))
        })
        if dead_currents and params.retry_current > 0:
            probe.update(
                reprobe_dead_currents(
                    api, dead_currents, delays, args.target, args.timeout_ms,
                    token, params.retry_current, log,
                )
            )
    delays_round = probe

    # CY-GUS-DEV1: GPT is decided and executed only by the unified entry. It
    # runs for every decision-mode, so the legacy/shadow fastest-node plan can
    # never move GPT again. MUSE/US-FAST keep the code below unchanged.
    gpt_entry = _run_gpt_entry(args, api, token, delays_round, round_ts, log)

    alive_us = [n for n, d in probe.items() if d and region_of(n) == "US"]
    alive_sg = [n for n, d in probe.items() if d and region_of(n) == "SG"]
    alive_any = [n for n, d in probe.items() if d]
    mode = decide_mode(alive_us, alive_sg)
    log.info(
        f"mode={mode} alive_us={len(alive_us)} alive_sg={len(alive_sg)} "
        f"alive_any={len(alive_any)}"
    )

    plan = plan_switches(
        mode, groups, probe, set(alive_us), set(alive_sg), set(alive_any)
    )

    prev: dict = {}
    node_stats: dict[str, dict] = {}
    nodes_hist: dict[str, dict] = {}
    decisions: dict[str, dict] = {}
    group_states: dict[str, dict] = {}
    if not is_legacy:
        # The thresholded decision needs the history before it can decide.
        prev = load_state(args.state_file)
        nodes_hist = update_nodes(as_dict(prev.get("nodes")), delays_round, round_ts, now)
        node_stats = build_node_stats(nodes_hist, delays_round)
        group_states = migrate_state(
            prev, {g: groups[g].get("now") for g in SWITCH_GROUPS}, now
        )["groups"]
        decisions["GPT"] = {
            "decision": gpt_entry.action,
            "reason": gpt_entry.reason,
            "current": gpt_entry.current,
            "target": gpt_entry.target,
            "cur_ms": None,
            "cur_smooth": None,
            "best": gpt_entry.target,
            "best_ms": None,
            "best_smooth": None,
            "need_margin_ms": None,
            "cand": None,
            "cand_streak": 0,
            "dwell_min": None,
        }
        for g in ("MUSE", "US-FAST"):
            engine = (groups[g].get("type") or "").lower()
            if engine not in {"selector", "fallback"}:
                det = {"decision": "skip", "reason": "skip_unmanaged_type",
                       "current": groups[g].get("now"), "target": None}
                decisions[g] = det
                log.info(f"skip fallback group={g} type={groups[g].get('type')}")
                continue
            det: dict[str, Any] = {}
            target, reason = decide_group(
                g, groups[g].get("now"), group_leaf_members(proxies, g),
                node_stats, group_states[g], now, params, det, engine=engine,
            )
            det["target"] = target
            det["reason"] = reason
            decisions[g] = det
            log.info(
                f"decide group={g} decision={det['decision']} reason={reason} "
                f"current={det.get('current')} target={target} "
                f"cur_smooth={det.get('cur_smooth')} best={det.get('best')} "
                f"best_smooth={det.get('best_smooth')} need_margin_ms={det.get('need_margin_ms')} "
                f"cand={det.get('cand')} cand_streak={det.get('cand_streak')} "
                f"dwell_min={det.get('dwell_min')}"
            )
            if det.get("muse_frozen_dead"):
                log.warn(f"muse_frozen_dead group=MUSE current={det.get('current')}")

    if is_legacy or args.decision_mode == "shadow":
        executed = dict(plan)
    else:
        executed = {g: decisions[g]["target"] for g in SWITCH_GROUPS}
    # GPT was already handled by the entry above: never PUT it again here, and
    # never let the legacy/shadow fastest-node plan touch it.
    executed["GPT"] = None
    shown_delays = delays_round

    applied: dict[str, str | None] = {}
    for g, desired in executed.items():
        current = groups[g].get("now")
        reason = (decisions.get(g) or {}).get("reason")
        if desired is None:
            applied[g] = None
            print(f"PLAN\t{g}\tunchanged\tnow={current}")
            log.info(f"plan group={g} unchanged now={current}"
                     + ("" if is_legacy else f" reason={reason}"))
            continue
        if desired == current:
            applied[g] = desired
            print(f"PLAN\t{g}\talready\t{desired}")
            log.info(f"already group={g} node={desired}"
                     + ("" if is_legacy else f" reason={reason}"))
            continue
        print(f"PLAN\t{g}\t{current}\t->\t{desired}\tdelay={shown_delays.get(desired)}")
        log.info(f"plan group={g} {current} -> {desired} delay={shown_delays.get(desired)}"
                 + ("" if is_legacy else f" reason={reason}"))
        ok = switch_group(api, g, desired, token, args.dry_run, log)
        applied[g] = desired if ok else None

    nows: dict[str, str | None] = {}
    if args.dry_run:
        for g in SWITCH_GROUPS:
            nows[g] = applied[g] if applied[g] is not None else groups[g].get("now")
    else:
        try:
            fresh = api_json("GET", f"{api}/proxies", token=token, timeout=8).get("proxies") or {}
            for g in SWITCH_GROUPS:
                nows[g] = (fresh.get(g) or {}).get("now")
        except Exception:
            for g in SWITCH_GROUPS:
                nows[g] = applied[g] if applied[g] is not None else groups[g].get("now")

    if is_legacy:
        # The old script read the state here, after the switches and the
        # read-back; that timing is part of the legacy equivalence.
        prev = load_state(args.state_file)

    prev_mode = prev.get("mode")
    prev_alerted = prev.get("alerted_mode")

    should_alert = False
    if mode in {"B", "C"} and prev_alerted != mode:
        should_alert = True
    if mode == "A" and prev_mode in {"B", "C"} and prev_alerted != "A_recovered":
        should_alert = True

    # Design 3.5: a round with zero alive nodes is probe-suspect; freeze and do
    # not alert on the first such round.
    all_dead_streak = max(0, as_int(prev.get("all_dead_streak"), 0))
    if not is_legacy:
        if not alive_any:
            all_dead_streak += 1
            if args.decision_mode == "hysteresis" and all_dead_streak < 2 and should_alert:
                should_alert = False
                log.warn(f"probe_suspect alive_any=0 streak={all_dead_streak} alert_suppressed")
        else:
            all_dead_streak = 0

    alert_text = build_alert_text(
        mode, prev_mode, {g: nows.get(g) for g in SWITCH_GROUPS},
        len(alive_us), len(alive_sg), len(alive_any),
    )

    alerted_mode = prev_alerted
    if should_alert and not args.dry_run:
        env = read_env_file(args.env_file)
        app_id = (env.get("FEISHU_APP_ID") or "").strip()
        app_secret = (env.get("FEISHU_APP_SECRET") or "").strip()
        receive_id, receive_id_type = resolve_receive_id(env, args.alert_target)
        sent = False
        if app_id and app_secret and receive_id and receive_id_type:
            sent = send_feishu_im(app_id, app_secret, receive_id, receive_id_type, alert_text, log)
        if not sent:
            append_alert_log(args.alert_log, alert_text, log)
        alerted_mode = "A_recovered" if mode == "A" else mode
    elif should_alert and args.dry_run:
        print(f"DRY-RUN-ALERT\t{alert_text}")
        log.info("dry-run alert suppressed")

    state: dict[str, Any] = {
        "mode": mode,
        "selected": {g: nows.get(g) for g in SWITCH_GROUPS},
        "planned": executed,
        "alive_us": len(alive_us),
        "alive_sg": len(alive_sg),
        "alive_any": len(alive_any),
        "alerted_mode": alerted_mode,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if not is_legacy:
        for g in SWITCH_GROUPS:
            det = decisions[g]
            real = nows.get(g)
            changed = group_states[g].get("current") != real
            group_states[g] = advance_group_state(
                group_states[g], real, now,
                proposed=None if changed else det.get("cand"),
                switch_reason=det.get("reason"),
            )
        state.update({
            "version": STATE_VERSION,
            "round_ts": round_ts,
            "all_dead_streak": all_dead_streak,
            "groups": group_states,
            "nodes": nodes_hist,
            "decisions": {
                g: {"target": decisions[g]["target"], "reason": decisions[g]["reason"]}
                for g in SWITCH_GROUPS
            },
        })
    if not args.dry_run:
        if not is_legacy:
            write_round_rows(args.rounds_jsonl, [
                {
                    "ts": round_ts,
                    "group": g,
                    "mode": mode,
                    "current": decisions[g].get("current"),
                    "cur_ms": decisions[g].get("cur_ms"),
                    "cur_smooth": decisions[g].get("cur_smooth"),
                    "best": decisions[g].get("best"),
                    "best_ms": decisions[g].get("best_ms"),
                    "best_smooth": decisions[g].get("best_smooth"),
                    "need_margin_ms": decisions[g].get("need_margin_ms"),
                    "cand": decisions[g].get("cand"),
                    "cand_streak": decisions[g].get("cand_streak"),
                    "dwell_min": decisions[g].get("dwell_min"),
                    "decision": decisions[g]["decision"],
                    "reason": decisions[g]["reason"],
                    "alive_us": len(alive_us),
                    "alive_sg": len(alive_sg),
                    "alive_any": len(alive_any),
                    "exec_mode": args.decision_mode,
                    "executed": executed.get(g),
                }
                for g in SWITCH_GROUPS
            ], log)
        save_state(args.state_file, state)
    else:
        log.info("dry-run: state not written")

    print(
        f"RESULT\tmode={mode}\talive_us={len(alive_us)}\talive_sg={len(alive_sg)}\t"
        f"alive_any={len(alive_any)}"
    )
    for g in SWITCH_GROUPS:
        print(f"NOW\t{g}\t{nows.get(g)}")
        log.info(f"now group={g} node={nows.get(g)}")
    log.info("failover done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

