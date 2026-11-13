"""Unified GPT stability policy (CY-GUS-DEV1) -- pure decision, no I/O.

The policy is the only place that decides whether the GPT group should move.
It never reads the controller, never writes state and never looks at a clock
other than the ``now`` it is handed, so every rule is directly testable.

Design rules (REV1):

* ``source`` is audit metadata only. It is never used for filtering, ranking
  or any threshold, so swapping every source label cannot change a decision.
* A healthy current node always holds. A faster same-region peer, a recovered
  other region or a revived current node never cause a move.
* A dead current node only moves to a *stable* candidate in its own region.
  Stability means ``stable_rounds`` consecutive alive rounds.
* Cross-region candidates never move on their own: the decision is
  ``needs_confirmation`` and carries no target, so the caller cannot PUT.
* An unknown region, a missing current node or a suspect probe round is never
  guessed around: the policy holds (or reports ``probe_suspect``).
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence

UNKNOWN_REGION = "UNKNOWN"
MISSING_DELAY = 10 ** 12

ACTION_HOLD = "hold"
ACTION_SWITCH_SAME_REGION = "switch_same_region"
ACTION_NEEDS_CONFIRMATION = "needs_confirmation"
ACTION_PROBE_SUSPECT = "probe_suspect"

ACTIONS = frozenset({
    ACTION_HOLD,
    ACTION_SWITCH_SAME_REGION,
    ACTION_NEEDS_CONFIRMATION,
    ACTION_PROBE_SUSPECT,
})


@dataclass(frozen=True)
class NodeSnapshot:
    """One node measured in one round.

    ``source`` is audit only; see the module docstring.
    """

    name: str
    region: str | None
    alive: bool
    delay_ms: int | None
    measured_at: str
    readback_ok: bool = True
    source: str = ""


@dataclass(frozen=True)
class Sample:
    """A persisted historical observation, one per measured round."""

    measured_at: str
    alive: bool
    delay_ms: int | None = None


@dataclass(frozen=True)
class RoundSnapshot:
    """Everything one round knows about the GPT group."""

    current: str | None
    nodes: Sequence[NodeSnapshot]
    round_ok: bool = True


@dataclass(frozen=True)
class PolicyConfig:
    stable_rounds: int = 10
    sample_keep: int = 12
    sample_smooth_max: int = 5
    sample_max_age_h: int = 6
    retry_current: int = 2
    decision_dedup_s: int = 180


@dataclass(frozen=True)
class Decision:
    action: str
    current: str | None
    target: str | None
    region: str | None
    reason: str


def parse_ts(value: object) -> dt.datetime | None:
    """Parse a stored timestamp to naive local time; anything else is None.

    Mirrors the helper in ``proxy-auto-switch.py``: a tz-aware value is
    normalised instead of trusted, so it can never raise against the naive
    wall clock the callers use.
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


def region_key(region: object) -> str | None:
    """Canonical region label, or None when the region is not known."""
    if not isinstance(region, str):
        return None
    text = region.strip()
    if not text:
        return None
    if text.upper() in {UNKNOWN_REGION, "NONE", "NULL", "?", "未知"}:
        return None
    return text.upper()


