"""Minimal Feishu Bitable client (self-contained copy/rewrite).

Security contract:
  * app_secret and tenant_access_token are never logged, printed or stored.
  * transport failures are reduced to a fixed class / numeric HTTP status.
  * table ids may be logged; credentials may not.

Only the operations needed by node_ops are implemented.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from . import schema

RETRY_CODES = {1254290, 1254291, 1254607, 99991400, 1310213}
TOKEN_CODES = {99991661, 99991663, 99991664, 99991668}

BASE = "https://open.feishu.cn/open-apis"


class FeishuError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}{':' + detail if detail else ''}")
        self.kind = kind
        self.detail = detail


def _safe_log(log: Optional[Callable[[str], None]], message: str) -> None:
    if log is not None:
        log(message)


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, app_token: str,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self._log = log
        self._token: Optional[str] = None
        self._expires_at = 0.0

    # -- transport ---------------------------------------------------------

    def _raw(self, method: str, url: str, body: Optional[dict],
             headers: Dict[str, str], timeout: int = 30) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise FeishuError(f"http_{exc.code}") from None
        except (socket.timeout, TimeoutError):
            raise FeishuError("timeout") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise FeishuError("timeout") from None
            raise FeishuError("network_error") from None
        except Exception as exc:  # pragma: no cover
            raise FeishuError(type(exc).__name__.lower()) from None
        try:
            return json.loads(raw or "{}")
        except ValueError:
            raise FeishuError("bad_response") from None

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 120:
            return self._token
        payload = self._raw(
            "POST",
            f"{BASE}/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
            {"Content-Type": "application/json; charset=utf-8"},
            timeout=20,
        )
        if payload.get("code") != 0 or not payload.get("tenant_access_token"):
            raise FeishuError("token_error")
        self._token = payload["tenant_access_token"]
        self._expires_at = time.time() + int(payload.get("expire") or 7200)
        _safe_log(self._log, "feishu token acquired")
        return self._token

    def call(self, method: str, path: str, body: Optional[dict] = None,
             params: Optional[dict] = None) -> dict:
        url = BASE + path
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
                raise FeishuError("token_error")
            if code in RETRY_CODES:
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FeishuError("rate_limited")
            raise FeishuError("api_error", f"code={code}")
        raise FeishuError("api_error")

    # -- tables ------------------------------------------------------------

    def list_tables(self) -> List[dict]:
        tables: List[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.call("GET", f"/bitable/v1/apps/{self.app_token}/tables",
                             params=params)
            tables.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return tables

    def create_table(self, name: str, fields: list[tuple]) -> str:
        body = {"table": {"name": name, "default_view_name": "表格", "fields": [
            {"field_name": f[0], "type": f[1], **({"property": f[2]} if f[2] else {})}
            for f in fields
        ]}}
        data = self.call("POST", f"/bitable/v1/apps/{self.app_token}/tables", body=body)
        table_id = data.get("table_id") or ""
        if not table_id:
            raise FeishuError("api_error", "create_table_no_id")
        return table_id

    def list_fields(self, table_id: str) -> List[dict]:
        fields: List[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.call(
                "GET",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
                params=params,
            )
            fields.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return fields

    def create_field(self, table_id: str, name: str, ftype: int,
                     prop: Optional[dict]) -> None:
        body: Dict[str, Any] = {"field_name": name, "type": ftype}
        if prop:
            body["property"] = prop
        self.call("POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
                  body=body)

    def update_field(self, table_id: str, field_id: str, name: str, ftype: int,
                     prop: dict) -> None:
        body = {"field_name": name, "type": ftype, "property": prop}
        self.call(
            "PUT",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields/{field_id}",
            body=body,
        )

    # -- records -----------------------------------------------------------

    def list_records(self, table_id: str) -> List[dict]:
        records: List[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.call(
                "GET",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
                params=params,
            )
            records.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return records

    def batch_delete(self, table_id: str, record_ids: List[str]) -> int:
        deleted = 0
        for start in range(0, len(record_ids), 200):
            chunk = record_ids[start:start + 200]
            self.call(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_delete",
                body={"records": chunk},
            )
            deleted += len(chunk)
        return deleted

    def batch_create(self, table_id: str, records: List[dict]) -> int:
        created = 0
        for start in range(0, len(records), 100):
            chunk = records[start:start + 100]
            self.call(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_create",
                body={"records": chunk},
            )
            created += len(chunk)
        return created


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def ensure_fields(client: FeishuClient, table_id: str, fields: list[tuple],
                  log: Callable[[str], None]) -> None:
    existing = {f.get("field_name"): f for f in client.list_fields(table_id)}
    for name, ftype, prop in fields:
        current = existing.get(name)
        if current is not None and int(current.get("type") or 0) != int(ftype):
            # Type changes are not silently coerced; report and create a fresh
            # field only if the platform allows it.
            log(f"field type mismatch name={name} existing={current.get('type')} wanted={ftype}")
            continue
        if current is None:
            try:
                client.create_field(table_id, name, ftype, prop)
                log(f"field created name={name} type={ftype}")
            except FeishuError as exc:
                log(f"field create failed name={name} class={exc.kind}")
            continue
        if ftype != schema.FT_SINGLE_SELECT or not prop:
            continue
        wanted = [option["name"] for option in prop.get("options", [])]
        have = [option.get("name") for option in
                ((current.get("property") or {}).get("options") or [])]
        missing = [item for item in wanted if item not in have]
        if not missing:
            continue
        merged = {"options": [{"name": item} for item in have + missing]}
        try:
            client.update_field(table_id, current.get("field_id", ""), name, ftype, merged)
            log(f"field options extended name={name} added={len(missing)}")
        except FeishuError as exc:
            log(f"field update failed name={name} class={exc.kind}")


def ensure_tables(client: FeishuClient,
                  log: Callable[[str], None]) -> Dict[str, Dict[str, str]]:
    """Idempotently create both tables and their fields. Returns key -> info."""
    remote = {table.get("name"): table.get("table_id") for table in client.list_tables()}
    tables: Dict[str, Dict[str, str]] = {}
    for spec in schema.TABLE_SPECS:
        table_id = remote.get(spec["name"])
        if table_id:
            log(f"table exists name={spec['name']}")
        else:
            table_id = client.create_table(spec["name"], spec["fields"])
            log(f"table created name={spec['name']}")
        ensure_fields(client, table_id, spec["fields"], log)
        tables[spec["key"]] = {"table_id": table_id, "name": spec["name"]}
    return tables


def replace_all(client: FeishuClient, table_id: str, records: List[dict],
                log: Callable[[str], None], *, dry_run: bool = False,
                allow_empty: bool = True) -> int:
    """Full replacement: create new rows, then delete the old ones.

    A failed/zero creation never deletes old rows.  With ``allow_empty=False``
    an empty payload aborts the replacement (protects the node table from a
    total-collection failure wiping it).
    """
    existing = client.list_records(table_id)
    old_ids = [row.get("record_id") for row in existing if row.get("record_id")]
    if dry_run:
        log(f"records dry-run create={len(records)} delete={len(old_ids)}")
        return 0
    if not records and not allow_empty:
        log(f"records replace refused empty_payload keep_old={len(old_ids)}")
        return 0
    if not records:
        if old_ids:
            deleted = client.batch_delete(table_id, old_ids)
            log(f"records deleted={deleted}")
        else:
            log("records created=0")
        return 0
    created = client.batch_create(table_id, records)
    log(f"records created={created}")
    if created != len(records):
        log("records delete skipped reason=create_incomplete")
        return created
    if old_ids:
        deleted = client.batch_delete(table_id, old_ids)
        log(f"records deleted={deleted}")
    return created
