"""deploy.bootstrap: the whole flow runs offline with fakes and never leaks."""

import hashlib
import json
import os
import re
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


class SecretTests(TempRootTestCase):
    """The controller SECRET: generated once, kept on re-runs, never leaked."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.dirname(self.platform.secret_path), exist_ok=True)

    def write_secret_file(self, text):
        with open(self.platform.secret_path, "w", encoding="utf-8") as stream:
            stream.write(text)

    def read_secret_file(self):
        with open(self.platform.secret_path, "r", encoding="utf-8") as stream:
            return stream.read()

    def generated_secret(self):
        match = re.search(r"^SECRET=(\S+)$", self.read_secret_file(), re.MULTILINE)
        self.assertIsNotNone(match, "the secret file must carry a SECRET line")
        return match.group(1)

    def test_first_run_generates_a_controller_secret(self):
        self.write_secret_file("# hand-added\nEXISTING_KEY=keep-me\n")
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        generated = self.generated_secret()
        self.assertGreaterEqual(len(generated), 32)
        self.assertIn("SECRET", result.secret_keys)

    def test_rerun_keeps_an_existing_secret(self):
        self.write_secret_file("SECRET=keep-me-bearer-value\n")
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertIn("SECRET=keep-me-bearer-value", self.read_secret_file())

    def test_blank_secret_is_regenerated(self):
        self.write_secret_file("SECRET=\n")
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertNotEqual(self.generated_secret(), "")

    def test_dry_run_writes_no_secret_file_and_still_counts_pool_nodes(self):
        result, _, _ = run_flow(self.platform, lock=self.lock, dry_run=True)
        self.assertTrue(result.ok())
        self.assertFalse(result.secret_written)
        self.assertFalse(os.path.exists(self.platform.secret_path))
        self.assertFalse(os.path.exists(self.platform.pool_path))
        # the dry run reached the node count without generating any file
        self.assertEqual(result.pool_nodes, 1)

    def test_dry_run_never_draws_from_the_rng_and_plans_the_secret_key(self):
        with mock.patch.object(bootstrap.secrets_mod, "token_urlsafe",
                               return_value="planned-not-used") as rng:
            result, _, _ = run_flow(self.platform, lock=self.lock, dry_run=True)
        rng.assert_not_called()
        self.assertTrue(result.ok())
        self.assertIn("SECRET", result.secret_keys)  # planned, value not drawn

    def test_real_run_draws_the_rng_once_when_no_secret_exists(self):
        with mock.patch.object(bootstrap.secrets_mod, "token_urlsafe",
                               return_value="drawn-bearer-token") as rng:
            result, _, _ = run_flow(self.platform, lock=self.lock)
        rng.assert_called_once()
        self.assertTrue(result.ok())
        self.assertIn("SECRET=drawn-bearer-token", self.read_secret_file())

    def test_real_run_keeps_the_file_secret_without_drawing(self):
        self.write_secret_file("SECRET=keep-me-bearer-value\n")
        with mock.patch.object(bootstrap.secrets_mod, "token_urlsafe") as rng:
            result, _, _ = run_flow(self.platform, lock=self.lock)
        rng.assert_not_called()
        self.assertTrue(result.ok())

    def test_generated_secret_never_reaches_logs_repr_or_summary(self):
        result, _, logs = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        generated = self.generated_secret()
        joined = "\n".join(logs)
        for blob in (joined, repr(result), json.dumps(result.summary())):
            self.assertNotIn(generated, blob)


class PoolDecisionTests(TempRootTestCase):
    """pool_required must match the final install mode (main URL decides)."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.dirname(self.platform.secret_path), exist_ok=True)

    def write_secret_file(self, text):
        with open(self.platform.secret_path, "w", encoding="utf-8") as stream:
            stream.write(text)

    def read_secret_file(self):
        with open(self.platform.secret_path, "r", encoding="utf-8") as stream:
            return stream.read()

    def test_existing_main_url_on_rerun_skips_the_pool(self):
        url = "https://sub.real-host.test/one?token=abc"
        self.write_secret_file(f"SUBSCRIPTION_URL={url}\nSECRET=keep\n")
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertFalse(result.pool_required)
        self.assertFalse(result.pool_written)
        self.assertFalse(os.path.exists(self.platform.pool_path))
        self.assertIn(f"SUBSCRIPTION_URL={url}", self.read_secret_file())

    def test_empty_main_url_still_builds_the_pool(self):
        self.write_secret_file("SUBSCRIPTION_URL=\nSECRET=keep\n")
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertTrue(result.pool_required)
        self.assertTrue(result.pool_written)
        self.assertTrue(os.path.exists(self.platform.pool_path))

    def test_sample_main_url_is_not_a_configuration(self):
        self.write_secret_file('SUBSCRIPTION_URL="https://example.com/your-sub"\n')
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertTrue(result.pool_required)
        self.assertTrue(result.pool_written)

    def test_env_only_aux_subscription_still_builds_the_pool(self):
        url = "https://sub.real-host.test/two?token=xyz"
        result, _, _ = run_flow(self.platform, lock=self.lock,
                                environ={"MPM_SUBSCRIPTION_URL_2": url})
        self.assertTrue(result.ok())
        self.assertTrue(result.pool_required)
        self.assertTrue(result.pool_written)
        self.assertIn(f"SUBSCRIPTION_URL_2={url}", self.read_secret_file())

    def test_env_main_url_overrides_the_file(self):
        self.write_secret_file("SUBSCRIPTION_URL=https://old.host.test/one\n")
        url = "https://sub.real-host.test/new?token=abc"
        result, _, _ = run_flow(self.platform, lock=self.lock,
                                environ={"MPM_SUBSCRIPTION_URL": url})
        self.assertTrue(result.ok())
        self.assertFalse(result.pool_required)
        self.assertIn(f"SUBSCRIPTION_URL={url}", self.read_secret_file())


