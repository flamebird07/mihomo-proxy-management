"""Prove the handed app can read and write the handed Bitable - before anything
is installed.

Read access is checked step by step (app credentials, the base itself, its
table list) and the first failure names the step that failed.  Write access is
proven the only honest way: by performing a real, wanted write - the machine's
row in 「部署记录」.  Problems are recorded as ``step:kind`` so the CLI can
print a targeted hint.  Nothing here lets a credential value escape: the app
token appears masked, exceptions contribute their kind or class name only.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../scripts
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from node_provider.feishu import FeishuClient, FeishuError  # noqa: E402

from . import feishu_url, registry

#: Name only - node_provider must not become a runtime dependency of verify.
NODES_TABLE_NAME = "节点测速总表"
WIKI_NODE_PATH = "/wiki/v2/spaces/get_node"
#: A FeishuError detail (``code=99991672``) is diagnostic; anything longer or
#: stranger is left out so no response text can ride along.
_DETAIL_RE = re.compile(r"^[A-Za-z0-9_=,\-]{1,40}$")

READ_HINT = ("应用无权读取该多维表：在多维表右上角「...」→「添加文档应用」中添加本应用，"
             "并确认已开通 bitable:app 权限")
HINTS = {
    "wiki": ("知识库地址无法解析：确认应用有知识库读权限（wiki:wiki:readonly），"
             "或直接粘贴多维表的 /base/<app_token> 地址"),
    "auth": "检查 FEISHU_APP_ID / FEISHU_APP_SECRET 是否正确、应用是否已发布",
    "base": READ_HINT,
    "tables": READ_HINT,
    "write": ("应用没有编辑权限：把本应用添加为该多维表的可编辑者，"
              "并确认开通的是 bitable:app（读写）而不是只读权限"),
    "nodes": "「节点测速总表」读取失败：不影响安装，但本机暂时不能从飞书取节点",
}


@dataclass
class AccessReport:
    """What the checks found.  ``problems`` holds ``step:kind`` strings."""

    read_ok: bool = False
    write_ok: bool = False
    base_name: str = ""
    app_token_masked: str = ""
    app_token: str = field(repr=False, default="")
    resolved_from_wiki: bool = False
    tables: tuple = ()
    nodes_table_present: bool = False
    nodes_rows: int = 0
    registry_action: str = ""
    problems: tuple = ()

    @property
    def ok(self) -> bool:
        """Both halves proven: the table can be read and it can be written."""
        return self.read_ok and self.write_ok

    @property
    def can_source_pool(self) -> bool:
        """「节点测速总表」 exists and has at least one row."""
        return self.nodes_table_present and self.nodes_rows > 0

    def hints(self) -> list:
        """One fixed hint per failing step, in failure order."""
        shown = []
        for problem in self.problems:
            hint = HINTS.get(problem.split(":", 1)[0])
            if hint and hint not in shown:
                shown.append(hint)
        return shown


def _problem(step: str, exc: BaseException) -> str:
    if isinstance(exc, FeishuError):
        detail = exc.detail or ""
        if detail and _DETAIL_RE.fullmatch(detail):
            return f"{step}:{exc.kind}({detail})"
        return f"{step}:{exc.kind}"
    return f"{step}:{type(exc).__name__}"


def verify_access(client_factory, credentials, parsed, record, *, log=None,
                  write_probe: bool = True) -> AccessReport:
    """Run the read checks, then the write probe.  Feishu errors never raise;
    the report names the step that failed and everything after it was skipped.

    ``write_probe=False`` skips the write step entirely (dry-run): nothing is
    written, ``registry_action`` becomes ``"skipped"`` and ``write_ok`` stays
    ``False`` - which in a dry-run means "write access was not verified", not
    "verified missing".  A dry-run caller must therefore judge success by
    ``read_ok`` alone.
    """
    say = log or (lambda _message: None)
    report = AccessReport()
    app_token = parsed.token

    def fail(step: str, exc: BaseException) -> AccessReport:
        problem = _problem(step, exc)
        report.problems += (problem,)
        say(f"feishu access step={step} problem={problem}")
        return report

    if parsed.kind == "wiki":
        try:
            # The node lookup does not need the app_token: probe with a blank one.
            probe = client_factory(credentials.app_id, credentials.app_secret, "")
            node = (probe.call("GET", WIKI_NODE_PATH, params={"token": parsed.token})
                    or {}).get("node") or {}
            if node.get("obj_type") != "bitable":
                raise FeishuError("not_bitable")
            app_token = str(node.get("obj_token") or "")
            if not app_token:
                raise FeishuError("no_obj_token")
            report.resolved_from_wiki = True
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            return fail("wiki", exc)
    report.app_token_masked = feishu_url.mask_token(app_token)
    report.app_token = app_token

    try:
        client = client_factory(credentials.app_id, credentials.app_secret, app_token)
        client.token()
    except Exception as exc:  # noqa: BLE001
        return fail("auth", exc)
    try:
        data = client.call("GET", f"/bitable/v1/apps/{app_token}")
        report.base_name = str(((data or {}).get("app") or {}).get("name") or "")
    except Exception as exc:  # noqa: BLE001
        return fail("base", exc)
    try:
        tables = client.list_tables()
    except Exception as exc:  # noqa: BLE001
        return fail("tables", exc)
    report.tables = tuple(str(table.get("name")) for table in tables
                          if isinstance(table, dict) and table.get("name"))
    report.read_ok = True
    say(f"feishu base readable tables={len(report.tables)} token={report.app_token_masked}")

    nodes = [table for table in tables if isinstance(table, dict)
             and table.get("name") == NODES_TABLE_NAME and table.get("table_id")]
    if nodes:
        report.nodes_table_present = True
        try:
            report.nodes_rows = len(client.list_records(nodes[0]["table_id"]))
        except Exception as exc:  # noqa: BLE001 - soft: a new or busy table must not block
            fail("nodes", exc)

    if not write_probe:
        report.registry_action = "skipped"
        say("feishu write probe skipped (dry-run) write_ok stays False")
        return report

    try:
        report.registry_action = registry.upsert_registry(client, record, log=say)
        report.write_ok = True
    except Exception as exc:  # noqa: BLE001
        fail("write", exc)
    say(f"feishu write probe ok action={report.registry_action} nodes_table={report.nodes_table_present} "
        f"nodes_rows={report.nodes_rows}")
    return report
