#!/usr/bin/env python3
"""Publish one live-status row into the Feishu Bitable table 实时状态 (DIAN-18).

Reads a live_status_collect.py JSON document plus a live_status_alert.py
evaluation (imported, or read from its --out JSON) and upserts exactly ONE row
keyed ``key=live``: update when the row exists, create when it does not. An
update never adds a row; if several ``key=live`` rows are found, only the
earliest is updated and the duplication is logged as a warning. Unknown
numbers are omitted from both payloads (null is never sent); unknown text is
``""``, which clears the cell on update.

Modes: ``--dry-run`` (default) only prints the row it would write; ``--apply``
writes it; ``--init`` provisions the table idempotently (create when missing,
otherwise only add missing fields). Development and tests must stay on
dry-run/fake clients — never point ``--init`` or ``--apply`` at real Feishu.

Defaults: log ``C:\mihomo\logs\live-status-publish.log``, lock
``C:\mihomo\live-status-publish.lock`` (``--log-file``/``--lock-file`` override).
A dry-run writes the log — use ``--log-file`` to send it elsewhere.

Only text and number fields are used. Every displayed time is Beijing-time
text: aware UTC stamps (ippool ``updated_at``) are converted, naive stamps are
read as Beijing time. Tokens, secrets and subscription URLs are never printed,
logged or written.

The exit code is 0 except when Feishu credentials are missing
(``credentials_missing`` exits 1, so a misconfigured scheduled task is visible
to the scheduler). A failed collection or evaluation still writes a
best-effort 数据陈旧 row (状态等级=严重) rather than raising. A failed
collection row names the failure category in its summary (api_unreachable,
timeout, invalid_json, ...) — never a URL, token or raw error text.

Bitable calls retry not only the Feishu business codes but also HTTP 429/5xx,
timeouts and connection errors: at most 4 attempts, 1/2/4s backoff. Retry logs
carry only the fixed error kind.

Nothing here starts a temporary mihomo, calls /configs, or probes any delay
endpoint: collection is delegated to live_status_collect (GET /proxies plus
local state files only). A dedicated named mutex/lock file serializes this
publisher; it is deliberately NOT shared with the main bitable sync.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent

BEIJING_TZ = timezone(timedelta(hours=8))

# Kept for compatibility; the token itself is resolved by resolve_app_token,
# never hardcoded.
DEFAULT_APP_TOKEN = None
APP_TOKEN_ENV_KEY = "FEISHU_BITABLE_APP_TOKEN"
DEFAULT_ENV_FILE = r"C:\Users\Administrator\.hermes\.env"
DEFAULT_LOG_FILE = r"C:\mihomo\logs\live-status-publish.log"
DEFAULT_LOCK_FILE = r"C:\mihomo\live-status-publish.lock"
DEFAULT_ALERT_STATE_FILE = r"C:\mihomo\state\live-status-publish-alert-state.json"
DEFAULT_PUBLISH_STATE_FILE = r"C:\mihomo\state\live-status-publish-state.json"
DEFAULT_STALE_MINUTES = 15.0

TABLE_NAME = "实时状态"
KEY_FIELD = "key"
KEY_VALUE = "live"
NOTE_TEXT = "生产实时状态，来源本机9090/proxies，非独立内核测速"

TASK_NAMES = ("MihomoFeishuBitableSync", "MihomoXiguaIpPool", "MihomoNinjaGrokFeishuSync")
TASK_RUNNING_CODE_MIN = 267008
TASK_RUNNING_CODE_MAX = 267055

FT_TEXT = 1
FT_NUMBER = 2
NUMBER_PROPERTY = {"formatter": "0"}

F_KEY = "key"
F_UPDATED = "更新时间"
F_LEVEL = "状态等级"
F_SUMMARY = "一句话摘要"
F_REASONS = "告警原因"
F_GPT_NOW = "GPT当前节点"
F_GPT_USABLE = "GPT可用数"
F_USFAST_NOW = "US-FAST当前节点"
F_GROKBOT_NOW = "GROKBOT当前节点"
F_GROKAPI_NOW = "GROKAPI当前节点"
F_SYNC_SUCCESS = "同步最近成功时间（北京）"
F_SYNC_FAILURES = "同步连续失败数"
F_IPPOOL_UPDATED = "IP池更新时间（北京）"
F_HEALTHY_IPS = "健康IP数"
F_TASKS = "计划任务结果摘要"
F_STALE = "数据是否陈旧"
F_NOTE = "备注"

TABLE_FIELDS: list[tuple[str, int, dict | None]] = [
    (F_KEY, FT_TEXT, None),
    (F_UPDATED, FT_TEXT, None),
    (F_LEVEL, FT_TEXT, None),
    (F_SUMMARY, FT_TEXT, None),
    (F_REASONS, FT_TEXT, None),
    (F_GPT_NOW, FT_TEXT, None),
    (F_GPT_USABLE, FT_NUMBER, NUMBER_PROPERTY),
    (F_USFAST_NOW, FT_TEXT, None),
    (F_GROKBOT_NOW, FT_TEXT, None),
    (F_GROKAPI_NOW, FT_TEXT, None),
    (F_SYNC_SUCCESS, FT_TEXT, None),
    (F_SYNC_FAILURES, FT_NUMBER, NUMBER_PROPERTY),
    (F_IPPOOL_UPDATED, FT_TEXT, None),
    (F_HEALTHY_IPS, FT_NUMBER, NUMBER_PROPERTY),
    (F_TASKS, FT_TEXT, None),
    (F_STALE, FT_TEXT, None),
    (F_NOTE, FT_TEXT, None),
]

# Rate-limit / transient API codes returned by the Bitable endpoints, and the
# token-expiry codes: same sets the main sync retries on.
RETRY_CODES = {1254290, 1254291, 1254607, 99991400, 1310213}
TOKEN_CODES = {99991661, 99991663, 99991664, 99991668}

# HTTP/transport failures retried by call() with the same backoff as RETRY_CODES.
RETRYABLE_HTTP_KINDS = {"http_429", "http_5xx", "timeout", "network_error"}

# Windows named-mutex constants (a dedicated name, not the main sync's).
PUBLISH_MUTEX_NAME = r"Local\mihomo-live-status-publish"
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PublishError(Exception):
    """Failure carrying a classification string that is safe to log."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# logging (never carries secrets, tokens or subscription URLs)