def iso(when: dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


def _series(history: Mapping[str, Sequence[Sample]], node: NodeSnapshot,
            policy: PolicyConfig) -> list[Sample]:
    """History for *node* with this round folded in.

    A repeated ``measured_at`` replaces the previous observation of the same
    round instead of appending a new one, so a retry inside one round never
    inflates the consecutive-round count.
    """
    prior = list(history.get(node.name, ()) or ())
    sample = Sample(node.measured_at, bool(node.alive and node.readback_ok),
                    node.delay_ms)
    if prior and prior[-1].measured_at == node.measured_at:
        prior[-1] = sample
    else:
        prior.append(sample)
    keep = max(1, policy.sample_keep)
    return prior[-keep:]


def stable_streak(history: Mapping[str, Sequence[Sample]], node: NodeSnapshot,
                  policy: PolicyConfig) -> int:
    """Consecutive alive rounds ending with this round (deduped by timestamp)."""
    streak = 0
    for sample in reversed(_series(history, node, policy)):
        if not sample.alive:
            break
        streak += 1
        if streak >= max(1, policy.stable_rounds):
            break
    return streak


def smoothed_delay(history: Mapping[str, Sequence[Sample]], node: NodeSnapshot,
                   policy: PolicyConfig, now: dt.datetime) -> int | None:
    """Median of the recent alive delays; used only to rank same-region peers."""
    values: list[int] = []
    for sample in _series(history, node, policy):
        if not sample.alive:
            continue
        if not isinstance(sample.delay_ms, int) or isinstance(sample.delay_ms, bool):
            continue
        if sample.delay_ms <= 0:
            continue
        when = parse_ts(sample.measured_at)
        if when is None:
            continue
        if (now - when).total_seconds() > policy.sample_max_age_h * 3600:
            continue
        values.append(sample.delay_ms)
    values = values[-max(1, policy.sample_smooth_max):]
    if not values:
        return None
    return int(statistics.median(values))


def _rank(history: Mapping[str, Sequence[Sample]], node: NodeSnapshot,
          policy: PolicyConfig, now: dt.datetime) -> tuple[int, str]:
    value = smoothed_delay(history, node, policy, now)
    return (MISSING_DELAY if value is None else value, node.name)


def decide(snapshot: RoundSnapshot, history: Mapping[str, Sequence[Sample]],
           policy: PolicyConfig, now: dt.datetime) -> Decision:
    """Pure GPT decision. No network, no mutation, no clock reads."""
    current = snapshot.current
    nodes = tuple(snapshot.nodes or ())

    if not snapshot.round_ok or not nodes:
        return Decision(ACTION_PROBE_SUSPECT, current, None, None, "probe_suspect")

    if not any(node.alive and node.readback_ok for node in nodes):
        return Decision(ACTION_PROBE_SUSPECT, current, None, None,
                        "probe_suspect_all_dead")

    if not current:
        return Decision(ACTION_HOLD, current, None, None, "hold_current_missing")

    by_name = {node.name: node for node in nodes}
    cur = by_name.get(current)
    if cur is None:
        return Decision(ACTION_HOLD, current, None, None, "hold_current_missing")

    cur_region = region_key(cur.region)
    if cur_region is None:
        return Decision(ACTION_HOLD, current, None, None, "hold_region_unknown")

    if not cur.readback_ok:
        return Decision(ACTION_HOLD, current, None, None, "hold_readback_unverified")

    if cur.alive:
        return Decision(ACTION_HOLD, current, None, cur_region, "hold_current_healthy")

    same: list[NodeSnapshot] = []
    cross: list[tuple[NodeSnapshot, str]] = []
    unstable_same = False
    for node in nodes:
        if node.name == current or not node.alive or not node.readback_ok:
            continue
        region = region_key(node.region)
        if region is None:
            continue
        if region == cur_region:
            if stable_streak(history, node, policy) >= policy.stable_rounds:
                same.append(node)
            else:
                unstable_same = True
        elif stable_streak(history, node, policy) >= policy.stable_rounds:
            cross.append((node, region))

    if same:
        target = min(same, key=lambda n: _rank(history, n, policy, now))
        return Decision(ACTION_SWITCH_SAME_REGION, current, target.name, cur_region,
                        "switch_same_region")

    if cross:
        _node, region = min(cross, key=lambda item: _rank(history, item[0], policy, now))
        return Decision(ACTION_NEEDS_CONFIRMATION, current, None, region,
                        "needs_confirmation_cross_region")

    if unstable_same:
        return Decision(ACTION_HOLD, current, None, cur_region,
                        "hold_same_region_unstable")

    return Decision(ACTION_HOLD, current, None, cur_region, "hold_no_candidate")
