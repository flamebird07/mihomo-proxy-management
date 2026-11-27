"""deploy.verify: read/write access is proven step by step, values never leak."""

import os
import secrets
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import credentials as creds  # noqa: E402
from deploy import feishu_url, registry, verify  # noqa: E402
from deploy_test_support import FakeFeishu, NODES_TABLE_ID  # noqa: E402
from node_provider.feishu import FeishuError  # noqa: E402

TOKEN = "fakeBaseToken1234567890abcd"
OBJ_TOKEN = "fakeObjToken1234567890efgh"
SECRET = "fake-app-secret-" + secrets.token_hex(8)
WRITE_METHODS = ("create_table", "create_field", "update_field", "batch_create",
                 "update_record", "batch_delete")


def fake_token():
    return "fake-app-token-" + secrets.token_hex(8)


def record():
    return registry.DeploymentRecord(hostname="host-a.local", platform="macos",
                                     project_version="1.0.0", deployed_at_ms=1)


class Factory:
    """Stands in for FeishuClient; records every construction."""

    def __init__(self, client):
        self.client = client
        self.built = []

    def __call__(self, app_id, app_secret, app_token, log=None):
        self.built.append((app_id, app_secret, app_token))
        return self.client


def credentials(url=None):
    return creds.Credentials(feishu_url=url or f"https://example.feishu.cn/base/{TOKEN}",
                             app_id="fake-cli-id", app_secret=SECRET)


def run(client, url=None):
    factory = Factory(client)
    logs = []
    parsed = feishu_url.parse_bitable_url(url or f"https://example.feishu.cn/base/{TOKEN}")
    report = verify.verify_access(factory, credentials(url), parsed, record(), log=logs.append)
    return factory, logs, report


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFeishu(rows=[{"fields": {}}, {"fields": {}}, {"fields": {}}])
        self.factory, self.logs, self.report = run(self.client)
        self.token = TOKEN

    def test_everything_proven(self):
        report = self.report
        self.assertTrue(report.read_ok)
        self.assertTrue(report.write_ok)
        self.assertTrue(report.ok)
        self.assertTrue(report.can_source_pool)
        self.assertEqual(report.problems, ())
        self.assertEqual(report.hints(), [])
        self.assertEqual(report.base_name, "fake base")
        self.assertEqual(report.tables, ("节点测速总表", "有效节点"))
        self.assertEqual(report.nodes_rows, 3)
        self.assertEqual(report.registry_action, "created")
        self.assertFalse(report.resolved_from_wiki)

    def test_the_client_gets_exactly_the_parsed_token(self):
        self.assertEqual(self.factory.built, [("fake-cli-id", SECRET, TOKEN)])

    def test_masked_token_is_not_the_token(self):
        masked = self.report.app_token_masked
        self.assertTrue(masked)
        self.assertNotEqual(masked, TOKEN)
        self.assertNotIn(TOKEN, masked)


class WikiAddressTests(unittest.TestCase):
    WIKI = f"https://example.feishu.cn/wiki/fakeWikiToken123456789xyz"

    def test_node_token_is_resolved_to_the_bitable(self):
        client = FakeFeishu(wiki_node={"obj_type": "bitable", "obj_token": OBJ_TOKEN})
        factory, _, report = run(client, self.WIKI)
        self.assertTrue(report.ok)
        self.assertTrue(report.resolved_from_wiki)
        self.assertEqual(factory.built, [("fake-cli-id", SECRET, ""),
                                         ("fake-cli-id", SECRET, OBJ_TOKEN)])
        self.assertIn(("call", "GET", "/wiki/v2/spaces/get_node",
                       {"token": "fakeWikiToken123456789xyz"}), client.calls)

    def test_non_bitable_node_stops_everything(self):
        client = FakeFeishu(wiki_node={"obj_type": "doc", "obj_token": OBJ_TOKEN})
        factory, _, report = run(client, self.WIKI)
        self.assertEqual(report.problems, ("wiki:not_bitable",))
        self.assertFalse(report.read_ok)
        self.assertFalse(report.write_ok)
        self.assertEqual(len(factory.built), 1)
        self.assertEqual(client.called("token"), [])
        self.assertEqual(client.called("list_tables"), [])

    def test_node_without_object_token_is_reported(self):
        _, _, report = run(FakeFeishu(wiki_node={"obj_type": "bitable"}), self.WIKI)
        self.assertEqual(report.problems, ("wiki:no_obj_token",))

    def test_wiki_failure_names_its_step_and_hint(self):
        client = FakeFeishu(raise_on={"call": FeishuError("http_403")})
        _, _, report = run(client, self.WIKI)
        self.assertEqual(report.problems, ("wiki:http_403",))
        self.assertTrue(any("知识库" in hint for hint in report.hints()))