# ---------------------------------------------------------------------------

class Logger:
    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, path: str, verbose: bool = False) -> None:
        self.path = Path(path)
        self.verbose = verbose
        self.write_failures = 0
        self.last_write_error = ""
        self._ready = False
        self._degrade_reported = False

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
                    self._report_write_failure(OSError("rotation renamed nothing"),
                                               "rotation blocked")
        except OSError as exc:
            self._report_write_failure(exc, "log dir setup blocked")

    def _report_write_failure(self, exc: OSError, what: str) -> None:
        self.write_failures += 1
        self.last_write_error = (f"{type(exc).__name__}(errno={exc.errno}) "
                                 f"at {self.path} {what}")
        if self._degrade_reported:
            return
        self._degrade_reported = True
        stream = sys.stderr
        if stream is None:
            return
        try:
            stream.write(f"{self._timestamp()} ERROR LOG-DEGRADED {self.last_write_error}\n")
            stream.flush()
        except (OSError, ValueError):
            pass

    def _timestamp(self) -> str:
        return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

    def _write(self, level: str, message: str) -> None:
        line = f"{self._timestamp()} {level} {message}"
        self._open()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            self._report_write_failure(exc, "append blocked")
            return
        if self.verbose:
            print(line, file=sys.stderr)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def exception(self, message: str) -> None:
        self._write("ERROR", f"{message}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# time helpers (everything displayed is Beijing-time text)
# ---------------------------------------------------------------------------

def _parse_iso(text):
    """Parse an ISO-8601 stamp; naive is read as Beijing time. None on failure."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = re.sub(r"(\.\d{6})\d+", r"\1", text.strip())
    try:
        parsed = datetime.fromisoformat(t)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed


def _beijing_text(moment) -> str:
    return moment.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _beijing_or_unknown(text) -> str:
    """Render any timestamp as Beijing text; 未知 when absent/unparsable."""
    parsed = _parse_iso(text)
    return _beijing_text(parsed) if parsed is not None else "未知"


def _collected_moment(collected):
    """The collection instant as an aware datetime, or None."""
    raw = collected.get("collected_at") if isinstance(collected, dict) else None
    if isinstance(raw, dict):
        candidates = (raw.get("utc"), raw.get("beijing"))
    elif isinstance(raw, str):
        candidates = (raw,)
    else:
        candidates = ()
    for text in candidates:
        parsed = _parse_iso(text)
        if parsed is not None:
            return parsed
    return None


# ---------------------------------------------------------------------------
# single-instance lock (dedicated mutex; NOT shared with the main bitable sync)
# ---------------------------------------------------------------------------

def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k32.ReleaseMutex.restype = wintypes.BOOL
    return k32


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        k32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # ACCESS_DENIED => it exists


class PublishLock:
    """Best-effort single-instance guard. ``acquire`` returns False when busy."""

    def __init__(self, path: str, log: Logger, *, mutex_name: str = PUBLISH_MUTEX_NAME) -> None:
        self.path = Path(path)
        self.log = log
        self.mutex_name = mutex_name
        self._handle: int | None = None
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            if sys.platform == "win32":
                return self._acquire_mutex()
            return self._acquire_file()
        except OSError as exc:
            self.log.error(f"lock error path={self.path} reason={type(exc).__name__}")
            return False

    def _acquire_mutex(self) -> bool:
        import ctypes

        k32 = _kernel32()
        handle = k32.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            self.log.error(f"lock busy path={self.path} reason=mutex_create_failed "
                           f"err={ctypes.get_last_error()}")
            return False
        rc = k32.WaitForSingleObject(handle, 0)
        if rc not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            k32.CloseHandle(handle)
            self.log.warn(f"lock busy path={self.path} reason="
                          f"{'mutex_timeout' if rc == WAIT_TIMEOUT else f'mutex_wait rc={rc}'}")
            return False
        if rc == WAIT_ABANDONED:
            self.log.warn(f"lock takeover path={self.path} reason=abandoned_mutex")
        self._handle = int(handle)
        self._write_meta()
        return True

    def _acquire_file(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = self._read_meta_pid()
            if holder is not None and _pid_alive(holder):
                self.log.warn(f"lock busy path={self.path} reason=held pid={holder}")
                return False
            self.log.warn(f"lock takeover path={self.path} reason=stale_pid")
            try:
                self.path.unlink()
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except OSError:
                return False
        self._fd = fd
        self._write_meta(fd=fd)
        return True

    def _read_meta_pid(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = data.get("pid") if isinstance(data, dict) else None
        return int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None

    def _write_meta(self, fd: int | None = None) -> None:
        payload = json.dumps({"pid": os.getpid(), "since": datetime.now(BEIJING_TZ).isoformat()},
                             ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if fd is not None:
                os.write(fd, payload.encode("utf-8"))
            else:
                self.path.write_text(payload, encoding="utf-8")
        except OSError:
            pass  # metadata is advisory; the mutex/file is the real guard

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            try:
                self.path.unlink()
            except OSError:
                pass
        if self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                k32 = _kernel32()
                k32.ReleaseMutex(handle)
                k32.CloseHandle(handle)
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Feishu client (token acquisition + retry mirror sync-feishu-bitable.py)
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


def resolve_app_token(cli_token: str | None, env: dict[str, str]) -> str:
    """Bitable app token: --app-token > process env > env file; "" when absent.

    The returned value is a credential: never log or print it.
    """
    for candidate in (cli_token, os.environ.get(APP_TOKEN_ENV_KEY), env.get(APP_TOKEN_ENV_KEY)):
        value = (candidate or "").strip()
        if value:
            return value
    return ""


class FeishuClient:
    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, app_token: str, log: Logger) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.log = log
        self._token: str | None = None
        self._expires_at = 0.0

    def _raw(self, method: str, url: str, body: dict | None, headers: dict[str, str],
             timeout: int = 30) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # The HTTP body may echo request details; only the status is kept.
            # 429/5xx are retryable and classified as such; other codes are not.
            if exc.code == 429:
                raise PublishError("http_429") from None
            if 500 <= exc.code < 600:
                raise PublishError("http_5xx") from None
            raise PublishError(f"http_{exc.code}") from None
        except (socket.timeout, TimeoutError):
            raise PublishError("timeout") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise PublishError("timeout") from None
            raise PublishError("network_error") from None
        except Exception as exc:  # pragma: no cover - defensive
            raise PublishError(type(exc).__name__.lower()) from None
        try:
            return json.loads(raw or "{}")
        except ValueError:
            raise PublishError("bad_response") from None

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
            raise PublishError("token_error", f"code={code}")
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
            auth_error = None
            net_error = None
            payload = None
            try:
                headers = {
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self.token()}",
                }
                payload = self._raw(method, url, body, headers)
            except PublishError as exc:
                # An auth failure inside token acquisition refreshes and retries
                # like before; HTTP 429/5xx, timeouts and connection errors get
                # the same bounded backoff as the retry codes. The kind is a
                # fixed classification: no URL, token or secret ever escapes.
                if exc.kind == "token_error":
                    auth_error = exc
                    self._token = None
                elif exc.kind in RETRYABLE_HTTP_KINDS:
                    net_error = exc
                else:
                    raise
            if auth_error is None and payload is not None:
                code = payload.get("code", 0)
                if code == 0:
                    return payload.get("data") or {}
                if code not in TOKEN_CODES and code not in RETRY_CODES:
                    raise PublishError("api_error", f"code={code}")
                if code in TOKEN_CODES:
                    self._token = None
            if attempt >= 3:
                if auth_error is not None:
                    raise auth_error
                if net_error is not None:
                    raise (PublishError("rate_limited", "kind=http_429")
                           if net_error.kind == "http_429" else
                           PublishError("network_failure", f"kind={net_error.kind}"))
                if code in TOKEN_CODES:
                    raise PublishError("token_error", f"code={code}")
                raise PublishError("rate_limited", f"code={code}")
            if auth_error is not None:
                time.sleep(0.5)
            elif net_error is None and code in TOKEN_CODES:
                time.sleep(0.5)
            else:
                time.sleep(delay)
                delay *= 2
        raise PublishError("api_error", "retry_exhausted")

    # -- tables / fields ---------------------------------------------------

    def list_tables(self) -> list[dict]:
        tables: list[dict] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
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
            raise PublishError("api_error", "create_table_no_id")
        return table_id

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

    def create_record(self, table_id: str, fields: dict) -> str:
        data = self.call(
            "POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
            body={"fields": fields},
        )
        record = data.get("record") or {}
        return str(record.get("record_id") or data.get("record_id") or "")

    def update_record(self, table_id: str, record_id: str, fields: dict) -> None:
        self.call(
            "PUT", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/{record_id}",
            body={"fields": fields},
        )


def build_client(args, log: Logger) -> FeishuClient:
    env = read_env_file(args.env_file)
    app_id = (env.get("FEISHU_APP_ID") or "").strip()
    app_secret = (env.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise PublishError("credentials_missing", "FEISHU_APP_ID/FEISHU_APP_SECRET not set")
    app_token = resolve_app_token(args.app_token, env)
    if not app_token:
        raise PublishError("credentials_missing",
                           "FEISHU_BITABLE_APP_TOKEN not set (--app-token / env / --env-file)")
    return FeishuClient(app_id, app_secret, app_token, log)


# ---------------------------------------------------------------------------
# row building
# ---------------------------------------------------------------------------

def _as_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _group(collected, name):
    groups = collected.get("groups") if isinstance(collected, dict) else None
    group = groups.get(name) if isinstance(groups, dict) else None
    return group if isinstance(group, dict) else None


def _group_now(collected, name):
    group = _group(collected, name)
    if group is None:
        return None
    now = group.get("now")
    return now if isinstance(now, str) and now.strip() else None


def _group_usable(collected, name):
    group = _group(collected, name)
    return _as_int(group.get("usable_count_by_history")) if group else None


def _state_entry(collected, key):
    entries = collected.get("state_files") if isinstance(collected, dict) else None
    entry = entries.get(key) if isinstance(entries, dict) else None
    return entry if isinstance(entry, dict) else None


def _sync_summary(collected):
    entry = _state_entry(collected, "feishu_bitable_sync_state")
    summary = entry.get("summary") if entry else None
    return summary if isinstance(summary, dict) else None


def _task_summary(collected):
    tasks = collected.get("scheduled_tasks") if isinstance(collected, dict) else None
    if not isinstance(tasks, dict):
        return None
    parts = []
    for name in TASK_NAMES:
        task = tasks.get(name)
        if not isinstance(task, dict):
            parts.append(f"{name}=缺失")
            continue
        code = _as_int(task.get("last_task_result"))
        if code is None:
            parts.append(f"{name}=未知")
        elif code == 0:
            parts.append(f"{name}=0")
        elif TASK_RUNNING_CODE_MIN <= code <= TASK_RUNNING_CODE_MAX:
            parts.append(f"{name}=运行中({code})")
        else:
            parts.append(f"{name}=失败({code})")
    return "；".join(parts)


def _reason_text(alert_result) -> str:
    reasons = alert_result.get("reasons") if isinstance(alert_result, dict) else None
    if not isinstance(reasons, list):
        return "无"
    lines = [str(r.get("text")) for r in reasons if isinstance(r, dict) and r.get("text")]
    return "\n".join(lines) if lines else "无"


def _level(alert_result) -> str:
    level = alert_result.get("level") if isinstance(alert_result, dict) else None
    return level if level in ("正常", "降级", "严重") else "严重"


def is_stale(collected, collected_moment, now, stale_minutes) -> bool:
    """True when this row must be flagged 数据陈旧."""
    if not isinstance(collected, dict) or not collected:
        return True
    if collected.get("ok") is not True:
        return True
    if collected_moment is None:
        return True
    return (now - collected_moment).total_seconds() > stale_minutes * 60.0


# Collector error strings are "<site>: <sanitized exception name or HTTPError
# <code>>"; the mapping keeps only fixed categories. Anything unmappable stays
# "unknown" — error text from a document supplied via --collect-json is never
# echoed into the row.
_FAILURE_CATEGORIES = {
    "connectionrefusederror": "api_unreachable",
    "connectionreseterror": "api_unreachable",
    "connectionabortederror": "api_unreachable",
    "remoteendpointnotfounderror": "connection_refused",
    "unknownhosterror": "dns_failure",
    "gaierror": "dns_failure",
    "timeouterror": "timeout",
    "readtimeout": "timeout",
    "sockettimeout": "timeout",
    "sslerror": "tls_failure",
    "oserror": "network_error",
    "filenotfounderror": "state_file_missing",
    "permissionerror": "state_file_unreadable",
    "isadirectoryerror": "state_file_unreadable",
    "jsondecodeerror": "invalid_json",
    "unicodeerror": "invalid_json",
    "valueerror": "invalid_json",
    "httperror": "server_error",
}


def _failure_category(token: str) -> str | None:
    token = token.strip().lower()
    if token.startswith("httperror"):
        parts = token.split()
        try:
            code = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            code = 0
        if code == 429:
            return "rate_limited"
        if 500 <= code < 600:
            return "server_error"
        return "client_error"
    if not token or " " in token:
        # Never a known classification token: refuse to echo anything else.
        return None
    return _FAILURE_CATEGORIES.get(token)


def _error_category(error) -> str | None:
    """Category from a collector error '<site>: <sanitized message>'."""
    if not isinstance(error, str) or ":" not in error:
        return None
    return _failure_category(error.split(":", 1)[1])


def collect_failure_category(collected) -> str:
    """One safe category for why this round failed (never raw text or URLs)."""
    if not isinstance(collected, dict) or not collected:
        return "collect_document_missing"
    if collected.get("collected_at") is None:
        return "collect_document_incomplete"
    categories = []
    api = collected.get("api")
    if isinstance(api, dict) and api.get("reachable") is False:
        categories.extend(_error_category(e) for e in api.get("errors") or [])
    if not categories:
        errors = collected.get("errors")
        if isinstance(errors, list):
            categories.extend(
                _error_category(e)
                for e in errors
                if isinstance(e, str)
                and (e.startswith("api.") or e.startswith("state_files.")))
    for category in categories:
        if category is not None and category != "client_error":
            return category
    if any(category is not None for category in categories):
        return "client_error"
    return "unknown"


def build_row(collected, alert_result, *, now=None, stale_minutes=DEFAULT_STALE_MINUTES,
              last_ok_collected_at=None) -> dict:
    """The 实时状态 field dict for one round. Never raises on bad input.

    A collection that failed (or an evaluation that could not run) collapses to
    the prescribed stale row: 状态等级=严重, 数据是否陈旧=是 and a summary naming
    the last successful collection. Everything readable is still filled in.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, datetime) and now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING_TZ)
    if not isinstance(collected, dict):
        collected = {}

    moment = _collected_moment(collected)
    stale = is_stale(collected, moment, now, stale_minutes)
    collect_failed = collected.get("ok") is not True or moment is None
    level = _level(alert_result)
    if collect_failed:
        level = "严重"
        # Only a round that actually succeeded may be named as the last good
        # collection; a failed round's own stamp proves nothing.
        last_ok = (last_ok_collected_at.strip()
                   if isinstance(last_ok_collected_at, str) and last_ok_collected_at.strip()
                   else None)
        category = collect_failure_category(collected)
        summary = (f"采集失败（原因:{category}）/数据陈旧，"
                   f"最后成功采集于 {last_ok or '未知'}")
    else:
        summary = alert_result.get("summary") if isinstance(alert_result, dict) else None
        summary = summary if isinstance(summary, str) and summary.strip() else "无摘要"

    publish_text = _beijing_text(now)
    if moment is not None and (now - moment).total_seconds() >= 60:
        updated_text = f"{publish_text}（数据采集于 {_beijing_text(moment)}）"
    else:
        updated_text = publish_text

    sync = _sync_summary(collected) or {}
    ippool = _state_entry(collected, "ippool") or {}

    row: dict[str, Any] = {
        F_KEY: KEY_VALUE,
        F_UPDATED: updated_text,
        F_LEVEL: level,
        F_SUMMARY: summary,
        F_REASONS: _reason_text(alert_result),
        F_GPT_NOW: _group_now(collected, "GPT"),
        F_GPT_USABLE: _group_usable(collected, "GPT"),
        F_USFAST_NOW: _group_now(collected, "US-FAST"),
        F_GROKBOT_NOW: _group_now(collected, "GROKBOT"),
        F_GROKAPI_NOW: _group_now(collected, "GROKAPI"),
        F_SYNC_SUCCESS: _beijing_or_unknown(sync.get("last_success_time")),
        F_SYNC_FAILURES: _as_int(sync.get("consecutive_failures")),
        # ippool updated_at is written as UTC by ippool-collector; naive stamps
        # elsewhere in this ecosystem are Beijing local time.
        F_IPPOOL_UPDATED: _beijing_or_unknown(ippool.get("updated_at")),
        F_HEALTHY_IPS: _as_int(ippool.get("healthy_ip_count")),
        F_TASKS: _task_summary(collected),
        F_STALE: "是" if stale else "否",
        F_NOTE: NOTE_TEXT,
    }
    return _finalize_row(row)


