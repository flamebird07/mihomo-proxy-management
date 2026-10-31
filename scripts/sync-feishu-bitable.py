#!/usr/bin/env python3
"""Sync mihomo three-source status + ChatGPT speed test into a Feishu Bitable.

Three tables are maintained in one Bitable app (one sync batch per run):

  1. 订阅源状态   - one fixed row per subscription source (飞兔/星辰/CoffeeCloud)
  2. 可测速节点   - every parsed node with its ChatGPT-trace delay
  3. GPT可用地区组 - Singapore/US nodes whose ChatGPT trace test succeeded

Security contract
-----------------
* Subscription URLs, FEISHU_APP_SECRET and tenant_access_token are never
  printed, logged or persisted by this script.
* Log lines carry only HTTP status codes, row counts and error
  classifications (http_403 / token_error / timeout / network_error /
  parse_error / api_error).
* The state file keeps table_id values only (no credentials).
* stdout carries the operator summary only; progress goes to the log file.

Usage
-----
  python sync-feishu-bitable.py                     # full sync
  python sync-feishu-bitable.py --dry-run           # collect + test, no write
  python sync-feishu-bitable.py --ensure-tables     # only create tables
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import convert_sub  # noqa: E402  (same directory, project-local converter)

try:
    import yaml  # noqa: F401  (convert_sub already requires it)
except ImportError:  # pragma: no cover - convert_sub fails closed on its own
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_APP_TOKEN = "URo9bAsd5azYe5swAmtclIbEnNh"
DEFAULT_ENV_FILE = r"C:\Users\Administrator\.hermes\.env"
DEFAULT_STATE_FILE = r"C:\mihomo\feishu-bitable-tables.json"
DEFAULT_LOG_FILE = r"C:\mihomo\logs\feishu-bitable-sync.log"
DEFAULT_SOURCES_ENV = str(SCRIPT_DIR.parent / "config" / "subscription.env")
DEFAULT_CORE = r"C:\mihomo\mihomo.exe"
DEFAULT_TARGET = "https://chatgpt.com/cdn-cgi/trace"
DEFAULT_LIVE_CONTROLLER = "http://127.0.0.1:9090"
DEFAULT_LIVE_ENV = r"C:\mihomo\subscription.env"
DEFAULT_FETCH_PROXY = "http://127.0.0.1:7890"

PLACEHOLDER_TABLE_ID = "tbldzw2ismrKCEAC"
PLACEHOLDER_TABLE_NAME = "数据表"

SOURCE_NAMES = {1: "飞兔", 2: "星辰", 3: "CoffeeCloud"}

# Feishu Bitable field types
FT_TEXT = 1
FT_NUMBER = 2
FT_SINGLE_SELECT = 3
FT_DATE_TIME = 5
FT_CHECKBOX = 7

DATE_PROPERTY = {"date_formatter": "yyyy/MM/dd HH:mm", "auto_fill": False}
NUMBER_PROPERTY = {"formatter": "0"}

TABLE_SPECS: list[dict[str, Any]] = [
    {
        "key": "sources",
        "name": "订阅源状态",
        "fields": [
            ("源名称", FT_TEXT, None),
            ("源编号", FT_NUMBER, NUMBER_PROPERTY),
            ("HTTP状态", FT_NUMBER, NUMBER_PROPERTY),
            ("拉取结果", FT_SINGLE_SELECT, {"options": [{"name": "成功"}, {"name": "失败"}, {"name": "缓存"}]}),
            ("节点数", FT_NUMBER, NUMBER_PROPERTY),
            ("存活数", FT_NUMBER, NUMBER_PROPERTY),
            ("流量剩余", FT_TEXT, None),
            ("到期时间", FT_TEXT, None),
            ("失败原因", FT_TEXT, None),
            ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
        ],
    },
    {
        "key": "nodes",
        "name": "可测速节点",
        "fields": [
            ("节点名", FT_TEXT, None),
            ("来源", FT_SINGLE_SELECT, {"options": [{"name": n} for n in ("飞兔", "星辰", "CoffeeCloud", "未知")]}),
            ("协议", FT_TEXT, None),
            ("延迟ms", FT_NUMBER, NUMBER_PROPERTY),
            ("存活", FT_CHECKBOX, None),
            ("测速目标", FT_TEXT, None),
            ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
        ],
    },
    {
        "key": "gpt_regions",
        "name": "GPT可用地区组",
        "fields": [
            ("节点名", FT_TEXT, None),
            ("地区组", FT_SINGLE_SELECT, {"options": [{"name": "新加坡"}, {"name": "美国"}]}),
            ("来源", FT_SINGLE_SELECT, {"options": [{"name": n} for n in ("飞兔", "星辰", "CoffeeCloud", "未知")]}),
            ("延迟ms", FT_NUMBER, NUMBER_PROPERTY),
            ("GPT可用", FT_CHECKBOX, None),
            ("代理组", FT_TEXT, None),
            ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
        ],
    },
]

# Rate-limit / transient API codes returned by the Bitable endpoints.
RETRY_CODES = {1254290, 1254291, 1254607, 99991400, 1310213}
TOKEN_CODES = {99991661, 99991663, 99991664, 99991668}

SG_PATTERN = re.compile(r"新加坡|狮城|獅城|singapore|🇸🇬|\bSGP?\b", re.IGNORECASE)
US_PATTERN = re.compile(
    r"美国|美國|united states|\bUSA?\b|🇺🇸|los angeles|san jose|seattle|dallas|chicago|"
    r"new york|ashburn|silicon valley|phoenix|miami|atlanta|las vegas|portland|denver|"
    r"virginia|ohio|california|圣何塞|洛杉矶|西雅图|达拉斯|芝加哥|纽约|硅谷|凤凰城",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Logging (never writes secrets or URLs)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Env / secret loading
# ---------------------------------------------------------------------------

def read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except (OSError, UnicodeError):
        pass
    return values


class SyncError(Exception):
    """Failure carrying a classification string that is safe to log."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# Feishu client
