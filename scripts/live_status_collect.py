#!/usr/bin/env python3
"""Read-only live status collector for the local mihomo instance (DIAN-14).

Emits a single stable JSON document; never writes to Feishu, never triggers
delay probes or group re-selection, never modifies anything. The mihomo API
secret (read from config.yaml when present) lives in memory only and must
never appear in output, logs, or exception messages.

Exit code is always 0. Per-item failures are recorded in ``errors``
(secret-free, single line each) with the affected field set to null. When
the API is unreachable, ``ok`` is false but local items are still collected.

Output JSON schema (v1, stable key set)::

    {
      "schema_version": 1,
      "collected_at": {"utc": str, "beijing": str},
      "ok": bool,                      # false when the mihomo API read failed
      "api": {
        "base": str,                   # controller base URL actually used
        "reachable": bool,
        "errors": [str]                # API-read specific errors (sanitized)
      },
      "groups": {                      # one entry per GROUPS; null when unreadable
        "<GPT|US-FAST|GROKBOT|GROKAPI>": {
          "type": str,                 # mihomo group type, e.g. "Fallback"/"Selector"
          "now": str|null,             # currently selected member name
          "member_count": int,
          "members": [str],            # member order as reported by "all"
          "member_status": [
            {
              "name": str,
              "last_delay_ms": int|null,   # delay of the most recent history entry
              "last_time": str|null,       # timestamp of that entry (as reported)
              "usable_by_history": bool,   # last delay > 0 and record fresh
              "stale": bool                # no record, or older than the freshness window
            }
          ],
          "usable_count_by_history": int   # count of usable_by_history == true
        }
      },
      "usable_count_note": str,        # fixed caveat: the count is history-based only
      "process": {
        "mihomo_pid": int|null,
        "mihomo_start_time": str|null, # ISO local time
        "boot_time": str|null          # ISO local time
      },
      "state_files": {                 # null entries when the file is missing/unreadable
        "ippool": {"path": str, "host_count": int, "healthy_ip_count": int,
                    "ip_total": int, "updated_at": str|null},
        "feishu_gpt_history": {"path": str, "version": int|null, "node_count": int|null},
        "feishu_bitable_sync_state": {"path": str, "summary": object}
      },
      "scheduled_tasks": {             # null entries when the task is missing/unreadable
        "<task name>": {"exists": bool, "state": str|null, "last_run_time": str|null,
                         "last_task_result": int|null, "next_run_time": str|null}
      },
      "freshness_window_minutes": float,
      "errors": [str]                  # all collection errors, sanitized
    }

Usability caveat (口径): ``usable_count_by_history`` is judged purely from each
member's most recent ``history`` entry (delay > 0 and recorded within the
freshness window, default 15 minutes). It only reflects the last probe mihomo
happens to have recorded — it is NOT a realtime measurement, and this script
deliberately performs no probing of its own.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
GROUPS = ("GPT", "US-FAST", "GROKBOT", "GROKAPI")
TASK_NAMES = ("MihomoFeishuBitableSync", "MihomoXiguaIpPool", "MihomoNinjaGrokFeishuSync")
DEFAULT_API = "http://127.0.0.1:9090"
DEFAULT_CONFIG = r"C:\mihomo\config.yaml"
DEFAULT_STATE_DIR = r"C:\mihomo\state"
DEFAULT_FRESHNESS_MINUTES = 15.0
BEIJING_TZ = timezone(timedelta(hours=8))
_USABLE_NOTE = (
    "口径=history：usable_count_by_history 仅按各成员最近一次 history 探测记录判断"
    "（delay>0 且记录在新鲜窗口内），不代表实时可用性；本脚本不触发任何探测。"
)


class _PowerShellError(RuntimeError):
    """Read-only PowerShell invocation failed; message holds sanitized stderr."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sanitize_error(exc: Exception) -> str:
    """One-line, secret-free description of an exception.

    Only _PowerShellError carries a message we authored ourselves (PowerShell
    exit detail); every other type is reduced to its class name so nothing from
    a URL, header, or config body can leak into the errors array.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError {exc.code}"
    if isinstance(exc, _PowerShellError):
        return str(exc.args[0]).replace("\n", " ")[:200]
    return type(exc).__name__


def _parse_iso(text):
    """Parse an ISO-8601 timestamp with >6 fractional digits (mihomo emits 7)."""
    if not isinstance(text, str) or not text:
        return None
    t = re.sub(r"(\.\d{6})\d+", r"\1", text.strip())
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def _read_secret_from_config(config_path):
    """Return the API secret from config.yaml, or None. Never raises with the value."""
    try:
        import yaml  # optional dependency; regex fallback below
    except ImportError:
        yaml = None
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError:
        return None
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                secret = data.get("secret")
                return str(secret).strip() if secret else None
        except Exception:
            pass
    m = re.search(r"^secret:\s*[\"']?([^\"'\r\n]+)", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _is_allowed_path(path):
    """Only GET /proxies and /proxies/<name> are read-only; /delay and writes are not."""
    parts = [p for p in path.split("/") if p]
    return parts[:1] == ["proxies"] and len(parts) <= 2


def http_get_json(url, secret=None, timeout=5):
    """GET a URL and parse JSON. Guarded so only read-only /proxies is ever fetched."""
    parsed = urllib.parse.urlsplit(url)
    if not _is_allowed_path(parsed.path):
        raise ValueError(f"path not allowed for read-only collector: {parsed.path}")
    req = urllib.request.Request(url, method="GET")
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_powershell(command, timeout=20):
    """Run a read-only PowerShell command; return stdout decoded best-effort."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        # PowerShell stderr never carries the API secret (it is only ever used in
        # HTTP headers), so its first line is safe and useful in the errors array.
        first = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = f": {first[0][:160]}" if first else ""
        raise _PowerShellError(f"powershell exited {proc.returncode}{detail}")
    raw = proc.stdout
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# API / groups
# ---------------------------------------------------------------------------