def _finalize_row(row: dict) -> dict:
    """Every declared field, in table order: unknown text becomes "" so an update
    clears a cell instead of leaving the previous round's value on screen. Unknown
    numbers stay None — upsert_row omits them from both create and update."""
    final: dict[str, Any] = {}
    for name, ftype, _ in TABLE_FIELDS:
        value = row.get(name)
        if value is None and ftype == FT_TEXT:
            value = ""
        final[name] = value
    return final


# ---------------------------------------------------------------------------
# collect / alert inputs
# ---------------------------------------------------------------------------

def load_json_document(spec: str) -> dict | None:
    """Read one JSON object from a path or '-' (stdin). None when unusable."""
    try:
        text = sys.stdin.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def run_collector(args, log: Logger) -> dict:
    """Collect in-process via live_status_collect (GET /proxies + local files)."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import live_status_collect

    log.info("collect start (in-process, read-only)")
    return live_status_collect.collect(
        api_base=args.api, config_path=args.config, state_dir=args.state_dir,
        freshness_minutes=args.freshness_minutes, timeout=args.timeout)


def run_alert_evaluation(collected: dict, state_file: str, log: Logger, *,
                         save: bool = True) -> dict:
    """Evaluate with live_status_alert; only a real write may advance its state.

    A dry-run must not consume a round of the GPT zero-usable counter, so its
    evaluation stays read-only.
    """
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import live_status_alert

    prev_state = live_status_alert.load_state(state_file)
    result = live_status_alert.evaluate(collected, prev_state=prev_state)
    if not save:
        return result
    try:
        live_status_alert.save_state(state_file, result["next_state"])
    except OSError as exc:
        log.warn(f"alert state write failed reason={type(exc).__name__}")
    return result


# ---------------------------------------------------------------------------
# publish state (last successful collection, for the stale-row summary)
# ---------------------------------------------------------------------------

def load_publish_state(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_publish_state(path: str, state: dict) -> None:
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
# upsert
# ---------------------------------------------------------------------------

def _field_text(value) -> str:
    """Bitable text cells arrive as str or as a list of segment dicts."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    return ""