# ---------------------------------------------------------------------------

class FeishuClient:
    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, app_token: str, log: Logger) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.log = log
        self._token: str | None = None
        self._expires_at = 0.0

    # -- transport ---------------------------------------------------------

    def _raw(self, method: str, url: str, body: dict | None, headers: dict[str, str],
             timeout: int = 30) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # The HTTP body may echo request details; only the status is kept.
            raise SyncError(f"http_{exc.code}") from None
        except (socket.timeout, TimeoutError):
            raise SyncError("timeout") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise SyncError("timeout") from None
            raise SyncError("network_error") from None
        except Exception as exc:  # pragma: no cover - defensive
            raise SyncError(type(exc).__name__.lower()) from None
        try:
            return json.loads(raw or "{}")
        except ValueError:
            raise SyncError("bad_response") from None

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 120:
            return self._token
        payload = self._raw(
            "POST",
            f"{self.BASE}/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
            {"Content-Type": "application/json; charset=utf-8"},
            timeout=20,
        )
        code = payload.get("code")
        if code != 0 or not payload.get("tenant_access_token"):
            raise SyncError("token_error", f"code={code}")
        self._token = payload["tenant_access_token"]
        self._expires_at = time.time() + int(payload.get("expire") or 7200)
        self.log.info("token acquired")
        return self._token

    def call(self, method: str, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        url = self.BASE + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        delay = 1.0
        for attempt in range(4):
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.token()}",
            }
            payload = self._raw(method, url, body, headers)
            code = payload.get("code", 0)
            if code == 0:
                return payload.get("data") or {}
            if code in TOKEN_CODES:
                self._token = None
                if attempt < 3:
                    time.sleep(0.5)
                    continue
                raise SyncError("token_error", f"code={code}")
            if code in RETRY_CODES:
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise SyncError("rate_limited", f"code={code}")
            raise SyncError("api_error", f"code={code}")
        raise SyncError("api_error", "retry_exhausted")

    # -- tables ------------------------------------------------------------

    def list_tables(self) -> list[dict]:
        tables: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.call("GET", f"/bitable/v1/apps/{self.app_token}/tables", params=params)
            tables.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return tables

    def create_table(self, name: str, fields: list[tuple]) -> str:
        body = {
            "table": {
                "name": name,
                "default_view_name": "表格",
                "fields": [
                    {"field_name": f[0], "type": f[1], **({"property": f[2]} if f[2] else {})}
                    for f in fields
                ],
            }
        }
        data = self.call("POST", f"/bitable/v1/apps/{self.app_token}/tables", body=body)
        table_id = data.get("table_id") or ""
        if not table_id:
            raise SyncError("api_error", "create_table_no_id")
        return table_id

    def delete_table(self, table_id: str) -> None:
        self.call("DELETE", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}")

    def list_fields(self, table_id: str) -> list[dict]:
        fields: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.call(
                "GET", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields", params=params
            )
            fields.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return fields

    def create_field(self, table_id: str, name: str, ftype: int, prop: dict | None) -> None:
        body: dict[str, Any] = {"field_name": name, "type": ftype}
        if prop:
            body["property"] = prop
        self.call("POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields", body=body)

    def update_field(self, table_id: str, field_id: str, name: str, ftype: int, prop: dict) -> None:
        body = {"field_name": name, "type": ftype, "property": prop}
        self.call(
            "PUT", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields/{field_id}", body=body
        )

    # -- records -----------------------------------------------------------

    def list_records(self, table_id: str) -> list[dict]:
        records: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.call(
                "GET", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records", params=params
            )
            records.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return records

    def batch_delete(self, table_id: str, record_ids: list[str]) -> int:
        deleted = 0
        for start in range(0, len(record_ids), 200):
            chunk = record_ids[start:start + 200]
            self.call(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_delete",
                body={"records": chunk},
            )
            deleted += len(chunk)
            time.sleep(0.3)
        return deleted

    def batch_create(self, table_id: str, records: list[dict]) -> int:
        created = 0
        for start in range(0, len(records), 100):
            chunk = records[start:start + 100]
            self.call(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_create",
                body={"records": chunk},
            )
            created += len(chunk)
            time.sleep(0.3)
        return created