def collect_groups(api_base, proxies_payload, freshness_minutes, http_get=http_get_json,
                   secret=None, timeout=5):
    """Build the per-group summary from a /proxies payload.

    ``proxies_payload`` is the parsed body of GET /proxies. When a member's
    data is missing from the payload it is refetched with GET
    /proxies/<quoted name> — still read-only, never a delay endpoint.
    """
    proxies = proxies_payload.get("proxies") or {}
    window = timedelta(minutes=freshness_minutes)
    now = datetime.now(timezone.utc)
    errors = []
    groups = {}
    for name in GROUPS:
        info = proxies.get(name)
        if not isinstance(info, dict):
            groups[name] = None
            errors.append(f"groups.{name}: not found in /proxies payload")
            continue
        members = list(info.get("all") or [])
        member_status = []
        for member in members:
            mdata = proxies.get(member)
            if not isinstance(mdata, dict):
                # Only ever a read-only GET /proxies/<name>; name is URL-encoded
                # because node names are typically Chinese.
                url = f"{api_base}/proxies/{urllib.parse.quote(member, safe='')}"
                try:
                    fetched = http_get(url, secret=secret, timeout=timeout)
                    mdata = fetched if isinstance(fetched, dict) else {}
                except Exception as exc:
                    mdata = {}
                    errors.append(f"groups.{name}.member[{member}]: {_sanitize_error(exc)}")
            history = mdata.get("history") or []
            last = history[-1] if isinstance(history, list) and history else None
            last_delay = None
            last_time_raw = None
            if isinstance(last, dict):
                delay = last.get("delay")
                if isinstance(delay, int) and delay >= 0:
                    last_delay = delay
                last_time_raw = last.get("time")
            parsed_time = _parse_iso(last_time_raw)
            stale = True
            if parsed_time is not None:
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=timezone.utc)
                stale = (now - parsed_time) > window
            usable = bool(isinstance(last_delay, int) and last_delay > 0 and not stale)
            member_status.append({
                "name": member,
                "last_delay_ms": last_delay,
                "last_time": last_time_raw if isinstance(last_time_raw, str) else None,
                "usable_by_history": usable,
                "stale": stale,
            })
        groups[name] = {
            "type": info.get("type"),
            "now": info.get("now"),
            "member_count": len(members),
            "members": members,
            "member_status": member_status,
            "usable_count_by_history": sum(1 for m in member_status if m["usable_by_history"]),
        }
    return groups, errors


# ---------------------------------------------------------------------------
# process / boot
# ---------------------------------------------------------------------------

