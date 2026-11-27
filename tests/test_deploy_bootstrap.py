"""deploy.bootstrap: the whole flow runs offline with fakes and never leaks."""

import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import bootstrap, supply  # noqa: E402
from deploy import credentials as creds  # noqa: E402
from deploy_test_support import FakeFeishu, NODES_TABLE_ID  # noqa: E402
from node_provider.feishu import FeishuError  # noqa: E402

TOKEN = "fakeBaseToken1234567890abcd"
SECRET = "fake-app-secret-" + secrets.token_hex(8)
APP_ID = "fake-cli-id"
NOW_MS = 1_700_000_000_000
ASSET_URL = "https://github.com/example/mihomo/releases/download/v1/mihomo-bin"
PAYLOAD = b"asset-mihomo-bin"


def make_lock():
    asset = supply.Asset(key="mihomo", os="macos", arch="arm64", url=ASSET_URL,
                         digest=hashlib.sha256(PAYLOAD).hexdigest(),
                         size=len(PAYLOAD), fmt="raw", install_as="mihomo",
                         mode="0644")
    return {"schema_version": 1, "assets": [asset]}


def write_lock_file(directory, lock):
    path = os.path.join(directory, "supply.lock.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "assets": []}, stream)
    return path


def fake_fetch(url, destination):
    name = url.rstrip("/").rsplit("/", 1)[-1]
    with open(destination, "wb") as stream:
        stream.write(b"asset-" + name.encode("ascii"))


def broken_fetch(url, destination):
    raise RuntimeError("boom while fetching")


class FakePlatform(bootstrap.Platform):
    def __init__(self, root, *, exit_code=0, error=None, nested=False):
        base = root if not nested else os.path.join(root, "deep", "nested")
        self.name = "macos"
        self.supply_os = "macos"
        self.arch = "arm64"
        self.pool_path = os.path.join(base, "providers", "feishu-pool.yaml")
        self.secret_path = os.path.join(base, "state", "subscription.env")
        self.staging_dir = os.path.join(base, "staging")
        self.exit_code = exit_code
        self.error = error
        self.requests = []

    def install(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.exit_code


class Factory:
    def __init__(self, client):
        self.client = client
        self.built = []

    def __call__(self, app_id, app_secret, app_token, log=None):
        self.built.append((app_id, app_secret, app_token))
        return self.client


def node_row(name, server):
    config = {"name": name, "type": "trojan", "server": server, "port": 443,
              "password": "fake-node-password"}
    return {"fields": {"节点名": name,
                       "节点配置JSON": json.dumps(config, ensure_ascii=False),
                       "地区组": "美国", "GPT可用": True,
                       "同步时间": NOW_MS - 60_000}}


def credentials(url=f"https://example.feishu.cn/base/{TOKEN}"):
    return creds.Credentials(feishu_url=url, app_id=APP_ID, app_secret=SECRET)


def run_flow(platform, client=None, *, environ=None, lock=None, fetch=fake_fetch,
             factory=None, creds_url=None, **kwargs):
    client = client if client is not None else FakeFeishu(rows=[node_row("节点甲", "192.0.2.1")])
    factory = factory or Factory(client)
    logs = []
    kwargs.setdefault("hostname", "host-a.local")
    creds_obj = credentials() if creds_url is None else credentials(creds_url)
    result = bootstrap.run_bootstrap(creds_obj, platform, lock=lock,
                                     environ=environ, client_factory=factory,
                                     project_version="1.0.0", fetch=fetch,
                                     fetch_release=lambda _url: None,
                                     now_ms=NOW_MS, log=logs.append, **kwargs)
    return result, factory, logs


class TempRootTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bootstrap-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.platform = FakePlatform(self.root)
        self.lock = make_lock()


class HappyPathTests(TempRootTestCase):
    def setUp(self):
        super().setUp()
        existing = "# hand-added\nEXISTING_KEY=keep-me\n"
        os.makedirs(os.path.dirname(self.platform.secret_path), exist_ok=True)
        with open(self.platform.secret_path, "w", encoding="utf-8") as stream:
            stream.write(existing)
        self.existing = existing

    def test_full_flow_with_the_pool_from_feishu(self):
        client = FakeFeishu(rows=[node_row("节点甲", "192.0.2.1"),
                                  node_row("节点乙", "192.0.2.2")])
        result, factory, logs = run_flow(self.platform, client, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertEqual(result.exit_code, bootstrap.EXIT_OK)
        self.assertTrue(result.pool_required)
        self.assertTrue(result.pool_written)
        self.assertEqual(result.pool_nodes, 2)
        self.assertEqual(sorted(result.staged), ["mihomo"])
        self.assertTrue(result.secret_written)
        self.assertEqual(result.install_exit, 0)
        self.assertTrue(result.ok())

        with open(self.platform.pool_path, "r", encoding="utf-8") as stream:
            proxies = yaml.safe_load(stream)["proxies"]
        self.assertEqual(len(proxies), 2)

        with open(self.platform.secret_path, "r", encoding="utf-8") as stream:
            text = stream.read()
        self.assertIn("# hand-added", text)          # comments survive
        self.assertIn("EXISTING_KEY=keep-me", text)  # unrelated keys survive
        self.assertIn(f"FEISHU_APP_ID={APP_ID}", text)
        self.assertIn(f"FEISHU_APP_SECRET={SECRET}", text)
        self.assertIn(f"FEISHU_BITABLE_APP_TOKEN={TOKEN}", text)

        self.assertEqual(len(self.platform.requests), 1)
        request = self.platform.requests[0]
        self.assertEqual(request.staging_dir, self.platform.staging_dir)
        self.assertEqual(request.pool_path, self.platform.pool_path)
        self.assertTrue(request.start)
        with open(os.path.join(self.platform.staging_dir, "mihomo"), "rb") as stream:
            self.assertEqual(stream.read(), PAYLOAD)

        # The daemon's client is built with the resolved token.
        self.assertEqual(factory.built[-1], (APP_ID, SECRET, TOKEN))

    def test_summary_is_json_safe_and_secret_free(self):
        result, _, _ = run_flow(self.platform, lock=self.lock)
        text = json.dumps(result.summary())
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(result.summary()["exit_code"], bootstrap.EXIT_OK)
        self.assertNotIn(TOKEN, repr(result))
        self.assertNotIn(SECRET, repr(result))


class SubscriptionModeTests(TempRootTestCase):
    def test_subscription_url_skips_the_pool(self):
        url = "https://sub.example.invalid/one?token=abc&flag=1"
        result, _, _ = run_flow(self.platform, lock=self.lock,
                                environ={"MPM_SUBSCRIPTION_URL": url})
        self.assertFalse(result.pool_required)
        self.assertFalse(result.pool_written)
        self.assertFalse(os.path.exists(self.platform.pool_path))
        with open(self.platform.secret_path, "r", encoding="utf-8") as stream:
            self.assertIn(f"SUBSCRIPTION_URL={url}", stream.read())

    def test_third_subscription_uses_its_own_key(self):
        url = "https://sub.example.invalid/three?token=xyz"
        result, _, _ = run_flow(self.platform, lock=self.lock,
                                environ={"MPM_SUBSCRIPTION_URL_3": url})
        with open(self.platform.secret_path, "r", encoding="utf-8") as stream:
            text = stream.read()
        self.assertIn(f"SUBSCRIPTION_URL_3={url}", text)
        self.assertNotIn("SUBSCRIPTION_URL=", text.replace("SUBSCRIPTION_URL_3=", ""))


class NodesFailOnSecondRead(FakeFeishu):
    """verify's soft read of the nodes table succeeds; the pool step's read fails."""

    def list_records(self, table_id):
        if table_id == NODES_TABLE_ID and self.called("list_records"):
            raise FeishuError("http_500")
        return super().list_records(table_id)


class FailureTests(TempRootTestCase):
    def test_invalid_address_stops_before_any_client(self):
        factory = Factory(FakeFeishu())
        result, _, _ = run_flow(self.platform, factory=factory, lock=self.lock,
                                environ={}, creds_url="https://example.com/base/whatever123")
        self.assertEqual(result.exit_code, bootstrap.EXIT_USAGE)
        self.assertEqual(result.problems, ("url:not_feishu_host",))
        self.assertEqual(factory.built, [])
        self.assertIsNone(result.install_exit)

    def run_with(self, client, **kwargs):
        result, _, _ = run_flow(self.platform, client, lock=self.lock, **kwargs)
        return result

    def test_read_failure_stops_everything(self):
        client = FakeFeishu(raise_on={"token": FeishuError("token_error")})
        result = self.run_with(client)
        self.assertEqual(result.exit_code, bootstrap.EXIT_ACCESS)
        self.assertTrue(result.problems[0].startswith("access:"))
        self.assertEqual(os.listdir(self.root), [])  # no download, no file, no install
        self.assertEqual(self.platform.requests, [])

    def test_write_failure_is_fatal_unless_dry_run(self):
        client = FakeFeishu(rows=[node_row("节点甲", "192.0.2.1")],
                            raise_on={"batch_create": FeishuError("api_error", "code=91403")})
        result = self.run_with(client)
        self.assertEqual(result.exit_code, bootstrap.EXIT_ACCESS)
        self.assertTrue(result.problems[0].startswith("access:write:"))
        self.assertEqual(os.listdir(self.root), [])

        dry = self.run_with(client, dry_run=True)
        self.assertTrue(dry.ok())
        self.assertEqual(dry.access.registry_action, "skipped")
        self.assertFalse(dry.secret_written)
        self.assertFalse(dry.pool_written)
        self.assertEqual(os.listdir(self.root), [])

    def test_supply_failure_stops_before_secrets_and_install(self):
        result, _, _ = run_flow(self.platform, lock=self.lock, fetch=broken_fetch)
        self.assertEqual(result.exit_code, bootstrap.EXIT_SUPPLY)
        self.assertTrue(result.problems[0].startswith("supply:"))
        self.assertFalse(result.secret_written)
        self.assertFalse(os.path.exists(self.platform.secret_path))
        self.assertEqual(self.platform.requests, [])

    def test_skip_supply_leaves_staging_empty(self):
        result, _, _ = run_flow(self.platform, lock=self.lock, skip_supply=True)
        self.assertTrue(result.ok())
        self.assertEqual(result.staged, {})

    def test_pool_failures(self):
        result = self.run_with(FakeFeishu(tables=[]))
        self.assertEqual(result.exit_code, bootstrap.EXIT_POOL)
        self.assertEqual(result.problems, ("pool:no_nodes_in_table",))

        result = self.run_with(FakeFeishu(rows=[{"fields": {}}]))
        self.assertEqual(result.exit_code, bootstrap.EXIT_POOL)
        self.assertEqual(result.problems, ("pool:below_min_nodes",))

        result = self.run_with(NodesFailOnSecondRead(rows=[node_row("节点甲", "192.0.2.1")]))
        self.assertEqual(result.exit_code, bootstrap.EXIT_POOL)
        self.assertEqual(result.problems[0], "pool:http_500")

    def test_secret_write_failure_stops_the_install(self):
        blocker = os.path.join(self.root, "state")
        with open(blocker, "w", encoding="utf-8") as stream:
            stream.write("not a directory")
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertEqual(result.exit_code, bootstrap.EXIT_INSTALL)
        self.assertTrue(result.problems[0].startswith("secrets:"))
        self.assertEqual(self.platform.requests, [])


class DryRunTests(TempRootTestCase):
    def test_dry_run_writes_nothing_and_creates_no_directory(self):
        lock_path = write_lock_file(self.root, None)
        client = FakeFeishu(rows=[node_row("节点甲", "192.0.2.1")])
        result, _, _ = run_flow(self.platform, client, lock=self.lock, dry_run=True)
        self.assertEqual(result.exit_code, bootstrap.EXIT_OK)
        self.assertTrue(result.ok())
        self.assertFalse(result.pool_written)
        self.assertFalse(result.secret_written)
        self.assertIsNone(result.install_exit)
        self.assertEqual(result.staged, {})
        self.assertEqual(os.listdir(self.root), ["supply.lock.json"])
        self.assertTrue(os.path.exists(lock_path))


class InstallTests(TempRootTestCase):
    def test_nonzero_install_exit_is_reported(self):
        platform = FakePlatform(self.root, exit_code=3)
        result, _, _ = run_flow(platform, lock=self.lock)
        self.assertEqual(result.exit_code, bootstrap.EXIT_INSTALL)
        self.assertEqual(result.install_exit, 3)
        self.assertEqual(result.problems, ("install:exit_3",))

    def test_install_exception_names_the_class(self):
        platform = FakePlatform(self.root, error=OSError("installer missing"))
        result, _, _ = run_flow(platform, lock=self.lock)
        self.assertEqual(result.exit_code, bootstrap.EXIT_INSTALL)
        self.assertEqual(result.problems, ("install:OSError",))

    def test_skip_install_succeeds_without_installing(self):
        platform = FakePlatform(self.root)
        result, _, _ = run_flow(platform, lock=self.lock, skip_install=True)
        self.assertTrue(result.ok())
        self.assertIsNone(result.install_exit)
        self.assertEqual(platform.requests, [])

    def test_start_false_is_passed_through(self):
        platform = FakePlatform(self.root)
        result, _, _ = run_flow(platform, lock=self.lock, start=False)
        self.assertTrue(result.ok())
        self.assertFalse(platform.requests[0].start)


class ParentDirectoryTests(TempRootTestCase):
    def test_missing_parents_are_created(self):
        platform = FakePlatform(self.root, nested=True)
        result, _, _ = run_flow(platform, lock=self.lock)
        self.assertTrue(result.ok())
        for path in (platform.pool_path, platform.secret_path):
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(os.listdir(platform.staging_dir), ["mihomo"])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "deep", "nested", "providers")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "deep", "nested", "state")))

    def test_dry_run_creates_no_directory_on_the_same_paths(self):
        platform = FakePlatform(self.root, nested=True)
        result, _, _ = run_flow(platform, lock=self.lock, dry_run=True)
        self.assertTrue(result.ok())
        self.assertEqual(os.listdir(self.root), [])


class NoLeakTests(TempRootTestCase):
    def test_no_secret_reaches_logs_repr_or_summary(self):
        url = "https://sub.example.invalid/one?token=abc&flag=1"
        cases = [
            dict(lock=self.lock),
            dict(lock=self.lock, environ={"MPM_SUBSCRIPTION_URL": url}),
            dict(lock=self.lock, fetch=broken_fetch),
            dict(lock=self.lock, dry_run=True),
        ]
        for index, kwargs in enumerate(cases):
            with self.subTest(case=index):
                platform = FakePlatform(self.root)
                result, _, logs = run_flow(platform, **kwargs)
                joined = "\n".join(logs)
                for text in [joined, repr(result), json.dumps(result.summary())]:
                    self.assertNotIn(TOKEN, text)
                    self.assertNotIn(SECRET, text)
                    if "environ" in kwargs:
                        self.assertNotIn(url, text)


if __name__ == "__main__":
    unittest.main()