# ---------------------------------------------------------------------------
# Table provisioning
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, tables: dict[str, dict[str, str]]) -> None:
    payload = {"tables": tables, "updated": dt.datetime.now().isoformat(timespec="seconds")}
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        pass


def ensure_fields(client: FeishuClient, table_id: str, fields: list[tuple], log: Logger) -> None:
    existing = {f.get("field_name"): f for f in client.list_fields(table_id)}
    for name, ftype, prop in fields:
        current = existing.get(name)
        if current is None:
            try:
                client.create_field(table_id, name, ftype, prop)
                log.info(f"field created name={name} type={ftype}")
            except SyncError as exc:
                log.warn(f"field create failed name={name} class={exc.kind}")
            continue
        if ftype != FT_SINGLE_SELECT or not prop:
            continue
        wanted = [o["name"] for o in prop.get("options", [])]
        current_prop = current.get("property") or {}
        have = [o.get("name") for o in (current_prop.get("options") or [])]
        missing = [name_ for name_ in wanted if name_ not in have]
        if not missing:
            continue
        merged = {"options": [{"name": n} for n in have + missing]}
        try:
            client.update_field(table_id, current.get("field_id", ""), name, ftype, merged)
            log.info(f"field options extended name={name} added={len(missing)}")
        except SyncError as exc:
            log.warn(f"field update failed name={name} class={exc.kind}")


def ensure_tables(client: FeishuClient, state_file: str, keep_placeholder: bool,
                  log: Logger) -> dict[str, dict[str, str]]:
    remote = {t.get("name"): t.get("table_id") for t in client.list_tables()}
    log.info(f"remote tables={len(remote)}")
    tables: dict[str, dict[str, str]] = {}
    for spec in TABLE_SPECS:
        table_id = remote.get(spec["name"])
        if table_id:
            log.info(f"table exists name={spec['name']}")
        else:
            table_id = client.create_table(spec["name"], spec["fields"])
            log.info(f"table created name={spec['name']}")
            time.sleep(0.4)
        ensure_fields(client, table_id, spec["fields"], log)
        tables[spec["key"]] = {"table_id": table_id, "name": spec["name"]}

    real_ids = {entry["table_id"] for entry in tables.values()}
    placeholder = remote.get(PLACEHOLDER_TABLE_NAME) or PLACEHOLDER_TABLE_ID
    if not keep_placeholder and placeholder and placeholder not in real_ids and len(tables) == 3:
        try:
            client.delete_table(placeholder)
            log.info("placeholder table removed")
        except SyncError as exc:
            log.warn(f"placeholder delete failed class={exc.kind}")

    save_state(state_file, tables)
    return tables