def collect_process_info():
    """mihomo.exe PID / start time and boot time. psutil first, PowerShell fallback."""
    pid = None
    start_time = None
    boot_time = None
    errors = []
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["name", "pid", "create_time"]):
                if (proc.info.get("name") or "").lower() == "mihomo.exe":
                    pid = proc.info["pid"]
                    ct = proc.info.get("create_time")
                    if ct:
                        start_time = datetime.fromtimestamp(ct).astimezone().isoformat()
                    break
        except Exception as exc:
            errors.append(f"process.psutil: {_sanitize_error(exc)}")
        try:
            boot_time = datetime.fromtimestamp(psutil.boot_time()).astimezone().isoformat()
        except Exception as exc:
            errors.append(f"process.boot_time: {_sanitize_error(exc)}")
    if pid is None:
        try:
            out = _run_powershell(
                "Get-Process mihomo -ErrorAction SilentlyContinue | "
                "Select-Object -First 1 Id, StartTime | ConvertTo-Json -Compress")
            data = json.loads(out) if out.strip() else None
            if isinstance(data, dict) and data.get("Id"):
                pid = int(data["Id"])
                st = data.get("StartTime")
                if isinstance(st, str):
                    start_time = _parse_iso(st.replace("/Date(", "").rstrip(")/")) or st
                elif isinstance(st, dict) and "DateTime" in st:
                    start_time = st["DateTime"]
        except Exception as exc:
            errors.append(f"process.powershell_pid: {_sanitize_error(exc)}")
    if boot_time is None:
        try:
            out = _run_powershell(
                "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')")
            boot_time = out.strip() or None
        except Exception as exc:
            errors.append(f"process.powershell_boot: {_sanitize_error(exc)}")
    return {"mihomo_pid": pid, "mihomo_start_time": start_time, "boot_time": boot_time}, errors


# ---------------------------------------------------------------------------
# local state files (read-only)
# ---------------------------------------------------------------------------

def _resolve_state_file(state_dir, filename):
    """Look for filename under state_dir, its immediate subdirs, and its parent."""
    base = Path(state_dir)
    candidates = [base / filename]
    try:
        candidates += sorted(p / filename for p in base.iterdir() if p.is_dir())
    except OSError:
        pass
    candidates.append(base.parent / filename)
    for path in candidates:
        if path.is_file():
            return path
    return None


def collect_state_files(state_dir):
    errors = []
    result = {"ippool": None, "feishu_gpt_history": None, "feishu_bitable_sync_state": None}

    path = _resolve_state_file(state_dir, "ippool.json")
    if path is None:
        errors.append("state_files.ippool: ippool.json not found (null)")
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            hosts = data.get("hosts") or {}
            ip_total = 0
            healthy = 0
            for host in hosts.values():
                ips = (host or {}).get("ips") or {}
                ip_total += len(ips)
                healthy += sum(1 for ip in ips.values() if (ip or {}).get("healthy") is True)
            result["ippool"] = {
                "path": str(path),
                "host_count": len(hosts),
                "healthy_ip_count": healthy,
                "ip_total": ip_total,
                "updated_at": data.get("updated_at"),
            }
        except Exception as exc:
            errors.append(f"state_files.ippool: {_sanitize_error(exc)}")

    path = _resolve_state_file(state_dir, "feishu-gpt-history.json")
    if path is None:
        errors.append("state_files.feishu_gpt_history: feishu-gpt-history.json not found (null)")
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            nodes = data.get("nodes")
            result["feishu_gpt_history"] = {
                "path": str(path),
                "version": data.get("version"),
                "node_count": len(nodes) if isinstance(nodes, (list, dict)) else None,
            }
        except Exception as exc:
            errors.append(f"state_files.feishu_gpt_history: {_sanitize_error(exc)}")

    path = _resolve_state_file(state_dir, "feishu-bitable-sync-state.json")
    if path is None:
        errors.append(
            "state_files.feishu_bitable_sync_state: feishu-bitable-sync-state.json not found (null)")
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            summary = {k: data.get(k) for k in (
                "last_result", "last_success_time", "last_start", "last_end",
                "last_error", "consecutive_failures", "duration_sec")
                if k in data} if isinstance(data, dict) else None
            result["feishu_bitable_sync_state"] = {"path": str(path), "summary": summary}
        except Exception as exc:
            errors.append(f"state_files.feishu_bitable_sync_state: {_sanitize_error(exc)}")

    return result, errors


# ---------------------------------------------------------------------------
# scheduled tasks (read-only)
# ---------------------------------------------------------------------------