def find_key_records(records: list, key_value: str = KEY_VALUE) -> list[dict]:
    matches = []
    for record in records:
        if not isinstance(record, dict):
            continue
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if _field_text(fields.get(KEY_FIELD)) == key_value:
            matches.append(record)
    return matches


def earliest_record(records: list[dict]) -> dict:
    """The oldest matching row: created_time when every row carries one, else
    the API's own order (which is creation order)."""
    stamps = [record.get("created_time") for record in records]
    if all(isinstance(stamp, (int, float)) and not isinstance(stamp, bool) for stamp in stamps):
        return min(zip(stamps, range(len(records)), records), key=lambda item: item[:2])[2]
    return records[0]


def resolve_table_id(client, table_id: str | None, log: Logger) -> str:
    if table_id:
        return table_id
    for table in client.list_tables():
        if isinstance(table, dict) and table.get("name") == TABLE_NAME:
            found = table.get("table_id")
            if isinstance(found, str) and found:
                return found
    raise PublishError("table_missing", f"run --init to create {TABLE_NAME}")


def ensure_table(client, args, log: Logger) -> tuple[str, list[str]]:
    """--init: create the table when missing, otherwise only add missing fields."""
    created_fields: list[str] = []
    table_id = args.table_id
    existed = True
    if not table_id:
        remote = {t.get("name"): t.get("table_id") for t in client.list_tables()
                  if isinstance(t, dict)}
        table_id = remote.get(TABLE_NAME)
        if not table_id:
            existed = False
            table_id = client.create_table(TABLE_NAME, TABLE_FIELDS)
            log.info(f"init table created name={TABLE_NAME} table_id={table_id}")
    if existed:
        present = {f.get("field_name") for f in client.list_fields(table_id)
                   if isinstance(f, dict)}
        for name, ftype, prop in TABLE_FIELDS:
            if name in present:
                continue  # never touch an existing field
            client.create_field(table_id, name, ftype, prop)
            created_fields.append(name)
        log.info(f"init table checked name={TABLE_NAME} table_id={table_id} "
                 f"missing_fields_added={len(created_fields)}")
    return str(table_id), created_fields