# ---------------------------------------------------------------------------
# Subscription collection (per source, then tagged)
# ---------------------------------------------------------------------------

class SourceResult:
    def __init__(self, index: int, name: str) -> None:
        self.index = index
        self.name = name
        self.http_status = 0
        self.result = "失败"
        self.nodes: list[dict] = []
        self.error = ""
        self.traffic = ""
        self.expire = ""

    @property
    def alive(self) -> int:
        return sum(1 for node in self.nodes if node.get("_delay"))


def _classify_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    text = type(exc).__name__.lower()
    return "timeout" if "timeout" in text else "network_error"


def http_fetch(url: str, ua: str, timeout: int,
               proxy: str | None) -> tuple[int, str, dict[str, str], str]:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            headers = {k.lower(): v for k, v in response.headers.items()}
            raw = response.read()
        return status, raw.decode("utf-8", "replace"), headers, ""
    except urllib.error.HTTPError as exc:
        return int(exc.code), "", {}, f"http_{int(exc.code)}"
    except urllib.error.URLError as exc:
        return 0, "", {}, _classify_reason(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return 0, "", {}, _classify_reason(exc)


def _format_bytes(value: int) -> str:
    if value <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f}{unit}" if unit != "B" else f"{value}B"
        value /= 1024.0
    return ""


def parse_userinfo(headers: dict[str, str]) -> tuple[str, str]:
    raw = headers.get("subscription-userinfo") or headers.get("subscription-user-info") or ""
    if not raw:
        return "", ""
    values: dict[str, str] = {}
    for part in raw.split(";"):
        key, _, value = part.partition("=")
        values[key.strip().lower()] = value.strip()
    traffic = ""
    try:
        total = int(float(values.get("total", "0") or 0))
        upload = int(float(values.get("upload", "0") or 0))
        download = int(float(values.get("download", "0") or 0))
        if total > 0:
            used = min(upload + download, total)
            traffic = f"剩余 {_format_bytes(total - used)} / 共 {_format_bytes(total)}"
    except (TypeError, ValueError):
        traffic = ""
    expire = ""
    try:
        stamp = int(float(values.get("expire", "0") or 0))
        if stamp > 0:
            expire = dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        expire = ""
    return traffic, expire


def collect_source(index: int, url: str, ua: str, timeout: int,
                   fallback_proxy: str | None, log: Logger) -> SourceResult:
    result = SourceResult(index, SOURCE_NAMES.get(index, f"源{index}"))
    if not url:
        result.error = "url_missing"
        log.warn(f"source#{index} url_missing")
        return result

    status, text, headers, err = http_fetch(url, ua, timeout, None)
    if err and fallback_proxy:
        log.info(f"source#{index} direct failed class={err}; retry via local proxy")
        status2, text2, headers2, err2 = http_fetch(url, ua, timeout, fallback_proxy)
        if not err2:
            status, text, headers, err = status2, text2, headers2, ""
        else:
            err = err2 if not status else err
    result.http_status = status
    result.traffic, result.expire = parse_userinfo(headers)

    if err:
        result.error = err
        log.warn(f"source#{index} fetch failed status={status} class={err}")
        return result
    if not text.strip():
        result.error = "empty_body"
        log.warn(f"source#{index} empty body status={status}")
        return result

    try:
        nodes, stats = convert_sub.convert_content(
            text, warn=lambda message: log.info(f"source#{index} convert: {message}")
        )
    except convert_sub.ConvertError as exc:
        result.error = f"parse_error(code={exc.exit_code})"
        log.warn(f"source#{index} parse failed class=parse_error")
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"parse_error({type(exc).__name__.lower()})"
        log.warn(f"source#{index} parse failed class=parse_error")
        return result

    result.nodes = nodes
    if nodes:
        result.result = "成功"
        log.info(
            f"source#{index} parsed status={status} nodes={len(nodes)} "
            f"format={stats.input_format} skipped={stats.input_nodes - stats.output_nodes}"
        )
    else:
        result.error = "no_nodes"
        log.warn(f"source#{index} produced no nodes status={status}")
    return result


