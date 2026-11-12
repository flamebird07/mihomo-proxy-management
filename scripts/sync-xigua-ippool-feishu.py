#!/usr/bin/env python3
"""Sync the Xigua IP-pool snapshot to an independent Feishu Bitable table."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import ipaddress
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SYNC_PATH = SCRIPT_DIR / "sync-feishu-bitable.py"
_spec = importlib.util.spec_from_file_location("xigua_feishu_base", SYNC_PATH)
base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = base
_spec.loader.exec_module(base)

TABLE_NAME = "西瓜IP池"
DEFAULT_IPPOOL_STATE = r"C:\mihomo\state\xigua-ippool\ippool.json"
DEFAULT_FINAL_PROVIDER = r"C:\mihomo\providers\xigua-ippool.yaml"
DEFAULT_CURSOR = r"C:\mihomo\state\xigua-ippool\feishu-sync-state.json"
DEFAULT_LOCK = r"C:\mihomo\state\xigua-ippool\feishu-sync.lock"
DEFAULT_LOG = r"C:\mihomo\logs\xigua-ippool-feishu.log"

# Independent from the main sync writer mutex: the Xigua table is a separate
# table, so it must not serialize behind Local\mihomo-feishu-bitable-sync.
XIGUA_SYNC_MUTEX_NAME = r"Local\mihomo-xigua-ippool-feishu-sync"

FIELDS = [
    ("唯一键", base.FT_TEXT, None),
    ("原入口域名", base.FT_TEXT, None),
    ("IP", base.FT_TEXT, None),
    ("本轮发现", base.FT_CHECKBOX, None),
    ("入口健康", base.FT_CHECKBOX, None),
    ("连续成功", base.FT_NUMBER, base.NUMBER_PROPERTY),
    ("连续失败", base.FT_NUMBER, base.NUMBER_PROPERTY),
    ("成功总数", base.FT_NUMBER, base.NUMBER_PROPERTY),
    ("失败总数", base.FT_NUMBER, base.NUMBER_PROPERTY),
    ("延迟ms", base.FT_NUMBER, base.NUMBER_PROPERTY),
    ("最近结果", base.FT_TEXT, None),
    ("首次发现", base.FT_DATE_TIME, base.DATE_PROPERTY),
    ("最后发现", base.FT_DATE_TIME, base.DATE_PROPERTY),
    ("状态更新时间", base.FT_DATE_TIME, base.DATE_PROPERTY),
    ("最终可用", base.FT_CHECKBOX, None),
    ("最终节点名", base.FT_TEXT, None),
]


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def parse_time(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def time_ms(value) -> int | None:
    parsed = parse_time(value)
    return int(parsed.timestamp() * 1000) if parsed else None


def same_time(left, right) -> bool:
    a, b = parse_time(left), parse_time(right)
    if a and b:
        return a == b
    return isinstance(left, str) and isinstance(right, str) and left.strip() == right.strip()


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise base.SyncError("bad_state", type(exc).__name__) from None
    if not isinstance(data, dict) or data.get("schema") != 2:
        raise base.SyncError("bad_state", "schema_not_v2")
    hosts = data.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        raise base.SyncError("bad_state", "hosts_empty_or_invalid")
    total = 0
    for host, host_state in hosts.items():
        if not isinstance(host, str) or not host or not isinstance(host_state, dict):
            raise base.SyncError("bad_state", "host_invalid")
        ips = host_state.get("ips")
        if not isinstance(ips, dict):
            raise base.SyncError("bad_state", "ips_invalid")
        for ip, record in ips.items():
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise base.SyncError("bad_state", "ip_invalid") from None
            if not isinstance(record, dict):
                raise base.SyncError("bad_state", "ip_record_invalid")
            total += 1
    if total == 0:
        raise base.SyncError("empty_snapshot", "refuse_clear")
    return data


def final_mapping(path: Path, state: dict) -> dict[tuple[str, str], list[str]]:
    if not path.exists():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    proxies = document.get("proxies", []) if isinstance(document, dict) else []
    hosts_by_ip: dict[str, list[str]] = {}
    for host, host_state in state["hosts"].items():
        for ip in host_state["ips"]:
            hosts_by_ip.setdefault(ip, []).append(host)
    mapped: dict[tuple[str, str], list[str]] = {}
    for proxy in proxies if isinstance(proxies, list) else []:
        if not isinstance(proxy, dict):
            continue
        ip, name = str(proxy.get("server", "")), str(proxy.get("name", ""))
        if not ip or not name:
            continue
        host = ""
        parts = name.rsplit("@", 2)
        if len(parts) == 3 and parts[2] == ip and parts[1] in state["hosts"]:
            host = parts[1]
        elif len(hosts_by_ip.get(ip, [])) == 1:
            host = hosts_by_ip[ip][0]
        if host and ip in state["hosts"][host]["ips"]:
            mapped.setdefault((host, ip), []).append(name)
    return {key: sorted(set(names)) for key, names in mapped.items()}


def build_records(state: dict, mapping: dict[tuple[str, str], list[str]]) -> list[dict]:
    updated = state.get("updated_at")
    updated_ms = time_ms(updated)
    records = []
    for host in sorted(state["hosts"]):
        for ip in sorted(state["hosts"][host]["ips"], key=lambda x: ipaddress.ip_address(x)):
            item = state["hosts"][host]["ips"][ip]
            names = mapping.get((host, ip), [])
            fields = {
                "唯一键": f"{host}|{ip}", "原入口域名": host, "IP": ip,
                "本轮发现": same_time(item.get("last_seen"), updated),
                "入口健康": bool(item.get("healthy", False)),
                "连续成功": int(item.get("success_streak") or 0),
                "连续失败": int(item.get("failure_streak") or 0),
                "成功总数": int(item.get("success_total") or 0),
                "失败总数": int(item.get("failure_total") or 0),
                "延迟ms": item.get("latency_ms"), "最近结果": str(item.get("last_result") or ""),
                "首次发现": time_ms(item.get("first_seen")),
                "最后发现": time_ms(item.get("last_seen")),
                "状态更新时间": updated_ms,
                "最终可用": bool(names), "最终节点名": "、".join(names),
            }
            fields = {key: value for key, value in fields.items() if value is not None and value != ""}
            records.append({"fields": fields})
    if not records:
        raise base.SyncError("empty_snapshot", "refuse_clear")
    return records


def read_cursor(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_cursor(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def ensure_table(client, log) -> str:
    remote = {item.get("name"): item.get("table_id") for item in client.list_tables()}
    table_id = remote.get(TABLE_NAME) or client.create_table(TABLE_NAME, FIELDS)
    if not table_id:
        raise base.SyncError("api_error", "table_id_missing")
    base.ensure_fields(client, table_id, FIELDS, log)
    return table_id


def sync_snapshot(client, state_path: Path, provider_path: Path, cursor_path: Path, log) -> str:
    state_sha = sha256_path(state_path)
    provider_sha = sha256_path(provider_path)
    state = load_state(state_path)
    records = build_records(state, final_mapping(provider_path, state))
    table_id = ensure_table(client, log)
    cursor = read_cursor(cursor_path)
    signature = {"state_sha256": state_sha, "provider_sha256": provider_sha, "table_id": table_id}
    if all(cursor.get(key) == value for key, value in signature.items()):
        log.info(f"unchanged rows={len(records)} table_id={table_id}")
        cursor.update({"last_result": "unchanged"})
        write_cursor(cursor_path, cursor)
        return "unchanged"
    created = base.replace_records(client, table_id, records, log)
    if created != len(records):
        raise base.SyncError("write_verify_failed", "created_count_mismatch")
    remote = client.list_records(table_id)
    remote_keys = {
        (item.get("fields") or {}).get("唯一键") for item in remote if isinstance(item, dict)
    }
    stable_key = records[0]["fields"]["唯一键"]
    if len(remote) != len(records) or stable_key not in remote_keys:
        raise base.SyncError("write_verify_failed", "readback_mismatch")
    signature.update({"last_result": "written", "row_count": len(records), "stable_key": stable_key})
    write_cursor(cursor_path, signature)
    log.info(f"written rows={len(records)} readback={len(remote)} table_id={table_id}")
    return "written"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sync Xigua IP pool to a dedicated Feishu table")
    parser.add_argument("--ippool-state", default=DEFAULT_IPPOOL_STATE)
    parser.add_argument("--final-provider", default=DEFAULT_FINAL_PROVIDER)
    parser.add_argument("--cursor", default=DEFAULT_CURSOR)
    parser.add_argument("--lock", default=DEFAULT_LOCK)
    parser.add_argument("--log-file", default=DEFAULT_LOG)
    parser.add_argument("--env-file", default=base.DEFAULT_ENV_FILE)
    parser.add_argument("--app-token", default=base.DEFAULT_APP_TOKEN)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log = base.Logger(args.log_file)
    lock = None
    try:
        lock = base.SyncLock(args.lock, log, mutex_name=XIGUA_SYNC_MUTEX_NAME).acquire()
        env = base.read_env_file(args.env_file)
        env.update({key: value for key, value in os.environ.items() if key.startswith("FEISHU_")})
        app_id = (env.get("FEISHU_APP_ID") or "").strip()
        app_secret = (env.get("FEISHU_APP_SECRET") or "").strip()
        if not app_id or not app_secret:
            raise base.SyncError("config_error", "feishu_credentials_missing")
        client = base.FeishuClient(app_id, app_secret, args.app_token, log)
        result = sync_snapshot(client, Path(args.ippool_state), Path(args.final_provider),
                               Path(args.cursor), log)
        print(json.dumps({"status": result, "table": TABLE_NAME}, ensure_ascii=False))
        return 0
    except base.SyncError as exc:
        log.error(f"sync failed class={exc.kind}")
        print(json.dumps({"status": "error", "class": exc.kind}), file=sys.stderr)
        return 3 if exc.kind == "lock_busy" else 1
    except Exception as exc:
        log.error(f"sync failed class={type(exc).__name__}")
        print(json.dumps({"status": "error", "class": type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        base.release_sync_lock(lock, log)


if __name__ == "__main__":
    raise SystemExit(main())
