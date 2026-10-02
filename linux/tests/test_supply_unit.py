"""Section 11 test categories 6 and 10: supply-chain verification (all negatives),
authenticated controller client, and systemd unit content.

No download, no execution, no network: fetch/probe/transport are injected.
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
import urllib.parse

import support
from mpm import controller as controller_mod
from mpm import supply, unit as unit_mod
from mpm.errors import FailClosed, NotReady
from mpm.preflight import evaluate

REAL_LOCK = os.path.join(support.LINUX_DIR, "supply", "mihomo.lock.json")

#: reference sentinel from the review: credentials in the userinfo *and* in the
#: query string, and the password itself is already percent-encoded
SENTINEL_URL = "https://user:p%40ss@health.example/check?token=s3cr3t"

#: fragments of it that must never surface (raw and encoded shapes)
SENTINEL_PARTS = (
    SENTINEL_URL,
    urllib.parse.quote(SENTINEL_URL, safe=""),
    urllib.parse.quote_plus(SENTINEL_URL),
    urllib.parse.quote(SENTINEL_URL, safe=":/?=&@"),
    "p%40ss",
    "s3cr3t",
    "token=s3cr3t",
    "token%3Ds3cr3t",
    "health.example/check",
    "health.example%2Fcheck",
    "%2Fcheck",
    "user:p",
)


class LockValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.addCleanup(self.sb.cleanup)

    def load(self, path: str) -> supply.Lock:
        return supply.load_lock(path)

    def test_shipped_lock_parses(self) -> None:
        lock = self.load(REAL_LOCK)
        self.assertEqual(lock.tag, "v1.19.32")
        for key in ("linux-amd64-v3", "linux-amd64-compatible", "linux-arm64", "geoip", "geosite"):
            asset = lock.asset(key)
            self.assertEqual(len(asset.sha256), 64, key)
            self.assertGreater(asset.size, 0, key)
        # every URL is the official host, https
        for asset in lock.assets.values():
            self.assertTrue(asset.url.startswith("https://github.com/MetaCubeX/"), asset.url)

    def test_lock_documents_no_fallback_and_no_credentials(self) -> None:
        with open(REAL_LOCK, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertIn("never degrade", raw["policy"]["fallback"].lower())
        self.assertTrue(raw["policy"]["credentials"].lower().startswith("none"))

    def test_missing_digest_fails_closed(self) -> None:
        path = support.mutate_lock(self.sb.lock_path, key="linux-amd64-v3", sha256=support.DELETE)
        with self.assertRaises(FailClosed) as caught:
            self.load(path)
        self.assertIn("sha256", str(caught.exception))

    def test_short_or_ambiguous_digest_fails_closed(self) -> None:
        for digest in ("deadbeef", "", "sha256:12", "z" * 64):
            path = support.mutate_lock(self.sb.lock_path, key="linux-amd64-v3", sha256=digest)
            with self.subTest(digest=digest), self.assertRaises(FailClosed):
                self.load(path)

    def test_missing_size_fails_closed(self) -> None:
        path = support.mutate_lock(self.sb.lock_path, key="linux-amd64-v3", size=support.DELETE)
        with self.assertRaises(FailClosed):
            self.load(path)

    def test_zero_size_fails_closed(self) -> None:
        path = support.mutate_lock(self.sb.lock_path, key="linux-amd64-v3", size=0)
        with self.assertRaises(FailClosed):
            self.load(path)

    def test_unofficial_host_fails_closed(self) -> None:
        mirror = "https://mirror.example.invalid/mihomo.gz"
        path = support.mutate_lock(self.sb.lock_path, key="linux-amd64-v3", url=mirror)
        with self.assertRaises(FailClosed) as caught:
            self.load(path)
        self.assertIn("official GitHub host", str(caught.exception))

    def test_plain_http_fails_closed(self) -> None:
        path = support.mutate_lock(
            self.sb.lock_path,
            key="linux-amd64-v3",
            url="http://github.com/MetaCubeX/mihomo/releases/download/x/mihomo-linux-amd64-v3-x.gz",
        )
        with self.assertRaises(FailClosed):
            self.load(path)

    def test_duplicate_key_fails_closed(self) -> None:
        with open(self.sb.lock_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["assets"].append(dict(payload["assets"][0]))
        path = os.path.join(self.sb.root, "dup.lock.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with self.assertRaises(FailClosed) as caught:
            self.load(path)
        self.assertIn("duplicated", str(caught.exception))

    def test_missing_required_asset_fails_closed(self) -> None:
        with open(self.sb.lock_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["assets"] = [a for a in payload["assets"] if a["key"] != "geosite"]
        path = os.path.join(self.sb.root, "partial.lock.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with self.assertRaises(FailClosed) as caught:
            self.load(path)
        self.assertIn("geosite", str(caught.exception))

    def test_missing_tag_fails_closed(self) -> None:
        with open(self.sb.lock_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["mihomo_tag"] = ""
        path = os.path.join(self.sb.root, "notag.lock.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with self.assertRaises(FailClosed):
            self.load(path)

    def test_geo_without_install_as_fails_closed(self) -> None:
        path = support.mutate_lock(self.sb.lock_path, key="geoip", install_as=support.DELETE)
        with self.assertRaises(FailClosed):
            self.load(path)

    def test_geo_traversal_install_as_fails_closed(self) -> None:
        path = support.mutate_lock(self.sb.lock_path, key="geoip", install_as="../../etc/GeoIP.dat")
        with self.assertRaises(FailClosed):
            self.load(path)

    def test_unknown_lock_kind_fails_closed(self) -> None:
        path = support.mutate_lock(self.sb.lock_path, key="geoip", kind="firmware")
        with self.assertRaises(FailClosed):
            self.load(path)

    def test_missing_lock_file_fails_closed(self) -> None:
        from mpm import lifecycle

        ctx = self.sb.ctx(lock_path=os.path.join(self.sb.root, "absent.lock.json"))
        self.sb.write_secret_file()
        with self.assertRaises(FailClosed) as caught:
            lifecycle.install(ctx)
        self.assertIn("lock file is missing", str(caught.exception))


class AssetSelectionTests(unittest.TestCase):
    def test_exact_match_required(self) -> None:
        assets = [{"name": "a.gz"}, {"name": "b.gz"}]
        self.assertEqual(supply.select_asset_by_name(assets, "a.gz")["name"], "a.gz")
        with self.assertRaises(FailClosed) as caught:
            supply.select_asset_by_name(assets, "c.gz")
        self.assertIn("no asset named", str(caught.exception))

    def test_ambiguous_match_fails_closed(self) -> None:
        assets = [{"name": "a.gz", "digest": "sha256:" + "1" * 64}, {"name": "a.gz", "digest": "sha256:" + "2" * 64}]
        with self.assertRaises(FailClosed) as caught:
            supply.select_asset_by_name(assets, "a.gz")
        self.assertIn("ambiguous", str(caught.exception))

    def test_digest_absent_in_metadata_fails_closed(self) -> None:
        with self.assertRaises(FailClosed) as caught:
            supply.digest_from_metadata({"name": "a.gz"})
        self.assertIn("unverified download", str(caught.exception))

    def test_normalise_digest_forms(self) -> None:
        bare = "a" * 64
        self.assertEqual(supply.normalise_digest(bare), bare)
        self.assertEqual(supply.normalise_digest("SHA256:" + bare), bare)
        for bad in ("sha256:" + "a" * 63, "md5:" + "a" * 32, "", "  "):
            with self.subTest(bad=bad), self.assertRaises(FailClosed):
                supply.normalise_digest(bad)

    def test_lock_digest_mismatch_against_metadata_fails(self) -> None:
        lock = supply.load_lock(REAL_LOCK)
        binary_names = [a.name for a in lock.assets.values() if a.kind == "binary"]
        self.assertEqual(len(binary_names), 3)
        # one entry per pinned asset, all with a *wrong* digest
        metadata = [{"name": name, "digest": "sha256:" + "f" * 64} for name in binary_names]
        with self.assertRaises(FailClosed) as caught:
            supply.audit_lock_against_metadata(lock, metadata)
        self.assertIn("disagrees", str(caught.exception))
        # and it passes when every digest agrees
        supply.audit_lock_against_metadata(
            lock,
            [
                {"name": name, "digest": "sha256:" + lock.asset_by_name(name).sha256}
                for name in binary_names
            ],
        )


class VerifyInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.suffix = evaluate(self.sb.facts).asset_suffix

    def re_pin(self, payload: bytes) -> None:
        """Serve ``payload`` and make the lock agree with it, so that a later
        rejection must come from the *content* checks (gzip/ELF/arch), not from
        the digest check."""
        self.sb.mock_binary = payload
        self.sb.rebuild_lock(binary_payload=payload)

    def run_install(self, *, payload: bytes | None = None, fetch=None, suffix: str | None = None,
                    re_pin: bool = True):
        served = payload if payload is not None else self.sb.mock_binary
        if payload is not None and re_pin:
            self.re_pin(payload)
        self.lock = supply.load_lock(self.sb.lock_path)

        def serve(url: str, dest: str) -> None:
            data = served
            if url.rsplit("/", 1)[-1].startswith("geo"):
                data = support.GEO_MOCK_BYTES
            with open(dest, "wb") as handle:
                handle.write(data)

        from mpm.atomicio import ensure_dir

        ensure_dir(self.sb.layout.staging_dir, 0o700)
        return supply.install_verified(
            layout=self.sb.layout,
            lock=self.lock,
            asset_suffix=suffix or self.suffix,
            fetch=fetch or serve,
        )

    def test_happy_path_installs_and_switches_symlink(self) -> None:
        outcome = self.run_install()
        self.assertEqual(outcome.version, "v0.0.0-mock")
        self.assertTrue(os.path.islink(self.sb.layout.libexec_current))
        self.assertTrue(os.path.isfile(self.sb.layout.binary))
        self.assertEqual(support.file_mode(self.sb.layout.binary), 0o755)
        target = os.readlink(self.sb.layout.libexec_current)
        self.assertTrue(target.endswith(os.path.join("mihomo-proxy-management", "v0.0.0-mock")), target)
        self.assertEqual(os.listdir(self.sb.layout.staging_dir), [])

    def test_digest_mismatch_installs_nothing(self) -> None:
        # lock still pins the original bytes, we serve something else
        original = self.sb.lock_path
        self.sb.rebuild_lock(binary_payload=support.gz(support.make_elf()))
        self.sb.lock_path = original  # lock disagrees with what we will serve
        self.sb.mock_binary = support.gz(support.make_elf() + b"tampered")
        with self.assertRaises(FailClosed) as caught:
            self.run_install(re_pin=False)
        message = str(caught.exception)
        self.assertIn("sha256 mismatch", message)
        self.assertIn("expected sha256:", message)
        self.assertFalse(os.path.exists(self.sb.layout.libexec_current))
        self.assertFalse(os.path.isdir(self.sb.layout.libexec_version_dir("v0.0.0-mock")))

    def test_size_mismatch_installs_nothing(self) -> None:
        # correct digest, wrong pinned size -> fail
        with open(self.sb.lock_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        for entry in payload["assets"]:
            if entry["kind"] == "binary":
                entry["size"] = entry["size"] - 1
        path = os.path.join(self.sb.root, "badsize.lock.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        lock = supply.load_lock(path)
        from mpm.atomicio import ensure_dir

        ensure_dir(self.sb.layout.staging_dir, 0o700)
        with self.assertRaises(FailClosed) as caught:
            supply.install_verified(
                layout=self.sb.layout, lock=lock, asset_suffix=self.suffix, fetch=self._serve_default()
            )
        self.assertIn("size mismatch", str(caught.exception))
        self.assertFalse(os.path.exists(self.sb.layout.libexec_current))

    def _serve_default(self):
        def serve(url: str, dest: str) -> None:
            data = self.sb.mock_binary
            if url.rsplit("/", 1)[-1].startswith("geo"):
                data = support.GEO_MOCK_BYTES
            with open(dest, "wb") as handle:
                handle.write(data)

        return serve

    def test_non_gzip_payload_rejected(self) -> None:
        with self.assertRaises(FailClosed) as caught:
            self.run_install(payload=b"PK\x03\x04 not gzip at all")
        self.assertIn("gzip", str(caught.exception))

    def test_non_elf_payload_rejected(self) -> None:
        with self.assertRaises(FailClosed) as caught:
            self.run_install(payload=support.gz(b"\xca\xfe\xba\xbe" + b"\x00" * 200))
        self.assertIn("not an ELF", str(caught.exception))

    def test_wrong_architecture_rejected(self) -> None:
        # x86_64 host, arm64 binary
        with self.assertRaises(FailClosed) as caught:
            self.run_install(payload=support.gz(support.make_elf(machine=0xB7)))
        self.assertIn("architecture mismatch", str(caught.exception))
        self.assertFalse(os.path.exists(self.sb.layout.libexec_current))

    def test_arm64_host_accepts_arm64_binary(self) -> None:
        arm_bytes = support.gz(support.make_elf(machine=0xB7))
        sb = support.Sandbox(machine="aarch64", binary_bytes=arm_bytes)
        self.addCleanup(sb.cleanup)
        suffix = evaluate(sb.facts).asset_suffix
        self.assertEqual(suffix, "arm64")

        def serve(url: str, dest: str) -> None:
            data = arm_bytes
            if url.rsplit("/", 1)[-1].startswith("geo"):
                data = support.GEO_MOCK_BYTES
            with open(dest, "wb") as handle:
                handle.write(data)

        from mpm.atomicio import ensure_dir

        ensure_dir(sb.layout.staging_dir, 0o700)
        outcome = supply.install_verified(
            layout=sb.layout,
            lock=supply.load_lock(sb.lock_path),
            asset_suffix=suffix,
            fetch=serve,
        )
        self.assertEqual(outcome.asset, "mihomo-linux-arm64-v0.0.0-mock.gz")

    def test_x86_host_rejects_arm64_and_vice_versa(self) -> None:
        arm = support.gz(support.make_elf(machine=0xB7))
        with self.assertRaises(FailClosed):
            self.run_install(payload=arm, suffix="amd64-v3")
        x86 = support.gz(support.make_elf(machine=0x3E))
        sb = support.Sandbox(machine="aarch64", binary_bytes=x86)
        self.addCleanup(sb.cleanup)
        from mpm.atomicio import ensure_dir

        ensure_dir(sb.layout.staging_dir, 0o700)
        with self.assertRaises(FailClosed) as caught:
            supply.install_verified(
                layout=sb.layout,
                lock=supply.load_lock(sb.lock_path),
                asset_suffix="arm64",
                fetch=self._server(x86),
            )
        self.assertIn("architecture mismatch", str(caught.exception))

    def _server(self, data: bytes):
        def serve(url: str, dest: str) -> None:
            payload = data
            if url.rsplit("/", 1)[-1].startswith("geo"):
                payload = support.GEO_MOCK_BYTES
            with open(dest, "wb") as handle:
                handle.write(payload)

        return serve

    def test_truncated_elf_header_rejected(self) -> None:
        with self.assertRaises(FailClosed) as caught:
            self.run_install(payload=support.gz(b"\x7fELF\x02\x01\x01" + b"\x00" * 5))
        self.assertIn("ELF header truncated", str(caught.exception))

    def test_tar_member_escape_rejected(self) -> None:
        archive = support.tar_gz([("../../etc/passwd", b"evil")])
        with self.assertRaises(FailClosed) as caught:
            self.run_install(payload=archive)
        self.assertIn("single path element", str(caught.exception))

    def test_multi_member_archive_rejected(self) -> None:
        archive = support.tar_gz([("mihomo", support.make_elf()), ("README", b"extra")])
        with self.assertRaises(FailClosed) as caught:
            self.run_install(payload=archive)
        self.assertIn("exactly one file", str(caught.exception))

    def test_single_member_tar_gz_is_accepted(self) -> None:
        archive = support.tar_gz([("mihomo", support.make_elf())])
        outcome = self.run_install(payload=archive)
        self.assertTrue(os.path.isfile(self.sb.layout.binary))
        self.assertEqual(outcome.sha256, hashlib.sha256(archive).hexdigest())

    def test_geo_digest_mismatch_aborts_before_switch(self) -> None:
        def serve(url: str, dest: str) -> None:
            name = url.rsplit("/", 1)[-1]
            data = self.sb.mock_binary if name.startswith("mihomo-") else b"different-geo-bytes"
            with open(dest, "wb") as handle:
                handle.write(data)

        with self.assertRaises(FailClosed) as caught:
            self.run_install(fetch=serve, re_pin=False)
        self.assertIn("geo asset", str(caught.exception))
        self.assertFalse(os.path.exists(self.sb.layout.libexec_current))
        self.assertFalse(os.path.exists(self.sb.layout.geoip))

    def test_download_error_installs_nothing(self) -> None:
        def boom(url: str, dest: str) -> None:
            raise OSError("network down")

        with self.assertRaises((FailClosed, OSError)):
            self.run_install(fetch=boom, re_pin=False)
        self.assertFalse(os.path.exists(self.sb.layout.libexec_current))

    def test_staging_directory_is_0700(self) -> None:
        modes = {}

        def serve_and_record(url: str, dest: str) -> None:
            modes[os.path.dirname(dest)] = support.file_mode(os.path.dirname(dest))
            data = self.sb.mock_binary
            if url.rsplit("/", 1)[-1].startswith("geo"):
                data = support.GEO_MOCK_BYTES
            with open(dest, "wb") as handle:
                handle.write(data)

        self.run_install(fetch=serve_and_record)
        self.assertTrue(modes, "staging was never used")
        for directory, mode in modes.items():
            self.assertEqual(mode, 0o700, directory)

    def test_second_install_switches_symlink_atomically(self) -> None:
        self.run_install()
        first = os.readlink(self.sb.layout.libexec_current)
        outcome = self.run_install()
        self.assertEqual(os.readlink(self.sb.layout.libexec_current), first)
        self.assertEqual(outcome.version, "v0.0.0-mock")

    def test_install_never_executes_the_downloaded_binary(self) -> None:
        before = len(self.sb.executor.calls)
        self.run_install()
        self.assertEqual(len(self.sb.executor.calls), before)


class LifecycleSupplyTests(unittest.TestCase):
    """Failure of verification must leave the host untouched (D9b=A)."""

    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.sb.write_secret_file()

    def test_digest_mismatch_prevents_config_unit_and_start(self) -> None:
        from mpm import lifecycle

        # lock still pins the original payload; serve different bytes instead
        self.sb.mock_binary = self.sb.mock_binary + b"tampered-after- compression"
        with self.assertRaises(FailClosed):
            lifecycle.install(self.sb.ctx())
        self.assertFalse(self.sb.systemd_state.active)
        self.assertFalse(os.path.exists(self.sb.layout.unit))
        self.assertEqual(self.sb.executor.count("systemctl start"), 0)
        self.assertEqual(self.sb.executor.count("systemctl enable"), 0)

    def test_api_failure_never_degrades_to_unverified_download(self) -> None:
        def failing(url: str, dest: str) -> None:
            raise FailClosed("release metadata unreachable")

        from mpm import lifecycle

        ctx = self.sb.ctx(fetch_override=failing)
        with self.assertRaises(FailClosed) as caught:
            lifecycle.install(ctx)
        self.assertIn("metadata unreachable", str(caught.exception))
        self.assertFalse(os.path.exists(self.sb.layout.libexec_current))

    def test_failure_message_keeps_digests_and_drops_urls(self) -> None:
        from mpm import lifecycle

        self.sb.mock_binary = b"\x1f\x8b" + b"garbage-not-gzip-stream"
        with self.assertRaises(FailClosed) as caught:
            lifecycle.install(self.sb.ctx())
        message = str(caught.exception)
        self.assertIn("sha256 mismatch", message)
        # the pinned URL host is fine to show, the query-string token is not
        self.assertNotIn("token=", message)


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.secret = self.sb.canary_secret
        self.controller = controller_mod.Controller(secret=self.secret, transport=self.sb.transport)

    def test_every_request_carries_bearer_authorization(self) -> None:
        self.controller.version()
        self.controller.proxies()
        self.controller.providers()
        self.controller.provider(support.FOREIGN)
        self.controller.healthcheck(support.FOREIGN)
        self.controller.refresh_provider(support.FOREIGN)
        self.assertTrue(self.sb.transport.calls)
        for call in self.sb.transport.calls:
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + self.secret)

    def test_empty_secret_refused_construction(self) -> None:
        with self.assertRaises(FailClosed) as caught:
            controller_mod.Controller(secret="", transport=self.sb.transport)
        self.assertIn("unauthenticated", str(caught.exception))

    def test_whitespace_only_secret_refused(self) -> None:
        with self.assertRaises(FailClosed):
            controller_mod.Controller(secret="   ", transport=self.sb.transport)

    def test_http_error_raises_with_status(self) -> None:
        self.sb.transport.responses["GET /version"] = (401, b"{}")
        with self.assertRaises(controller_mod.ControllerError) as caught:
            self.controller.version()
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn(self.secret, str(caught.exception))

    def test_server_error_propagates(self) -> None:
        self.sb.transport.responses["PUT /providers/proxies/subscription-foreign"] = (500, b"boom")
        with self.assertRaises(controller_mod.ControllerError):
            self.controller.refresh_provider(support.FOREIGN)

    def test_call_log_never_records_query_strings(self) -> None:
        self.controller.delay("PROXY", url=self.sb.canary_foreign)
        paths = [call["path"] for call in self.controller.calls]
        self.assertEqual(paths, ["/proxies/PROXY/delay"])
        for path in paths:
            self.assertNotIn("?", path)
            self.assertNotIn(self.sb.canary_foreign, path)

    def test_non_2xx_error_never_echoes_the_query_string(self) -> None:
        """A percent-encoded health-check URL is invisible to the sanitizer.

        ``delay()`` puts the probe URL into the query string, so the exception
        text may only contain the endpoint - raw *and* percent-encoded values
        must be absent from stdout, stderr, the JSON dump and the exception.
        """
        out, err = support.CapturingStream(), support.CapturingStream()
        self.sb.transport.responses["GET /proxies/CN-EXIT/delay"] = (
            502,
            json.dumps({"error": "upstream rejected the probe"}).encode(),
        )
        with self.assertRaises(controller_mod.ControllerError) as caught:
            self.controller.delay(
                "CN-EXIT", url="https://user:token-abc123@cn-health.example.invalid/generate_204"
            )
        message = str(caught.exception)
        for stream in (out, err):
            stream.write(message)
        rendered = message + json.dumps({"error": message}) + out.text + err.text
        for secret in (
            "token-abc123",
            "cn-health.example.invalid",
            "generate_204",
            # the percent-encoded shapes the query string actually carries
            "user%3Atoken-abc123",
            "https%3A%2F%2F",
            "%2Fgenerate_204",
            "user:token-abc123@cn-health",
        ):
            self.assertNotIn(secret, rendered, secret)
        # the endpoint itself is still shown, so the failure stays debuggable
        self.assertIn("/proxies/CN-EXIT/delay", message)
        self.assertEqual(caught.exception.status, 502)
        self.assertNotIn("?", message)

    def test_non_2xx_error_with_a_canary_probe_url_is_clean(self) -> None:
        """Same guarantee with the sandbox's own canary values registered."""
        self.sb.transport.responses["GET /proxies/PROXY/delay"] = (500, b"boom")
        with self.assertRaises(controller_mod.ControllerError) as caught:
            self.controller.delay("PROXY", url=self.sb.canary_health)
        message = str(caught.exception)
        for canary in self.sb.canaries():
            self.assertNotIn(canary, message)
            self.assertNotIn(canary.replace("/", "%2F").replace(":", "%3A"), message)
        self.assertIn("/proxies/PROXY/delay", message)

    # -- item 1 (review round 2): the check must cover *every* non-2xx -------

    def test_every_non_2xx_status_raises(self) -> None:
        """1xx/3xx/4xx/5xx are all errors: a redirect is never a success."""
        for status in (100, 101, 199, 300, 301, 302, 303, 304, 307, 308, 400, 401, 403, 404, 500, 502, 503):
            with self.subTest(status=status):
                self.sb.transport.responses["GET /version"] = (
                    status,
                    json.dumps({"version": SENTINEL_URL}).encode(),
                )
                with self.assertRaises(controller_mod.ControllerError) as caught:
                    self.controller.request("GET", "/version")
                self.assertEqual(caught.exception.status, status)

    def test_2xx_statuses_still_return_their_payload(self) -> None:
        """Guard against tightening the check past the 2xx family."""
        for status in (200, 201, 202):
            with self.subTest(status=status):
                self.sb.transport.responses["GET /version"] = (
                    status,
                    b'{"version": "mock-1.0.0"}',
                )
                code, payload = self.controller.request("GET", "/version")
                self.assertEqual(code, status)
                self.assertEqual(payload, {"version": "mock-1.0.0"})
        # 204 no content is a success with an empty payload
        self.sb.transport.responses["GET /version"] = (204, b"")
        code, payload = self.controller.request("GET", "/version")
        self.assertEqual((code, payload), (204, None))

    def test_non_2xx_never_returns_the_body_in_any_status_class(self) -> None:
        """3xx/4xx/5xx, body in plain and percent-encoded form.

        The sentinel URL must appear in neither the exception text nor the CLI
        output channels, and the call must not resolve to a payload.
        """
        from mpm import cli, lifecycle

        bodies = {
            "json-raw": json.dumps({"url": SENTINEL_URL, "delay": 42}).encode(),
            "json-encoded": json.dumps(
                {"url": urllib.parse.quote(SENTINEL_URL, safe=""), "delay": 42}
            ).encode(),
            "text-raw": ("redirecting to " + SENTINEL_URL).encode(),
            "text-encoded": (
                "redirecting to " + urllib.parse.quote_plus(SENTINEL_URL)
            ).encode(),
        }
        for shape, status in (("3xx", 302), ("4xx", 404), ("5xx", 503)):
            for label, body in bodies.items():
                key = "GET /proxies/CN-EXIT/delay"
                self.sb.transport.responses[key] = (status, body)
                ctx = self.sb.ctx()
                with self.subTest(shape=shape, status=status, body=label):
                    with self.assertRaises(controller_mod.ControllerError) as caught:
                        self.controller.delay("CN-EXIT", url=SENTINEL_URL)
                    message = str(caught.exception)
                    self.assertEqual(caught.exception.status, status)
                    # route the message through the real output channels
                    report = lifecycle.Report(command="test", status="FAILED")
                    report.add(message)
                    report.warn(message)
                    self.assertNotEqual(cli.emit(ctx, report, as_json=True), 0)
                    self.assertNotEqual(cli.emit(ctx, report, as_json=False), 0)
                    rendered = message + json.dumps({"error": message}) + ctx.out.text + ctx.err.text
                    for part in SENTINEL_PARTS:
                        self.assertNotIn(part, rendered, part)
                    # the body was never parsed into a payload
                    self.assertNotIn("42", rendered)
                    # and the failure stays debuggable
                    self.assertIn("/proxies/CN-EXIT/delay", message)
                    self.assertIn(str(status), message)
                    self.assertNotIn("?", message)
                self.sb.transport.responses.pop(key, None)

    def test_3xx_with_a_plausible_delay_body_is_not_a_pass(self) -> None:
        """A 302 carrying ``{"delay": 42}`` must not silently count as success."""
        self.sb.transport.responses["GET /proxies/CN-EXIT/delay"] = (
            302,
            json.dumps({"delay": 42}).encode(),
        )
        with self.assertRaises(controller_mod.ControllerError) as caught:
            self.controller.delay("CN-EXIT", url=SENTINEL_URL)
        self.assertEqual(caught.exception.status, 302)

    def test_redirect_body_never_reaches_readiness(self) -> None:
        """/version answering 301 with an echoed body must not pass readiness.

        Before the fix this was a success: the body parsed, ``version`` was the
        sentinel URL, and readiness would have reported it as the running
        version.
        """
        from mpm import lifecycle

        ctx = self.sb.installed()
        lifecycle.stop(ctx)
        self.sb.transport.responses["GET /version"] = (
            301,
            json.dumps({"version": SENTINEL_URL}).encode(),
        )
        with self.assertRaises(NotReady):
            lifecycle.start(self.sb.ctx())
        out = self.sb.outputs()
        for part in SENTINEL_PARTS:
            self.assertNotIn(part, out, part)

    def test_non_json_body_is_sanitised(self) -> None:
        self.sb.transport.responses["GET /version"] = (
            200,
            ("token=leaked-secret-value " + self.secret).encode(),
        )
        payload = self.controller.request("GET", "/version")[1]
        self.assertNotIn("leaked-secret-value", payload)
        self.assertNotIn(self.secret, payload)

    def test_base_url_is_loopback_only(self) -> None:
        self.assertTrue(controller_mod.BASE_URL.startswith("http://127.0.0.1:"))
        self.assertNotIn("0.0.0.0", controller_mod.BASE_URL)