def upsert_row(client, table_id: str, row: dict, log: Logger) -> str:
    """Update the single key=live row, or create it. Returns the action taken."""
    matches = find_key_records(client.list_records(table_id))
    payload = {name: value for name, value in row.items() if value is not None}
    if not matches:
        record_id = client.create_record(table_id, payload)
        log.info(f"row created key={KEY_VALUE} table_id={table_id} record_id={record_id}")
        return "created"
    if len(matches) > 1:
        log.warn(f"duplicate key rows key={KEY_VALUE} count={len(matches)} "
                 f"table_id={table_id} action=update_earliest_only")
    target = earliest_record(matches)
    record_id = str(target.get("record_id") or "")
    if not record_id:
        raise PublishError("api_error", "record_without_id")
    # Unknown numbers are omitted here too (an explicit null could wipe a cell
    # the fetch could not explain); unknown text is "" and still clears.
    client.update_record(table_id, record_id, payload)
    log.info(f"row updated key={KEY_VALUE} table_id={table_id} record_id={record_id} "
             f"matches={len(matches)}")
    return "updated"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_collector_defaults(parser) -> None:
    defaults = {"api": "http://127.0.0.1:9090", "config": r"C:\mihomo\config.yaml",
                "state_dir": r"C:\mihomo\state", "freshness": 15.0}
    try:
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import live_status_collect as collector

        defaults = {"api": collector.DEFAULT_API, "config": collector.DEFAULT_CONFIG,
                    "state_dir": collector.DEFAULT_STATE_DIR,
                    "freshness": collector.DEFAULT_FRESHNESS_MINUTES}
    except Exception:
        pass
    parser.add_argument("--api", default=defaults["api"],
                        help="mihomo controller base URL for the in-process collect")
    parser.add_argument("--config", default=defaults["config"],
                        help="mihomo config.yaml (secret read in memory only)")
    parser.add_argument("--state-dir", default=defaults["state_dir"], help="local state directory")
    parser.add_argument("--freshness-minutes", type=float, default=defaults["freshness"],
                        help="history freshness window passed to the collector")
    parser.add_argument("--timeout", type=float, default=5, help="per-request timeout seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upsert the single 实时状态 row in Feishu Bitable "
                    "(exit 1 only when Feishu credentials are missing).",
        epilog=(f"default --log-file {DEFAULT_LOG_FILE} and --lock-file {DEFAULT_LOCK_FILE}; "
                "a --dry-run run still writes the log (redirect it with --log-file); "
                "Feishu calls retry HTTP 429/5xx, timeouts and connection errors "
                "with a 1/2/4s backoff (4 attempts max)."))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true",
                      help="idempotent provisioning: create the table if missing, else only add "
                           "missing fields (no row is written)")
    mode.add_argument("--dry-run", action="store_true",
                      help="print the row that would be written (default)")
    mode.add_argument("--apply", action="store_true", help="really write the row to Feishu")
    parser.add_argument("--collect-json", default=None,
                        help="live_status_collect.py JSON path, or '-' for stdin "
                             "(default: collect in-process, read-only)")
    parser.add_argument("--alert-json", default=None,
                        help="live_status_alert.py --out JSON to reuse instead of evaluating")
    parser.add_argument("--alert-state-file", default=DEFAULT_ALERT_STATE_FILE,
                        help="alert round-counter state used when this script evaluates")
    parser.add_argument("--publish-state-file", default=DEFAULT_PUBLISH_STATE_FILE,
                        help="remembers the last successfully published collection time")
    parser.add_argument("--stale-minutes", type=float, default=DEFAULT_STALE_MINUTES,
                        help=f"data age above which 数据是否陈旧=是 (default {DEFAULT_STALE_MINUTES})")
    parser.add_argument("--app-token", default=DEFAULT_APP_TOKEN,
                        help="Bitable app token (overrides env FEISHU_BITABLE_APP_TOKEN and the env file)")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help="file holding FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_BITABLE_APP_TOKEN")
    parser.add_argument("--table-id", default=None,
                        help=f"skip the {TABLE_NAME} table lookup and use this table_id")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                        help=f"log file path, also written by dry-run (default {DEFAULT_LOG_FILE})")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE,
                        help=f"lock file path (default {DEFAULT_LOCK_FILE})")
    parser.add_argument("--verbose", action="store_true", help="mirror log lines to stderr")
    _add_collector_defaults(parser)
    return parser