class ReadFailureTests(unittest.TestCase):
    def assert_nothing_after_auth(self, client):
        for method in ("call", "list_tables", "list_records") + WRITE_METHODS:
            self.assertEqual(client.called(method), [], method)

    def test_bad_app_credentials(self):
        client = FakeFeishu(raise_on={"token": FeishuError("token_error")})
        _, _, report = run(client)
        self.assertEqual(report.problems, ("auth:token_error",))
        self.assertFalse(report.read_ok)
        self.assertFalse(report.write_ok)
        self.assertFalse(report.ok)
        self.assertTrue(any("FEISHU_APP_ID" in hint for hint in report.hints()))
        self.assert_nothing_after_auth(client)

    def test_base_not_readable(self):
        client = FakeFeishu(raise_on={"call": FeishuError("http_403")})
        _, _, report = run(client)
        self.assertEqual(report.problems, ("base:http_403",))
        self.assertFalse(report.ok)
        self.assertTrue(any("添加文档应用" in hint for hint in report.hints()))
        self.assertEqual(client.called("list_tables"), [])
        for method in WRITE_METHODS:
            self.assertEqual(client.called(method), [], method)

    def test_table_list_unreadable(self):
        client = FakeFeishu(raise_on={"list_tables": FeishuError("api_error", "code=99991672")})
        _, _, report = run(client)
        self.assertEqual(report.problems, ("tables:api_error(code=99991672)",))
        self.assertFalse(report.read_ok)
        self.assertFalse(report.write_ok)
        for method in WRITE_METHODS:
            self.assertEqual(client.called(method), [], method)


class NodeTableTests(unittest.TestCase):
    def test_fresh_base_without_the_nodes_table_is_still_deployable(self):
        client = FakeFeishu(tables=[])
        _, _, report = run(client)
        self.assertFalse(report.nodes_table_present)
        self.assertFalse(report.can_source_pool)
        self.assertTrue(report.ok)
        self.assertEqual(report.problems, ())
        # Only the registry is read (to find this host's row), never a nodes table.
        self.assertEqual([call for call in client.called("list_records")
                          if call[1] == NODES_TABLE_ID], [])

    def test_empty_nodes_table_cannot_source_a_pool(self):
        _, _, report = run(FakeFeishu(rows=[]))
        self.assertTrue(report.nodes_table_present)
        self.assertFalse(report.can_source_pool)
        self.assertTrue(report.ok)

    def test_unreadable_nodes_table_is_a_soft_failure(self):
        client = FakeFeishu(raise_for_table={NODES_TABLE_ID: FeishuError("timeout")})
        _, _, report = run(client)
        self.assertIn("nodes:timeout", report.problems)
        self.assertTrue(report.read_ok)
        self.assertTrue(report.write_ok)
        self.assertTrue(report.ok)
        self.assertFalse(report.can_source_pool)
        self.assertTrue(any("不影响安装" in hint for hint in report.hints()))


class WriteProbeTests(unittest.TestCase):
    def test_write_denied(self):
        client = FakeFeishu(raise_on={"batch_create": FeishuError("api_error", "code=91403")})
        _, _, report = run(client)
        self.assertEqual(report.problems, ("write:api_error(code=91403)",))
        self.assertTrue(report.read_ok)
        self.assertFalse(report.write_ok)
        self.assertFalse(report.ok)
        self.assertEqual(report.registry_action, "")
        self.assertTrue(any("编辑权限" in hint for hint in report.hints()))

    def test_unusual_detail_text_is_left_out(self):
        for detail in ("code=91403 more words here", "x" * 41, "code=91403\nline"):
            with self.subTest(detail=detail[:20]):
                client = FakeFeishu(raise_on={"batch_create": FeishuError("api_error", detail)})
                _, _, report = run(client)
                self.assertEqual(report.problems, ("write:api_error",))

    def test_non_feishu_exception_contributes_its_class_name_only(self):
        message = f"boom {SECRET} {fake_token()}"
        client = FakeFeishu(raise_on={"batch_create": RuntimeError(message)})
        logs = []
        factory = Factory(client)
        parsed = feishu_url.parse_bitable_url(f"https://example.feishu.cn/base/{TOKEN}")
        report = verify.verify_access(factory, credentials(), parsed, record(), log=logs.append)
        self.assertEqual(report.problems, ("write:RuntimeError",))
        self.assertNotIn("boom", str(report.problems))
        self.assertNotIn("boom", "\n".join(logs))

    def test_repeat_run_updates_the_existing_row(self):
        client = FakeFeishu()
        run(client)
        _, _, report = run(client)
        self.assertEqual(report.registry_action, "updated")
        self.assertTrue(report.ok)


class NoLeakTests(unittest.TestCase):
    def test_token_and_secret_never_reach_logs_or_report(self):
        url = f"https://example.feishu.cn/base/{TOKEN}"
        for client in (FakeFeishu(), FakeFeishu(raise_on={"batch_create": FeishuError("http_403")}),
                       FakeFeishu(raise_on={"token": FeishuError("token_error")})):
            with self.subTest(problems=client.raise_on):
                factory, logs, report = run(client, url)
                for text in list(logs) + [str(report.problems), repr(report), "\n".join(report.hints())]:
                    self.assertNotIn(TOKEN, text)
                    self.assertNotIn(SECRET, text)
                self.assertEqual([item[1] for item in factory.built], [SECRET] * len(factory.built))


if __name__ == "__main__":
    unittest.main()
