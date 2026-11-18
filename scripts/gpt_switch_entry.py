"""Thin GPT switch entry (CY-GUS-DEV1).

The entry owns everything the pure policy cannot do: serialising runs with a
named lock, suppressing an identical decision inside a short window, checking
the controller's selection right before a PUT, forcing a read-back afterwards,
and persisting the round history.

It deliberately has no network code of its own. A ``controller`` object is
injected with four small methods::

    read_group(group) -> GroupView(current, engine, members)
    probe(names, fresh=False) -> ProbeRound(nodes, round_ok)
    put(group, name) -> None            # raises on failure
    selected(group) -> str | None

``fresh=True`` is the current-node retry path; it asks the controller to
measure again instead of reusing the round's first result.

The lock mirrors ``sync-feishu-bitable.py``'s ``writer_mutex_name()`` shape: a
single named mutex name is exposed and a factory is injected. Offline (and in
tests) ``serial_lock_factory`` is the fake implementation; deployment injects
a real ``Local\\...`` named-mutex factory.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from gpt_stability_policy import (
    ACTION_SWITCH_SAME_REGION,
    Decision,
    NodeSnapshot,
    PolicyConfig,
    RoundSnapshot,
    Sample,
    decide,
    iso,
    parse_ts,
)

LOCK_NAME = r"Local\mihomo-gpt-stability-switch"
SUPPORTED_GROUP = "GPT"
SUPPORTED_ENGINE = "selector"
STATE_VERSION = 1


class LockBusy(RuntimeError):
    """Another run owns the named lock."""


@dataclass(frozen=True)
class GroupView:
    current: str | None
    engine: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class ProbeRound:
    nodes: tuple[NodeSnapshot, ...]
    round_ok: bool = True


@dataclass(frozen=True)
class EntryResult:
    group: str
    result: str
    action: str
    current: str | None
    target: str | None
    reason: str
    put: bool
    readback: str | None
    region: str | None = None


def named_lock_name() -> str:
    """One named mutex for every GPT policy writer, like writer_mutex_name()."""
    return LOCK_NAME


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _serial_lock(name: str):
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(name, threading.Lock())
    if not lock.acquire(timeout=30):
        raise LockBusy(name)
    try:
        yield
    finally:
        lock.release()


def serial_lock_factory(name: str = LOCK_NAME):
    """Offline fake of the named mutex: process-wide mutual exclusion."""
    return _serial_lock(name)


class JsonStore:
    """Minimal atomic JSON store; the only persistence the entry needs."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


def _history_from(payload: Mapping[str, Any]) -> dict[str, tuple[Sample, ...]]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, Mapping):
        return {}
    out: dict[str, tuple[Sample, ...]] = {}
    for name, rows in nodes.items():
        if not isinstance(name, str) or not isinstance(rows, list):
            continue
        samples: list[Sample] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            measured_at = row.get("measured_at")
            if not isinstance(measured_at, str) or not measured_at:
                continue
            delay = row.get("delay_ms")
            if isinstance(delay, bool) or not isinstance(delay, int):
                delay = None
            samples.append(Sample(measured_at, bool(row.get("alive")), delay))
        out[name] = tuple(samples)
    return out


def _history_payload(history: Mapping[str, Sequence[Sample]],
                     policy: PolicyConfig) -> dict[str, list[dict]]:
    keep = max(1, policy.sample_keep)
    return {
        name: [
            {"measured_at": sample.measured_at, "alive": bool(sample.alive),
             "delay_ms": sample.delay_ms}
            for sample in list(samples)[-keep:]
        ]
        for name, samples in history.items()
    }


def _merge_round(history: Mapping[str, Sequence[Sample]],
                 nodes: Sequence[NodeSnapshot], policy: PolicyConfig
                 ) -> dict[str, list[Sample]]:
    merged: dict[str, list[Sample]] = {
        name: list(samples) for name, samples in history.items()}
    keep = max(1, policy.sample_keep)
    for node in nodes:
        rows = merged.setdefault(node.name, [])
        sample = Sample(node.measured_at, bool(node.alive and node.readback_ok),
                        node.delay_ms)
        if rows and rows[-1].measured_at == node.measured_at:
            rows[-1] = sample
        else:
            rows.append(sample)
        merged[node.name] = rows[-keep:]
    return merged


def _decision_key(decision: Decision) -> tuple:
    return (decision.action, decision.current, decision.target, decision.region)