def _print_json(payload: dict) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_round_row(args, log: Logger) -> tuple[dict, dict, datetime]:
    """Collect (or read the given JSON), evaluate, and build the row.

    Never raises: any collection or evaluation failure collapses into the
    stale/severe fallback row required by the spec.
    """
    now = datetime.now(timezone.utc)
    last_ok = load_publish_state(args.publish_state_file).get("last_ok_collected_at")

    collected = None
    collect_error = ""
    if args.collect_json:
        collected = load_json_document(args.collect_json)
        if collected is None:
            collect_error = "collect_json_unusable"
    else:
        try:
            collected = run_collector(args, log)
        except Exception as exc:
            collect_error = f"collector_{type(exc).__name__}"
            log.exception("collector raised")
    if collected is None and not collect_error:
        collect_error = "collect_document_empty"
    elif isinstance(collected, dict) and not collected and not collect_error:
        collect_error = "collect_document_empty"

    alert_result = None
    if isinstance(collected, dict) and collected:
        if args.alert_json:
            alert_result = load_json_document(args.alert_json)
            if alert_result is None:
                log.error("alert json unusable; evaluating in-process instead")
        if alert_result is None:
            try:
                alert_result = run_alert_evaluation(collected, args.alert_state_file, log,
                                                    save=bool(args.apply))
            except Exception:
                log.exception("alert evaluation raised")
                alert_result = None
    if alert_result is None:
        if collect_error:
            log.error(f"fallback row reason={collect_error}")
        alert_result = {"level": "严重", "summary": "", "reasons": []}

    row = build_row(collected if isinstance(collected, dict) else {}, alert_result,
                    now=now, stale_minutes=args.stale_minutes, last_ok_collected_at=last_ok)
    return row, collected if isinstance(collected, dict) else {}, now