class UnitContentTests(unittest.TestCase):
    def render(self, *, tun: bool) -> str:
        return unit_mod.render(
            binary="/usr/libexec/mihomo-proxy-management/current/mihomo",
            state_dir="/var/lib/mihomo-proxy-management",
            run_dir="/run/mihomo-proxy-management",
            cli_entry="/usr/local/bin/mihomo-proxy-management",
            tun=tun,
        )

    def test_required_directives_present(self) -> None:
        for tun in (True, False):
            text = self.render(tun=tun)
            with self.subTest(tun=tun):
                unit_mod.audit(text, tun=tun)
                self.assertIn("Type=simple", text)
                self.assertIn("Restart=on-failure", text)
                self.assertIn("RestartSec=", text)
                self.assertIn("StartLimitIntervalSec=", text)
                self.assertIn("StartLimitBurst=", text)
                self.assertIn("KillMode=control-group", text)
                self.assertIn("UMask=0077", text)
                self.assertIn("NoNewPrivileges=true", text)
                self.assertIn("ProtectSystem=full", text)
                self.assertIn("ProtectHome=true", text)
                self.assertIn("PrivateTmp=true", text)
                self.assertIn("ExecStartPre=", text)

    def test_forbidden_directives_absent(self) -> None:
        for tun in (True, False):
            text = self.render(tun=tun)
            for forbidden in (
                "PrivateDevices=true",
                "PrivateUsers=true",
                "CAP_SYS_ADMIN",
                "Environment=",
                "EnvironmentFile=",
                "pkill",
                "pgrep",
                "killall",
            ):
                self.assertNotIn(forbidden, text, f"tun={tun} {forbidden}")
            self.assertIsNone(__import__("re").search(r"^ConditionPathExists=!/", text, __import__("re").M))

    def test_working_directory_equals_d_flag(self) -> None:
        for tun in (True, False):
            text = self.render(tun=tun)
            working = [l for l in text.splitlines() if l.startswith("WorkingDirectory=")][0].split("=", 1)[1]
            exec_start = [l for l in text.splitlines() if l.startswith("ExecStart=")][0].split("=", 1)[1]
            self.assertEqual(exec_start.split(" -d ")[1].strip(), working)

    def test_execstart_has_only_binary_and_d(self) -> None:
        text = self.render(tun=True)
        exec_start = [l for l in text.splitlines() if l.startswith("ExecStart=")][0]
        args = exec_start.split("=", 1)[1].split()
        self.assertEqual(args, ["/usr/libexec/mihomo-proxy-management/current/mihomo", "-d", "/var/lib/mihomo-proxy-management"])

    def test_execstartpre_is_project_preflight_or_configure(self) -> None:
        text = self.render(tun=True)
        line = [l for l in text.splitlines() if l.startswith("ExecStartPre=")][0]
        self.assertIn("configure --check-only", line)
        self.assertIn("/usr/local/bin/mihomo-proxy-management", line)

    def test_execstartpre_executes_the_wrapper_not_an_interpreter(self) -> None:
        """The wrapper is a /bin/sh script; running it through python3 makes the
        unit fail to start (item: ExecStartPre must not parse shell as Python)."""
        for tun in (True, False):
            text = self.render(tun=tun)
            line = [l for l in text.splitlines() if l.startswith("ExecStartPre=")][0]
            program = line.split("=", 1)[1].split()[0]
            with self.subTest(tun=tun):
                self.assertEqual(program, "/usr/local/bin/mihomo-proxy-management")
                self.assertNotIn("python3", line)
                self.assertNotIn("/usr/bin/python3", text)

    def test_audit_rejects_an_interpreter_prefixed_execstartpre(self) -> None:
        text = self.render(tun=True)
        tampered = text.replace(
            "ExecStartPre=/usr/local/bin/mihomo-proxy-management",
            "ExecStartPre=/usr/bin/python3 /usr/local/bin/mihomo-proxy-management",
        )
        self.assertNotEqual(tampered, text)
        with self.assertRaises(FailClosed) as caught:
            unit_mod.audit(tampered, tun=True)
        self.assertIn("interpreter", str(caught.exception))

    def test_audit_rejects_a_relative_execstartpre_program(self) -> None:
        text = self.render(tun=True)
        tampered = text.replace(
            "ExecStartPre=/usr/local/bin/mihomo-proxy-management",
            "ExecStartPre=mihomo-proxy-management",
        )
        with self.assertRaises(FailClosed):
            unit_mod.audit(tampered, tun=True)

    def test_render_refuses_an_empty_cli_entry(self) -> None:
        with self.assertRaises(FailClosed):
            unit_mod.render(
                binary="/usr/libexec/mihomo-proxy-management/current/mihomo",
                state_dir="/var/lib/mihomo-proxy-management",
                run_dir="/run/mihomo-proxy-management",
                cli_entry="",
                tun=True,
            )

    def test_tun_profile_grants_only_net_admin_net_raw(self) -> None:
        text = self.render(tun=True)
        self.assertIn("AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW", text)
        self.assertIn("CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW", text)

    def test_degraded_profile_grants_no_capabilities(self) -> None:
        text = self.render(tun=False)
        self.assertIn("CapabilityBoundingSet=", text)
        self.assertNotIn("AmbientCapabilities=", text)

    def test_unit_install_section_is_multi_user(self) -> None:
        self.assertIn("WantedBy=multi-user.target", self.render(tun=True))

    def test_audit_rejects_tampered_unit(self) -> None:
        mutations = {
            "restart-always": "Restart=on-failure",
            "killmode-process": "KillMode=control-group",
            "umask": "UMask=0077",
            "nonpriv": "NoNewPrivileges=true",
            "private-devices": "PrivateTmp=true",
        }
        text = self.render(tun=True)
        replacements = {
            "restart-always": "Restart=always",
            "killmode-process": "KillMode=process",
            "umask": "UMask=0022",
            "nonpriv": "NoNewPrivileges=false",
            "private-devices": "PrivateDevices=true",
        }
        for label, needle in mutations.items():
            with self.subTest(label):
                tampered = text.replace(needle, replacements[label])
                with self.assertRaises(FailClosed):
                    unit_mod.audit(tampered, tun=True)

    def test_shipped_template_is_non_secret_and_documents_omissions(self) -> None:
        path = os.path.join(support.LINUX_DIR, "systemd", "mihomo-proxy-management.service.template")
        text = support.read_text(path)
        for canary in ("MPM_SUBSCRIPTION_URL", "token="):
            self.assertNotIn(canary, text)
        for placeholder in ("{BINARY}", "{STATE_DIR}", "{CLI}"):
            self.assertIn(placeholder, text)
        # the shipped reference must not run the CLI wrapper through python3
        exec_pre = [l for l in text.splitlines() if l.startswith("ExecStartPre=")][0]
        self.assertNotIn("python3", exec_pre)
        self.assertIn("{CLI}", exec_pre)


