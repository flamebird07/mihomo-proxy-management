#!/usr/bin/env python3
"""Proxy auto-failover for mihomo select groups GPT / MUSE / US-FAST.

Runs after sync-feishu-bitable.py in the half-hour pipeline. Measures ChatGPT
trace delay via the live controller, classifies US vs SG from node names
(same heuristics as sync-feishu-bitable.py), then applies:

  A  >=1 US alive  -> GPT, MUSE, US-FAST each to fastest US in that group's all
  B  no US, SG ok  -> GPT + US-FAST to fastest SG; MUSE unchanged
  C  US+SG dead    -> GPT + US-FAST to any-region fastest alive; MUSE unchanged
                     (GPT also falls back so ChatGPT stays reachable)

State file debounce: same mode does not re-alert. Alerts prefer Feishu IM when
FEISHU_RECEIVE_ID / FEISHU_CHAT_ID is set; otherwise append alert log.

Secrets (FEISHU_APP_SECRET, tokens, URLs) are never printed or logged.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONTROLLER = "http://127.0.0.1:9090"
DEFAULT_TARGET = "https://chatgpt.com/cdn-cgi/trace"
DEFAULT_ENV_FILE = r"C:\Users\Administrator\.hermes\.env"
DEFAULT_STATE_FILE = r"C:\mihomo\state\proxy-failover-state.json"
DEFAULT_LOG_FILE = r"C:\mihomo\logs\proxy-failover.log"
DEFAULT_ALERT_LOG = r"C:\mihomo\logs\proxy-failover-alert.log"
DEFAULT_ALERT_TARGET = r"C:\mihomo\feishu-alert-target.json"
SWITCH_GROUPS = ("GPT", "MUSE", "US-FAST")
SKIP_MEMBERS = {"DIRECT", "REJECT", "COMPATIBLE", "PASS", "GLOBAL"}

SG_PATTERN = re.compile(r"新加坡|狮城|獅城|singapore|🇸🇬|\bSGP?\b", re.IGNORECASE)
US_PATTERN = re.compile(
    r"美国|美國|united states|\bUSA?\b|🇺🇸|los angeles|san jose|seattle|dallas|chicago|"
    r"new york|ashburn|silicon valley|phoenix|miami|atlanta|las vegas|portland|denver|"
    r"virginia|ohio|california|圣何塞|洛杉矶|西雅图|达拉斯|芝加哥|纽约|硅谷|凤凰城",
    re.IGNORECASE,
)


class Logger:
    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, path: str, verbose: bool = False) -> None:
        self.path = Path(path)
        self.verbose = verbose
        self._ready = False

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
                except OSError:
                    pass
        except OSError:
            pass

    def _write(self, level: str, message: str) -> None:
        line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {level} {message}"
        self._open()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
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


def region_of(name: str) -> str | None:
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
    query = urllib.parse.urlencode({"url": target, "timeout": str(timeout_ms)})
    endpoint = f"{api}/proxies/{urllib.parse.quote(name, safe='')}/delay?{query}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(endpoint, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=(timeout_ms / 1000) + 5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        value = int(payload.get("delay") or 0)
        return value if value > 0 else None
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
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


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


def plan_switches(mode: str, groups: dict[str, dict], delays: dict[str, int | None],
                  alive_us: set[str], alive_sg: set[str],
                  alive_any: set[str]) -> dict[str, str | None]:
    """Return desired node per group. None means leave unchanged."""
    plan: dict[str, str | None] = {g: None for g in SWITCH_GROUPS}

    def us_pick(group: str) -> str | None:
        members = set(groups.get(group, {}).get("all") or [])
        return pick_fastest([n for n in alive_us if n in members], delays)

    def sg_pick(group: str) -> str | None:
        members = set(groups.get(group, {}).get("all") or [])
        return pick_fastest([n for n in alive_sg if n in members], delays)

    def any_pick(group: str) -> str | None:
        members = set(groups.get(group, {}).get("all") or [])
        return pick_fastest([n for n in alive_any if n in members], delays)

    if mode == "A":
        for g in SWITCH_GROUPS:
            plan[g] = us_pick(g)
    elif mode == "B":
        # MUSE unchanged on US outage so muse.ai (routed via MUSE) stays put.
        plan["GPT"] = sg_pick("GPT")
        plan["US-FAST"] = sg_pick("US-FAST")
        plan["MUSE"] = None
    else:
        # Mode C: also switch GPT to any-region fastest so ChatGPT stays up.
        plan["GPT"] = any_pick("GPT")
        plan["US-FAST"] = any_pick("US-FAST")
        plan["MUSE"] = None
    return plan


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
        head = "【代理告警】美国+新加坡全挂，已切任意最快"
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


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = parse_args(argv)
    log = Logger(args.log_file, verbose=args.verbose)
    log.info("failover start")

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

    alive_us = [n for n, d in delays.items() if d and region_of(n) == "US"]
    alive_sg = [n for n, d in delays.items() if d and region_of(n) == "SG"]
    alive_any = [n for n, d in delays.items() if d]
    mode = decide_mode(alive_us, alive_sg)
    log.info(
        f"mode={mode} alive_us={len(alive_us)} alive_sg={len(alive_sg)} "
        f"alive_any={len(alive_any)}"
    )

    plan = plan_switches(
        mode, groups, delays, set(alive_us), set(alive_sg), set(alive_any)
    )

    applied: dict[str, str | None] = {}
    for g, desired in plan.items():
        current = groups[g].get("now")
        if desired is None:
            applied[g] = None
            print(f"PLAN\t{g}\tunchanged\tnow={current}")
            continue
        if desired == current:
            applied[g] = desired
            print(f"PLAN\t{g}\talready\t{desired}")
            log.info(f"already group={g} node={desired}")
            continue
        print(f"PLAN\t{g}\t{current}\t->\t{desired}\tdelay={delays.get(desired)}")
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

    prev = load_state(args.state_file)
    prev_mode = prev.get("mode")
    prev_alerted = prev.get("alerted_mode")

    should_alert = False
    if mode in {"B", "C"} and prev_alerted != mode:
        should_alert = True
    if mode == "A" and prev_mode in {"B", "C"} and prev_alerted != "A_recovered":
        should_alert = True

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

    state = {
        "mode": mode,
        "selected": {g: nows.get(g) for g in SWITCH_GROUPS},
        "planned": plan,
        "alive_us": len(alive_us),
        "alive_sg": len(alive_sg),
        "alive_any": len(alive_any),
        "alerted_mode": alerted_mode,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if not args.dry_run:
        save_state(args.state_file, state)
    else:
        log.info("dry-run: state not written")

    print(
        f"RESULT\tmode={mode}\talive_us={len(alive_us)}\talive_sg={len(alive_sg)}\t"
        f"alive_any={len(alive_any)}"
    )
    for g in SWITCH_GROUPS:
        print(f"NOW\t{g}\t{nows.get(g)}")
    log.info("failover done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