def publish(args, log: Logger, client_factory: Callable[[Any, Logger], Any]) -> str:
    """One full round. Returns a short result token for the end log line."""
    if args.init:
        client = client_factory(args, log)
        table_id, added = ensure_table(client, args, log)
        _print_json({"mode": "init", "table": TABLE_NAME, "table_id": table_id,
                     "fields_added": added})
        return "init_ok"

    row, collected, now = build_round_row(args, log)

    if not args.apply:
        _print_json({"mode": "dry-run", "action": "upsert", "table": TABLE_NAME,
                     "table_id": args.table_id, "row": row})
        log.info(f"dry-run row built level={row.get(F_LEVEL)} stale={row.get(F_STALE)}")
        return "dry_run_ok"

    client = client_factory(args, log)
    table_id = resolve_table_id(client, args.table_id, log)
    action = upsert_row(client, table_id, row, log)
    moment = _collected_moment(collected)
    if row.get(F_STALE) == "否" and moment is not None:
        state = load_publish_state(args.publish_state_file)
        state["last_ok_collected_at"] = _beijing_text(moment)
        state["updated_at"] = _beijing_text(now)
        try:
            save_publish_state(args.publish_state_file, state)
        except OSError as exc:
            log.warn(f"publish state write failed reason={type(exc).__name__}")
    return f"apply_{action}"