def load_cache_nodes(paths: list[str], log: Logger) -> list[dict]:
    """Load proxy nodes from yaml/txt cache files via convert_sub when possible."""
    nodes: list[dict] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        valid: list[dict] = []
        try:
            converted, stats = convert_sub.convert_content(content, warn=lambda _m: None)
            proxies = (converted or {}).get("proxies") if isinstance(converted, dict) else None
            if isinstance(proxies, list):
                valid = [p for p in proxies if isinstance(p, dict) and p.get("name") and p.get("type")]
                if valid:
                    log.info(
                        f"cache used path_suffix={path.suffix} nodes={len(valid)} "
                        f"format={getattr(stats, 'input_format', 'unknown')}"
                    )
        except Exception:
            valid = []
        if not valid:
            try:
                data = yaml.safe_load(content) if yaml is not None else None
            except Exception:
                log.warn("cache parse failed class=parse_error")
                continue
            proxies = (data or {}).get("proxies") if isinstance(data, dict) else None
            if isinstance(proxies, list):
                valid = [p for p in proxies if isinstance(p, dict) and p.get("name") and p.get("type")]
                if valid:
                    log.info(f"cache used path_suffix={path.suffix} nodes={len(valid)} format=yaml")
        if valid:
            nodes = valid
            break
    return nodes