def collect_scheduled_tasks(task_names=TASK_NAMES):
    errors = []
    tasks = {}
    try:
        names_literal = ", ".join("'{}'".format(n.replace("'", "''")) for n in task_names)
        script = (
            f"$names = @({names_literal});"
            "$out = foreach ($n in $names) {"
            "  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue;"
            "  if (-not $t) { [pscustomobject]@{name=$n; exists=$false; state=$null;"
            "    last_run_time=$null; last_task_result=$null; next_run_time=$null} }"
            "  else {"
            "    $i = $t | Get-ScheduledTaskInfo;"
            "    [pscustomobject]@{name=$n; exists=$true; state=[string]$t.State;"
            "      last_run_time=$(if ($i.LastRunTime) { $i.LastRunTime.ToString('o') } else { $null });"
            "      last_task_result=$i.LastTaskResult;"
            "      next_run_time=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('o') } else { $null }) }"
            "  }"
            "};"
            "ConvertTo-Json -Compress -InputObject @($out)")
        out = _run_powershell(script, timeout=30)
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        by_name = {item.get("name"): item for item in data if isinstance(item, dict)}
        for name in task_names:
            item = by_name.get(name)
            if item is None or not item.get("exists"):
                tasks[name] = None
                errors.append(f"scheduled_tasks.{name}: task not found (null)")
            else:
                tasks[name] = {
                    "exists": True,
                    "state": item.get("state"),
                    "last_run_time": item.get("last_run_time"),
                    "last_task_result": item.get("last_task_result"),
                    "next_run_time": item.get("next_run_time"),
                }
    except Exception as exc:
        for name in task_names:
            tasks.setdefault(name, None)
        errors.append(f"scheduled_tasks: {_sanitize_error(exc)}")
    return tasks, errors


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def collect(api_base=DEFAULT_API, config_path=DEFAULT_CONFIG, state_dir=DEFAULT_STATE_DIR,
            freshness_minutes=DEFAULT_FRESHNESS_MINUTES, http_get=None,
            process_collector=None, task_collector=None,
            timeout=5):
    """Run the full read-only collection and return the schema-v1 dict."""
    if http_get is None:
        http_get = http_get_json
    if process_collector is None:
        process_collector = collect_process_info
    if task_collector is None:
        task_collector = collect_scheduled_tasks
    errors = []
    now_utc = datetime.now(timezone.utc)
    secret = _read_secret_from_config(config_path)

    api_errors = []
    proxies_payload = None
    try:
        url = f"{api_base.rstrip('/')}/proxies"
        proxies_payload = http_get(url, secret=secret, timeout=timeout)
    except Exception as exc:
        api_errors.append(f"api./proxies: {_sanitize_error(exc)}")
    errors.extend(api_errors)

    groups = {name: None for name in GROUPS}
    if isinstance(proxies_payload, dict):
        groups, group_errors = collect_groups(
            api_base.rstrip("/"), proxies_payload, freshness_minutes,
            http_get=http_get, secret=secret, timeout=timeout)
        errors.extend(group_errors)

    process_info, process_errors = process_collector()
    errors.extend(process_errors)
    state_files, state_errors = collect_state_files(state_dir)
    errors.extend(state_errors)
    tasks, task_errors = task_collector()
    errors.extend(task_errors)

    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": {
            "utc": now_utc.isoformat(timespec="seconds"),
            "beijing": now_utc.astimezone(BEIJING_TZ).isoformat(timespec="seconds"),
        },
        "ok": proxies_payload is not None,
        "api": {
            "base": api_base.rstrip("/"),
            "reachable": proxies_payload is not None,
            "errors": api_errors,
        },
        "groups": groups,
        "usable_count_note": _USABLE_NOTE,
        "process": process_info,
        "state_files": state_files,
        "scheduled_tasks": tasks,
        "freshness_window_minutes": freshness_minutes,
        "errors": errors,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only mihomo live status collector (JSON only, no writes, no probes).")
    parser.add_argument("--json", action="store_true",
                        help="print the JSON document to stdout (default when --out is absent)")
    parser.add_argument("--out", default=None, help="optional output file path (UTF-8)")
    parser.add_argument("--api", default=DEFAULT_API, help=f"controller base URL (default {DEFAULT_API})")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="mihomo config.yaml path (secret read in-memory only)")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="local state directory")
    parser.add_argument("--freshness-minutes", type=float, default=DEFAULT_FRESHNESS_MINUTES,
                        help=f"history freshness window in minutes (default {DEFAULT_FRESHNESS_MINUTES})")
    parser.add_argument("--timeout", type=float, default=5, help="per-request timeout seconds")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    result = collect(api_base=args.api, config_path=args.config, state_dir=args.state_dir,
                     freshness_minutes=args.freshness_minutes, timeout=args.timeout)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