def main(argv=None, client_factory: Callable[[Any, Logger], Any] | None = None,
         lock_factory: Callable[[str, Logger], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = Logger(args.log_file, verbose=args.verbose)
    mode = "init" if args.init else ("apply" if args.apply else "dry-run")
    started = time.time()
    log.info(f"start mode={mode} table={TABLE_NAME} key={KEY_VALUE} "
             f"log={args.log_file} lock={args.lock_file}")
    result = "failed"
    exit_code = 0
    if client_factory is None:
        client_factory = build_client
    lock = (lock_factory or PublishLock)(args.lock_file, log)
    held = False
    try:
        held = bool(lock.acquire())
        if not held:
            log.warn(f"end mode={mode} result=skipped_lock_busy "
                     f"duration_sec={time.time() - started:.2f}")
            return 0
        result = publish(args, log, client_factory)
    except PublishError as exc:
        log.error(f"publish failed kind={exc.kind} detail={exc.detail}")
        result = f"failed_{exc.kind}"
        if exc.kind == "credentials_missing":
            exit_code = 1
    except Exception:
        log.exception("publish failed unexpectedly")
        result = "failed_unexpected"
    finally:
        if held:
            try:
                lock.release()
            except Exception:
                log.exception("lock release failed")
        log.info(f"end mode={mode} result={result} duration_sec={time.time() - started:.2f}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