def build_pool(results: list[SourceResult]) -> list[tuple[dict, str]]:
    pool: list[tuple[dict, str]] = []
    seen_keys: set[tuple] = set()
    used_names: set[str] = set()
    for result in results:
        for node in result.nodes:
            key = (node.get("type"), node.get("server"), node.get("port"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            name = str(node.get("name") or f"{result.name}-{len(pool) + 1}")
            candidate, suffix = name, 2
            while candidate in used_names:
                candidate = f"{name} [{suffix}]"
                suffix += 1
            used_names.add(candidate)
            pool.append(({**node, "name": candidate}, result.name))
    return pool


# ---------------------------------------------------------------------------
# Speed test (ChatGPT trace) via mihomo controller
# ---------------------------------------------------------------------------

def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_ports(mixed_port: int, controller_port: int) -> tuple[int, int]:
    for offset in range(0, 60, 2):
        mixed, controller = mixed_port + offset, controller_port + offset
        if _port_free(mixed) and _port_free(controller):
            return mixed, controller
    raise SyncError("no_free_port")


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


def test_isolated(core: str, names: list[str], proxies: list[dict], mixed_port: int,
                  controller_port: int, target: str, timeout_ms: int, workers: int,
                  log: Logger) -> dict[str, int | None]:
    if not Path(core).exists():
        raise SyncError("core_missing")
    mixed, controller = pick_ports(mixed_port, controller_port)
    runtime = Path(tempfile.mkdtemp(prefix="mihomo-feishu-"))
    api = f"http://127.0.0.1:{controller}"
    config = {
        "mixed-port": mixed,
        "allow-lan": False,
        "mode": "global",
        "log-level": "warning",
        "ipv6": False,
        "external-controller": f"127.0.0.1:{controller}",
        "dns": {"enable": False},
        "tun": {"enable": False},
        "proxies": proxies,
        "proxy-groups": [{"name": "TEST", "type": "select", "proxies": names}],
        "rules": ["MATCH,TEST"],
    }
    config_path = runtime / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [core, "-d", str(runtime), "-f", str(config_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SyncError("core_exited")
            try:
                with urllib.request.urlopen(f"{api}/version", timeout=1):
                    ready = True
                    break
            except Exception:
                time.sleep(0.3)
        if not ready:
            raise SyncError("core_not_ready")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            delays = dict(
                zip(names, pool.map(lambda n: request_delay(api, n, target, timeout_ms), names))
            )
        return delays
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(runtime, ignore_errors=True)


def test_live(api: str, token: str | None, names: list[str], target: str,
              timeout_ms: int, workers: int, log: Logger) -> dict[str, int | None]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        request = urllib.request.Request(f"{api}/proxies", headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        raise SyncError("controller_unreachable") from None
    available = set((data.get("proxies") or {}).keys())
    testable = [name for name in names if name in available]
    log.info(f"live controller available={len(available)} testable={len(testable)}")
    if not testable:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        delays = dict(
            zip(
                testable,
                pool.map(lambda n: request_delay(api, n, target, timeout_ms, token), testable),
            )
        )
    return delays


# ---------------------------------------------------------------------------
# Region / proxy-group inference
# ---------------------------------------------------------------------------

def region_of(name: str) -> str | None:
    if SG_PATTERN.search(name):
        return "新加坡"
    if US_PATTERN.search(name):
        return "美国"
    return None


def load_group_members(config_path: str, log: Logger) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    path = Path(config_path)
    if not path.exists():
        return groups
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        log.warn("config parse failed class=parse_error")
        return groups
    for group in (data or {}).get("proxy-groups") or []:
        if not isinstance(group, dict):
            continue
        name = group.get("name")
        members = group.get("proxies")
        if isinstance(name, str) and isinstance(members, list):
            groups[name] = [m for m in members if isinstance(m, str)]
    return groups


def pick_group(node_name: str, region: str, groups: dict[str, list[str]]) -> str:
    keywords = ("SINGAPORE", "SG", "新加坡", "狮城") if region == "新加坡" else (
        "US", "USA", "AMERICA", "GPT", "美国"
    )
    matched = [
        name for name, members in groups.items()
        if node_name in members and any(keyword in name.upper() for keyword in keywords)
    ]
    if matched:
        return "/".join(sorted(matched)[:3])
    return "SINGAPORE" if region == "新加坡" else "US-FAST"


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------

def compact(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None and v != ""}


def build_source_records(results: list[SourceResult], sync_ms: int) -> list[dict]:
    records = []
    for result in results:
        fields = compact(
            {
                "源名称": result.name,
                "源编号": result.index,
                "HTTP状态": result.http_status or 0,
                "拉取结果": result.result,
                "节点数": len(result.nodes),
                "存活数": result.alive,
                "流量剩余": result.traffic,
                "到期时间": result.expire,
                "失败原因": result.error,
                "同步时间": sync_ms,
            }
        )
        records.append({"fields": fields})
    return records


def build_node_records(pool: list[tuple[dict, str]], delays: dict[str, int | None],
                       target: str, sync_ms: int) -> list[dict]:
    records = []
    for node, source in pool:
        name = node["name"]
        delay = delays.get(name)
        fields = compact(
            {
                "节点名": name,
                "来源": source or "未知",
                "协议": str(node.get("type") or ""),
                "延迟ms": delay if delay else -1,
                "存活": bool(delay),
                "测速目标": target,
                "同步时间": sync_ms,
            }
        )
        records.append({"fields": fields})
    return records


def build_region_records(pool: list[tuple[dict, str]], delays: dict[str, int | None],
                         groups: dict[str, list[str]], sync_ms: int) -> list[dict]:
    records = []
    for node, source in pool:
        name = node["name"]
        delay = delays.get(name)
        if not delay:
            continue
        region = region_of(name)
        if not region:
            continue
        fields = compact(
            {
                "节点名": name,
                "地区组": region,
                "来源": source or "未知",
                "延迟ms": delay,
                "GPT可用": True,
                "代理组": pick_group(name, region, groups),
                "同步时间": sync_ms,
            }
        )
        records.append({"fields": fields})
    return records


# ---------------------------------------------------------------------------
# Table write (one batch per run)
# ---------------------------------------------------------------------------

def replace_records(client: FeishuClient, table_id: str, records: list[dict],
                    log: Logger) -> int:
    existing = client.list_records(table_id)
    ids = [r.get("record_id") for r in existing if r.get("record_id")]
    if ids:
        deleted = client.batch_delete(table_id, ids)
        log.info(f"records deleted={deleted}")
    if records:
        created = client.batch_create(table_id, records)
        log.info(f"records created={created}")
    else:
        created = 0
    return created


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync mihomo status into a Feishu Bitable.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="File holding FEISHU_APP_ID/SECRET")
    parser.add_argument("--app-token", default=DEFAULT_APP_TOKEN)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    parser.add_argument("--sources-env", default=DEFAULT_SOURCES_ENV)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--timeout-ms", type=int, default=7000)
    parser.add_argument("--fetch-timeout", type=int, default=30)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--core", default=DEFAULT_CORE)
    parser.add_argument("--mixed-port", type=int, default=17890)
    parser.add_argument("--controller-port", type=int, default=19090)
    parser.add_argument("--live-controller", default=DEFAULT_LIVE_CONTROLLER)
    parser.add_argument("--live-env", default=DEFAULT_LIVE_ENV)
    parser.add_argument("--config", default=r"C:\mihomo\config.yaml")
    parser.add_argument("--fetch-proxy", default=DEFAULT_FETCH_PROXY)
    parser.add_argument("--source1-cache", action="append", default=None,
                        help="Candidate YAML used when source 1 fails (repeatable)")
    parser.add_argument("--limit", type=int, default=0, help="Test at most N nodes (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Collect and test only; never write")
    parser.add_argument("--ensure-tables", action="store_true", help="Only create/repair tables")
    parser.add_argument("--no-speedtest", action="store_true")
    parser.add_argument("--keep-placeholder", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def print_summary(tables: dict[str, dict[str, str]], script_path: str, results: list[SourceResult],
                  node_rows: int, region_rows: int, error: str) -> None:
    labels = {"sources": "表1 订阅源状态", "nodes": "表2 可测速节点", "gpt_regions": "表3 GPT可用地区组"}
    for key, label in labels.items():
        entry = tables.get(key) or {}
        print(f"{label}\ttable_id={entry.get('table_id', '?')}\t{entry.get('name', '')}")
    print(f"脚本\t{script_path}")
    source1 = next((r for r in results if r.index == 1), None)
    http1 = source1.http_status if source1 else 0
    print(f"试跑\t表1={len(results)}行\t源1HTTP={http1}\t表2={node_rows}行\t表3={region_rows}行\t错误={error or 'none'}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = parse_args(argv)
    log = Logger(args.log_file, verbose=args.verbose)
    log.info("sync start")

    tables = load_state(args.state_file).get("tables") or {}
    results: list[SourceResult] = []
    node_rows = 0
    region_rows = 0
    error = ""
    sync_ms = int(time.time() * 1000)

    try:
        env = read_env_file(args.env_file)
        app_id = (env.get("FEISHU_APP_ID") or "").strip()
        app_secret = (env.get("FEISHU_APP_SECRET") or "").strip()
        if not app_id or not app_secret:
            raise SyncError("token_error", "credentials_missing")
        client = FeishuClient(app_id, app_secret, args.app_token, log)

        if args.dry_run:
            log.info("dry run: table provisioning skipped")
            if not tables:
                log.warn("dry run without state file; table ids unknown")
        else:
            tables = ensure_tables(client, args.state_file, args.keep_placeholder, log)
        if args.ensure_tables:
            print_summary(tables, str(Path(__file__).resolve()), [], 0, 0, "")
            log.info("ensure-tables done")
            return 0

        # --- sources -----------------------------------------------------
        sources_env = read_env_file(args.sources_env)
        for index in (1, 2, 3):
            url = (sources_env.get(f"SUBSCRIPTION_URL_{index}") or "").strip()
            ua = (sources_env.get(f"SUBSCRIPTION_UA_{index}") or "").strip() or convert_sub.DEFAULT_USER_AGENT
            result = collect_source(index, url, ua, args.fetch_timeout, args.fetch_proxy, log)
            results.append(result)

        # --- source 1 cache fallback ------------------------------------
        first = next((r for r in results if r.index == 1), None)
        if first is not None and first.result == "失败":
            cache_paths = args.source1_cache or [
                r"C:\mihomo\sub1_check.txt",
                r"C:\mihomo\providers\sub1-cache.yaml",
                r"C:\mihomo\providers\sub1_check.yaml",
                r"C:\mihomo\providers\source1.yaml",
                r"C:\mihomo\sub1_check.yaml",
                r"C:\mihomo\providers\sub1_check.txt",
            ]
            cached = load_cache_nodes(cache_paths, log)
            if cached:
                first.nodes = cached
                first.result = "缓存"
                first.error = f"{first.error or 'fetch_failed'}; using cache"

        # --- pool + speed test ------------------------------------------
        pool = build_pool(results)
        if args.limit and len(pool) > args.limit:
            pool = pool[: args.limit]
        log.info(f"pool nodes={len(pool)}")

        delays: dict[str, int | None] = {}
        if pool and not args.no_speedtest:
            names = [node["name"] for node, _ in pool]
            proxies = [{k: v for k, v in node.items() if not k.startswith("_")} for node, _ in pool]
            try:
                delays = test_isolated(args.core, names, proxies, args.mixed_port,
                                       args.controller_port, args.target, args.timeout_ms,
                                       args.workers, log)
                log.info(f"speedtest isolated done tested={len(delays)}")
            except SyncError as exc:
                log.warn(f"speedtest isolated failed class={exc.kind}; trying live controller")
                live_env = read_env_file(args.live_env)
                token = (live_env.get("SECRET") or "").strip() or None
                try:
                    delays = test_live(args.live_controller, token, names, args.target,
                                       args.timeout_ms, args.workers, log)
                    log.info(f"speedtest live done tested={len(delays)}")
                except SyncError as live_exc:
                    log.error(f"speedtest unavailable class={live_exc.kind}")
                    delays = {}
        for node, _ in pool:
            node["_delay"] = delays.get(node["name"])

        # Backfill delays onto each source's own node list so 表1「存活数」 matches 表2.
        delay_by_endpoint: dict[tuple, int | None] = {}
        for node, _source_name in pool:
            key = (node.get("type"), node.get("server"), node.get("port"))
            delay_by_endpoint[key] = delays.get(node["name"])
        for result in results:
            for node in result.nodes:
                key = (node.get("type"), node.get("server"), node.get("port"))
                if key in delay_by_endpoint:
                    node["_delay"] = delay_by_endpoint[key]

        # --- records -----------------------------------------------------
        source_records = build_source_records(results, sync_ms)
        node_records = build_node_records(pool, delays, args.target, sync_ms)
        groups = load_group_members(args.config, log)
        region_records = build_region_records(pool, delays, groups, sync_ms)
        node_rows = len(node_records)
        region_rows = len(region_records)
        alive_total = sum(1 for _, d in delays.items() if d)
        log.info(f"rows sources={len(source_records)} nodes={node_rows} regions={region_rows} "
                 f"alive={alive_total}")

        if args.dry_run:
            log.info("dry run: no write")
            print_summary(tables, str(Path(__file__).resolve()), results, node_rows, region_rows, "")
            return 0

        # --- write -------------------------------------------------------
        for key, records in (("sources", source_records), ("nodes", node_records),
                             ("gpt_regions", region_records)):
            table_id = (tables.get(key) or {}).get("table_id")
            if not table_id:
                raise SyncError("api_error", f"missing_table_id:{key}")
            count = replace_records(client, table_id, records, log)
            log.info(f"table {key} rows={count}")

        log.info("sync ok")
        print_summary(tables, str(Path(__file__).resolve()), results, node_rows, region_rows, "")
        return 0

    except SyncError as exc:
        error = exc.kind
        log.error(f"sync failed class={exc.kind} detail={exc.detail}")
    except Exception as exc:  # pragma: no cover - defensive
        error = type(exc).__name__.lower()
        log.error(f"sync failed class={error}")

    print_summary(tables, str(Path(__file__).resolve()), results, node_rows, region_rows, error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