def _duplicate(last: object, decision: Decision, now: datetime,
               policy: PolicyConfig) -> bool:
    """True only for the *same* decision inside the dedup window.

    A recorded hold never blocks a later dead-current switch: the action is
    part of the key, so a death inside the window is still allowed to move.
    """
    if not isinstance(last, Mapping):
        return False
    when = parse_ts(last.get("at"))
    if when is None:
        return False
    age = (now - when).total_seconds()
    if age < 0 or age >= policy.decision_dedup_s:
        return False
    stored = (last.get("action"), last.get("current"), last.get("target"),
              last.get("region"))
    return stored == _decision_key(decision)


def _probe_with_retry(controller, view: GroupView, policy: PolicyConfig):
    round_ = controller.probe(view.members)
    nodes = list(round_.nodes)
    if not round_.round_ok or policy.retry_current <= 0 or not view.current:
        return round_, nodes
    cur = next((node for node in nodes if node.name == view.current), None)
    if cur is not None and cur.alive and cur.readback_ok:
        return round_, nodes
    for _ in range(max(0, policy.retry_current)):
        try:
            extra = controller.probe([view.current], fresh=True)
        except Exception:
            break
        revived = next((node for node in extra.nodes if node.name == view.current), None)
        if revived is None:
            continue
        nodes = [revived if node.name == view.current else node for node in nodes]
        if revived.alive and revived.readback_ok:
            break
    return round_, nodes


def _save(store, history: Mapping[str, Sequence[Sample]],
          last_decision: Mapping[str, Any] | None,
          policy: PolicyConfig) -> None:
    if store is None:
        return
    payload: dict[str, Any] = {
        "version": STATE_VERSION,
        "nodes": _history_payload(history, policy),
    }
    if last_decision is not None:
        payload["last_decision"] = dict(last_decision)
    store.save(payload)


def _record(decision: Decision, now: datetime, result: str,
            readback: str | None, reason: str | None = None) -> dict:
    return {
        "at": iso(now),
        "action": decision.action,
        "current": decision.current,
        "target": decision.target,
        "region": decision.region,
        "reason": reason or decision.reason,
        "result": result,
        "readback": readback,
    }


def _rejected(group: str, current: str | None, reason: str) -> EntryResult:
    return EntryResult(group, "rejected", "rejected", current, None, reason,
                       False, None)


def _run_locked(group: str, controller, store, clock, policy: PolicyConfig
                ) -> EntryResult:
    now = clock()
    payload = store.load() if store is not None else {}
    history = _history_from(payload)
    last = payload.get("last_decision")

    view = controller.read_group(group)
    engine = (view.engine or "").lower()
    if engine != SUPPORTED_ENGINE:
        return _rejected(group, view.current, "engine_not_selector")

    round_, nodes = _probe_with_retry(controller, view, policy)
    snapshot = RoundSnapshot(view.current, tuple(nodes), round_.round_ok)
    decision = decide(snapshot, history, policy, now)

    if _duplicate(last, decision, now, policy):
        merged = _merge_round(history, snapshot.nodes, policy)
        _save(store, merged, last, policy)
        blocked = isinstance(last, Mapping) and last.get("result") == "unknown"
        return EntryResult(
            group,
            "unknown" if blocked else "deduped",
            decision.action,
            decision.current,
            decision.target,
            "retry_blocked" if blocked else "deduped",
            False,
            None,
            decision.region,
        )

    result = decision.action
    reason = decision.reason
    put = False
    readback: str | None = None

    if decision.action == ACTION_SWITCH_SAME_REGION and decision.target:
        before = controller.selected(group)
        if before != decision.current:
            # Someone moved the group between our probe and our write.
            result, reason = "aborted", "current_changed_before_put"
        else:
            try:
                controller.put(group, decision.target)
                put = True
            except Exception:
                result, readback = "unknown", "put_failed"
            else:
                after = controller.selected(group)
                if after == decision.target:
                    result, readback = "switched", "match"
                else:
                    result, readback = "unknown", "mismatch"

    merged = _merge_round(history, snapshot.nodes, policy)
    record = _record(decision, now, result, readback, reason)
    _save(store, merged, record, policy)

    return EntryResult(group, result, decision.action, decision.current,
                       decision.target, reason, put, readback, decision.region)


def run_once(group: str, controller, store, clock, lock_factory,
             policy: PolicyConfig | None = None) -> EntryResult:
    """Run one GPT decision round. Only ``group == "GPT"`` is accepted."""
    policy = policy or PolicyConfig()
    if group != SUPPORTED_GROUP:
        return _rejected(group, None, "group_not_supported")
    try:
        lock = lock_factory(named_lock_name())
    except LockBusy:
        return EntryResult(group, "busy", "busy", None, None, "lock_busy",
                           False, None)
    try:
        with lock:
            return _run_locked(group, controller, store, clock, policy)
    except LockBusy:
        return EntryResult(group, "busy", "busy", None, None, "lock_busy",
                           False, None)