class RealSystemdAnalyzeTests(unittest.TestCase):
    """The host has systemd-analyze; use it on a rendered unit (read-only check,
    no unit is ever installed or started)."""

    @classmethod
    def setUpClass(cls) -> None:
        import shutil

        cls.analyze = shutil.which("systemd-analyze")

    def test_rendered_unit_passes_systemd_analyze_verify(self) -> None:
        if not self.analyze:
            self.skipTest("systemd-analyze NOT_RUN: binary not present on this host")
        import subprocess
        import tempfile

        for tun in (True, False):
            text = unit_mod.render(
                binary="/usr/libexec/mihomo-proxy-management/current/mihomo",
                state_dir="/var/lib/mihomo-proxy-management",
                run_dir="/run/mihomo-proxy-management",
                cli_entry="/usr/local/bin/mihomo-proxy-management",
                tun=tun,
            )
            with tempfile.TemporaryDirectory() as work:
                unit_path = os.path.join(work, unit_mod.UNIT_FILENAME)
                with open(unit_path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                # ensure the referenced programs exist so verify does not complain
                binary = os.path.join(work, "mihomo")
                with open(binary, "w", encoding="utf-8") as handle:
                    handle.write("#!/bin/sh\nexit 0\n")
                os.chmod(binary, 0o755)
                # ExecStartPre now names the CLI wrapper itself, so the fixture
                # has to provide that path too
                wrapper = os.path.join(work, "mihomo-proxy-management")
                with open(wrapper, "w", encoding="utf-8") as handle:
                    handle.write("#!/bin/sh\nexit 0\n")
                os.chmod(wrapper, 0o755)
                patched = text.replace(
                    "/usr/libexec/mihomo-proxy-management/current/mihomo", binary
                ).replace("/usr/local/bin/mihomo-proxy-management", wrapper)
                with open(unit_path, "w", encoding="utf-8") as handle:
                    handle.write(patched)
                result = subprocess.run(
                    [self.analyze, "verify", unit_path],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                complaints = [
                    line
                    for line in (result.stdout + result.stderr).splitlines()
                    if "executable" in line or "Unknown" in line or "invalid" in line.lower()
                ]
                self.assertFalse(
                    complaints,
                    f"tun={tun} rc={result.returncode} output={result.stdout + result.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