class MainUrlSyncTests(TempRootTestCase):
    """The main URL written to the file must match the derived install mode:
    a placeholder must be cleared, a real value must survive."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.dirname(self.platform.secret_path), exist_ok=True)

    def write_secret_file(self, text):
        with open(self.platform.secret_path, "w", encoding="utf-8") as stream:
            stream.write(text)

    def read_secret_file(self):
        with open(self.platform.secret_path, "r", encoding="utf-8") as stream:
            return stream.read()

    def assert_main_url_cleared(self):
        text = self.read_secret_file()
        self.assertIsNone(re.search(r"(?m)^SUBSCRIPTION_URL=\S", text),
                          f"main URL must be cleared, got: {text!r}")

    def test_placeholder_env_url_ends_up_cleared_in_the_file(self):
        placeholder = "https://provider.example.invalid/link?clash=1"
        result, _, _ = run_flow(self.platform, lock=self.lock,
                                environ={"MPM_SUBSCRIPTION_URL": placeholder})
        self.assertTrue(result.ok())
        self.assertTrue(result.pool_required)
        self.assertTrue(result.pool_written)
        self.assert_main_url_cleared()
        self.assertNotIn(placeholder, self.read_secret_file())

    def test_placeholder_file_url_is_cleared_without_env_override(self):
        self.write_secret_file("SUBSCRIPTION_URL=https://provider.example.invalid/link\n")
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertTrue(result.pool_required)
        self.assertTrue(result.pool_written)
        self.assert_main_url_cleared()

    def test_placeholder_env_url_does_not_overwrite_a_real_file_url(self):
        real = "https://sub.real-host.test/one?token=abc"
        self.write_secret_file(f"SUBSCRIPTION_URL={real}\n")
        result, _, _ = run_flow(self.platform, lock=self.lock,
                                environ={"MPM_SUBSCRIPTION_URL": "https://example.com/your-sub"})
        self.assertTrue(result.ok())
        self.assertFalse(result.pool_required)
        self.assertIn(f"SUBSCRIPTION_URL={real}", self.read_secret_file())

    def test_valid_env_url_wins_over_a_placeholder_file_url(self):
        url = "https://sub.real-host.test/new?token=abc"
        self.write_secret_file("SUBSCRIPTION_URL=https://example.com/your-sub\n")
        result, _, _ = run_flow(self.platform, lock=self.lock,
                                environ={"MPM_SUBSCRIPTION_URL": url})
        self.assertTrue(result.ok())
        self.assertFalse(result.pool_required)
        text = self.read_secret_file()
        self.assertIn(f"SUBSCRIPTION_URL={url}", text)
        self.assertNotIn("your-sub", text)

    def test_real_aux_and_unrelated_keys_survive_the_main_url_clear(self):
        aux = "https://sub.real-host.test/two?token=xyz"
        result, _, _ = run_flow(
            self.platform, lock=self.lock,
            environ={"MPM_SUBSCRIPTION_URL": "https://example.com/your-sub",
                     "MPM_SUBSCRIPTION_URL_2": aux})
        self.assertTrue(result.ok())
        text = self.read_secret_file()
        self.assert_main_url_cleared()
        self.assertIn(f"SUBSCRIPTION_URL_2={aux}", text)
        self.assertIn("FEISHU_BITABLE_APP_TOKEN=", text)


class ExistingFileEncodingTests(TempRootTestCase):
    """Windows-edited env files carry a BOM; reading must not corrupt keys."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.dirname(self.platform.secret_path), exist_ok=True)

    def test_bom_prefixed_file_keeps_its_keys_and_main_url(self):
        url = "https://sub.real-host.test/one?token=abc"
        with open(self.platform.secret_path, "wb") as stream:
            stream.write(
                "\ufeff".encode("utf-8")
                + f"SUBSCRIPTION_URL={url}\r\nSECRET=keep\r\nNOTE=keep-me\r\n".encode("utf-8"))
        result, _, _ = run_flow(self.platform, lock=self.lock)
        self.assertTrue(result.ok())
        self.assertFalse(result.pool_required)  # the BOM hid nothing
        with open(self.platform.secret_path, "r", encoding="utf-8") as stream:
            text = stream.read()
        self.assertIn(f"SUBSCRIPTION_URL={url}", text)
        self.assertIn("NOTE=keep-me", text)
        self.assertIn("SECRET=keep", text)


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
