"""deploy.supply: digest-locked staging is pure, offline, and leak-free."""

import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import supply  # noqa: E402

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
PAYLOAD = b"mihomo fake binary payload"
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
MARKER = "UNIQUE-DOWNLOAD-CONTENT-7f3a9c4b"
MARKER_PAYLOAD = b"prefix " + MARKER.encode("ascii") + b" suffix"

#: Sentinel for "remove this field from the entry" in lock overrides.
DELETE = object()


def entry(**overrides):
    """One minimal valid lock asset entry, with fields overridden or removed."""
    base = {"key": "tool", "os": "macos", "arch": "arm64",
            "url": "https://github.com/example/tool/releases/download/v1/tool-v1.gz",
            "digest": "ab" * 32, "size": 5, "fmt": "gzip", "install_as": "tool"}
    for name, value in overrides.items():
        if value is DELETE:
            base.pop(name, None)
        else:
            base[name] = value
    return base


def lock_doc(**asset_overrides):
    """A minimal valid lock document; ``asset_overrides`` edits the one entry."""
    return {"schema_version": 1, "assets": [entry(**asset_overrides)]}


def write_lock(directory, doc, name="supply.lock.json"):
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as stream:
        if isinstance(doc, str):
            stream.write(doc)
        else:
            json.dump(doc, stream)
    return path


def make_asset(**overrides):
    """A raw-format asset whose digest/size match ``PAYLOAD``."""
    fields = dict(key="tool", os="macos", arch="arm64",
                  url="https://github.com/example/tool/releases/download/v1/tool-v1",
                  digest=PAYLOAD_SHA, size=len(PAYLOAD), fmt="raw",
                  install_as="tool", mode="0644")
    fields.update(overrides)
    return supply.Asset(**fields)


def gzip_asset(payload=PAYLOAD, **overrides):
    data = gzip.compress(payload)
    fields = dict(key="gztool", os="macos", arch="arm64",
                  url="https://github.com/example/tool/releases/download/v1/tool-v1.gz",
                  digest=hashlib.sha256(data).hexdigest(), size=len(data),
                  fmt="gzip", install_as="tool", mode="0644")
    fields.update(overrides)
    return supply.Asset(**fields), data


def zip_data(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def zip_asset(entries, member, **overrides):
    data = zip_data(entries)
    fields = dict(key="ziptool", os="macos", arch="arm64",
                  url="https://github.com/example/tool/releases/download/v1/tool-v1.zip",
                  digest=hashlib.sha256(data).hexdigest(), size=len(data),
                  fmt="zip", member=member, install_as="tool", mode="0644")
    fields.update(overrides)
    return supply.Asset(**fields), data


def fake_fetch(pairs, fail_on=None):
    """A fetcher writing canned bytes to ``destination``; ``pairs`` maps a URL
    to the bytes it serves, ``fail_on`` maps a URL to an exception (its
    message deliberately carries leakable text)."""
    calls = []

    def fetch(url, destination):
        calls.append(url)
        if fail_on and url in fail_on:
            raise RuntimeError(f"boom while fetching {url} " + MARKER)
        with open(destination, "wb") as stream:
            stream.write(pairs[url])

    fetch.calls = calls
    return fetch


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="supply-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class LockFileTests(unittest.TestCase):
    """The shipped lock loads, validates, and selects by platform."""

    def setUp(self):
        self.lock = supply.load_lock()

    def test_schema_version_is_one(self):
        self.assertEqual(self.lock["schema_version"], 1)

    def test_top_level_keys_are_preserved(self):
        for key in ("policy", "mihomo_tag", "python_tag", "generated_at"):
            self.assertIn(key, self.lock)

    def test_macos_arm64_selection(self):
        keys = {asset.key for asset in
                supply.select_assets(self.lock, os_name="macos", arch="arm64")}
        self.assertEqual(keys, {"mihomo-darwin-arm64", "geoip", "geosite", "geoip-metadb"})

    def test_windows_amd64_selection(self):
        keys = {asset.key for asset in
                supply.select_assets(self.lock, os_name="windows", arch="amd64")}
        self.assertTrue({"mihomo-windows-amd64-v3", "wintun", "python-windows-amd64",
                         "geoip", "geosite", "geoip-metadb"} <= keys)
        self.assertFalse(any("darwin" in key for key in keys))

    def test_every_asset_is_well_formed(self):
        for asset in self.lock["assets"]:
            if asset.rolling:
                self.assertTrue(asset.api, asset.key)
            if asset.fmt == "zip":
                self.assertTrue(asset.member, asset.key)
            self.assertRegex(asset.digest, r"^[0-9a-f]{64}$")


class LockValidationTests(TempDirTestCase):
    """Every malformed lock is rejected with kind and the offending field."""

    def load(self, doc):
        return supply.load_lock(write_lock(self.tmp, doc))

    def assert_invalid(self, doc, detail=None):
        with self.assertRaises(supply.SupplyError) as caught:
            self.load(doc)
        self.assertEqual(caught.exception.kind, "lock_invalid")
        if detail is not None:
            self.assertEqual(caught.exception.detail, detail)
        return caught.exception

    def test_missing_file(self):
        with self.assertRaises(supply.SupplyError) as caught:
            supply.load_lock(os.path.join(self.tmp, "absent.json"))
        self.assertEqual(caught.exception.kind, "lock_missing")
        self.assertEqual(caught.exception.detail, "")

    def test_not_json(self):
        self.assert_invalid("this is {{{ not json")

    def test_top_level_not_an_object(self):
        self.assert_invalid([1, 2])

    def test_bad_schema_version(self):
        self.assert_invalid({"schema_version": 2, "assets": [entry()]}, "schema_version")

    def test_empty_assets(self):
        self.assert_invalid({"schema_version": 1, "assets": []}, "assets")

    def test_assets_not_a_list(self):
        self.assert_invalid({"schema_version": 1, "assets": {"tool": {}}}, "assets")

    def test_entry_not_an_object(self):
        self.assert_invalid({"schema_version": 1, "assets": ["tool"]}, "assets[0]")

    def test_missing_required_field(self):
        self.assert_invalid(lock_doc(digest=DELETE), "digest")

    def test_digest_not_hex64(self):
        for digest in ("AB" * 32, "zz" * 32, "ab" * 31, 123):
            with self.subTest(digest=str(digest)[:8]):
                self.assert_invalid(lock_doc(digest=digest), "digest")

    def test_size_zero_or_string_or_bool(self):
        for size in (0, -1, "5", True):
            with self.subTest(size=repr(size)):
                self.assert_invalid(lock_doc(size=size), "size")

    def test_bad_fmt(self):
        self.assert_invalid(lock_doc(fmt="tar"), "fmt")

    def test_bad_os(self):
        self.assert_invalid(lock_doc(os="android"), "os")

    def test_bad_arch(self):
        self.assert_invalid(lock_doc(arch="386"), "arch")

    def test_empty_key(self):
        self.assert_invalid(lock_doc(key=""), "key")

    def test_duplicate_key(self):
        doc = lock_doc()
        doc["assets"].append(entry(key="tool", fmt="raw", install_as="other"))
        self.assert_invalid(doc, "tool")

    def test_rolling_without_api(self):
        self.assert_invalid(lock_doc(rolling=True), "api")

    def test_zip_without_member(self):
        self.assert_invalid(lock_doc(fmt="zip", member=DELETE), "member")

    def test_raw_with_member(self):
        self.assert_invalid(lock_doc(member="x.bin"), "member")

    def test_bad_mode(self):
        self.assert_invalid(lock_doc(mode="0999"), "mode")

    def test_unknown_fields_are_ignored(self):
        lock = self.load(lock_doc(future_field={"whatever": 1}))
        self.assertEqual(len(lock["assets"]), 1)
        self.assertEqual(lock["assets"][0].key, "tool")

    def test_defaults_apply(self):
        asset = self.load(lock_doc())["assets"][0]
        self.assertEqual(asset.mode, "0644")
        self.assertFalse(asset.rolling)
        self.assertEqual(asset.member, "")
        self.assertEqual(asset.api, "")
        self.assertEqual(asset.authenticode_signer, "")


class DigestAndVerifyTests(unittest.TestCase):
    def test_sha256_of_empty_is_the_known_value(self):
        self.assertEqual(supply.sha256_hex(b""), EMPTY_SHA)

    def test_size_mismatch(self):
        with self.assertRaises(supply.SupplyError) as caught:
            supply.verify_payload(PAYLOAD + b"x", make_asset())
        self.assertEqual(caught.exception.kind, "size_mismatch")
        self.assertEqual(caught.exception.detail, "tool")

    def test_digest_mismatch(self):
        asset = make_asset(digest="cd" * 32)
        with self.assertRaises(supply.SupplyError) as caught:
            supply.verify_payload(PAYLOAD, asset)
        self.assertEqual(caught.exception.kind, "digest_mismatch")
        self.assertEqual(caught.exception.detail, "tool")

    def test_correct_payload_passes_raw_and_gzip(self):
        supply.verify_payload(PAYLOAD, make_asset())
        asset, data = gzip_asset()
        supply.verify_payload(data, asset)

    def test_non_rolling_ignores_api_digest(self):
        asset = make_asset(digest="cd" * 32)
        with self.assertRaises(supply.SupplyError) as caught:
            supply.verify_payload(PAYLOAD, asset, api_digest=PAYLOAD_SHA)
        self.assertEqual(caught.exception.kind, "digest_mismatch")

    def test_rolling_accepts_different_api_digest(self):
        other = b"new upstream build of the geo database"
        asset = make_asset(key="geoip", rolling=True,
                           digest=hashlib.sha256(b"old stale build").hexdigest(),
                           size=len(other))
        supply.verify_payload(other, asset, api_digest=hashlib.sha256(other).hexdigest())

    def test_rolling_falls_back_when_api_digest_is_not_hex(self):
        asset = make_asset(rolling=True, digest=PAYLOAD_SHA)
        supply.verify_payload(PAYLOAD, asset, api_digest="not-hex-at-all")


class ReleaseDigestTests(unittest.TestCase):
    NAME = "geoip.dat"
    HEX = "9d" * 32

    def asset(self):
        return make_asset(key="geoip",
                          url="https://github.com/a/b/releases/download/latest/geoip.dat")

    def test_digest_extracted_and_lowercased(self):
        release = {"assets": [{"name": self.NAME, "digest": "sha256:" + self.HEX}]}
        self.assertEqual(supply.asset_digest_from_release(release, self.asset()), self.HEX)

    def test_uppercase_digest_is_normalized(self):
        release = {"assets": [{"name": self.NAME, "digest": "SHA256:" + self.HEX.upper()}]}
        self.assertEqual(supply.asset_digest_from_release(release, self.asset()), self.HEX)

    def test_name_mismatch(self):
        release = {"assets": [{"name": "other.dat", "digest": "sha256:" + self.HEX}]}
        self.assertEqual(supply.asset_digest_from_release(release, self.asset()), "")

    def test_missing_or_malformed_digest(self):
        for item in ({"name": self.NAME},
                     {"name": self.NAME, "digest": "md5:" + self.HEX},
                     {"name": self.NAME, "digest": "sha256:short"}, None):
            with self.subTest(item=item):
                release = {"assets": [item]}
                self.assertEqual(supply.asset_digest_from_release(release, self.asset()), "")

    def test_unusable_release_objects(self):
        asset = self.asset()
        for release in (None, [], {}, {"assets": "nope"}, 42):
            self.assertEqual(supply.asset_digest_from_release(release, asset), "")


class MirrorTests(unittest.TestCase):
    def test_github_url_is_prefixed(self):
        self.assertEqual(
            supply.member_url("https://github.com/a/b", "https://mirror.example.invalid/"),
            "https://mirror.example.invalid/a/b")

    def test_empty_mirror_returns_url_as_is(self):
        url = "https://github.com/a/b"
        self.assertEqual(supply.member_url(url, ""), url)
        self.assertEqual(supply.member_url(url), url)

    def test_non_github_url_is_untouched(self):
        url = "https://www.wintun.net/builds/wintun-0.14.1.zip"
        self.assertEqual(supply.member_url(url, "https://mirror.example.invalid"), url)


class ExtractTests(TempDirTestCase):
    def test_raw_is_passthrough(self):
        asset = make_asset()
        self.assertIs(supply.extract_member(PAYLOAD, asset), PAYLOAD)

    def test_gzip_decompresses(self):
        asset, data = gzip_asset()
        self.assertEqual(supply.extract_member(data, asset), PAYLOAD)

    def test_corrupt_gzip(self):
        asset, _ = gzip_asset()
        with self.assertRaises(supply.SupplyError) as caught:
            supply.extract_member(b"this is definitely not gzip", asset)
        self.assertEqual(caught.exception.kind, "archive_invalid")
        self.assertEqual(caught.exception.detail, "gztool")

    def test_zip_exact_member(self):
        asset, data = zip_asset({"wintun/bin/amd64/wintun.dll": PAYLOAD},
                                "wintun/bin/amd64/wintun.dll")
        self.assertEqual(supply.extract_member(data, asset), PAYLOAD)

    def test_zip_glob_member(self):
        asset, data = zip_asset({"mihomo-windows-amd64-v3-v1.19.34.exe": PAYLOAD},
                                "mihomo-windows-amd64*.exe")
        self.assertEqual(supply.extract_member(data, asset), PAYLOAD)

    def test_zip_ambiguous_glob(self):
        asset, data = zip_asset({"a/tool": b"one", "b/tool": b"two", "c/tool/": b""},
                                "*/tool")
        with self.assertRaises(supply.SupplyError) as caught:
            supply.extract_member(data, asset)
        self.assertEqual(caught.exception.kind, "member_ambiguous")

    def test_zip_missing_member(self):
        asset, data = zip_asset({"other/file": PAYLOAD}, "tool")
        with self.assertRaises(supply.SupplyError) as caught:
            supply.extract_member(data, asset)
        self.assertEqual(caught.exception.kind, "member_missing")

    def test_zip_directory_entries_never_match(self):
        asset, data = zip_asset({"a/b": PAYLOAD, "a/b/": b""}, "a/*")
        self.assertEqual(supply.extract_member(data, asset), PAYLOAD)

    def test_non_zip_data(self):
        asset, _ = zip_asset({"tool": PAYLOAD}, "tool")
        with self.assertRaises(supply.SupplyError) as caught:
            supply.extract_member(b"junk data, not a zip", asset)
        self.assertEqual(caught.exception.kind, "archive_invalid")

    def test_gzip_bomb_is_capped(self):
        asset, data = gzip_asset(payload=b"x" * 40)
        with mock.patch.object(supply, "MAX_MEMBER_BYTES", 10):
            with self.assertRaises(supply.SupplyError) as caught:
                supply.extract_member(data, asset)
        self.assertEqual(caught.exception.kind, "archive_too_large")

    def test_zip_member_is_capped(self):
        asset, data = zip_asset({"tool": b"y" * 40}, "tool")
        with mock.patch.object(supply, "MAX_MEMBER_BYTES", 10):
            with self.assertRaises(supply.SupplyError) as caught:
                supply.extract_member(data, asset)
        self.assertEqual(caught.exception.kind, "archive_too_large")


class StageAssetTests(TempDirTestCase):
    def test_stage_writes_the_extracted_bytes(self):
        asset, data = gzip_asset(mode="0755")
        target = supply.stage_asset(asset, data, self.tmp)
        self.assertEqual(target, os.path.join(self.tmp, "tool"))
        with open(target, "rb") as stream:
            self.assertEqual(stream.read(), PAYLOAD)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_mode_bits_are_applied(self):
        asset, data = gzip_asset(mode="0755")
        status = os.stat(supply.stage_asset(asset, data, self.tmp))
        self.assertEqual(status.st_mode & 0o777, 0o755)

    def test_no_temp_files_remain(self):
        asset, data = gzip_asset()
        supply.stage_asset(asset, data, self.tmp)
        self.assertEqual([name for name in os.listdir(self.tmp)
                          if name.startswith(".tmp-")], [])

    def test_digest_mismatch_leaves_nothing(self):
        asset, _ = gzip_asset(digest="cd" * 32)
        with self.assertRaises(supply.SupplyError):
            supply.stage_asset(asset, b"whatever bytes", self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_missing_staging_dir(self):
        with self.assertRaises(supply.SupplyError) as caught:
            supply.stage_asset(make_asset(), PAYLOAD, os.path.join(self.tmp, "nope"))
        self.assertEqual(caught.exception.kind, "stage_failed")


class StageAssetsTests(TempDirTestCase):
    SECOND = b"second tool bytes"
    A1 = make_asset(key="a1", install_as="one",
                    url="https://github.com/example/tool/releases/download/v1/tool-one")
    A2 = make_asset(key="a2", install_as="two",
                    url="https://github.com/example/tool/releases/download/v1/tool-two",
                    digest=hashlib.sha256(SECOND).hexdigest(), size=len(SECOND))
    PAIRS = {A1.url: PAYLOAD, A2.url: SECOND}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []

    def fetch(self, fail_on=None):
        return fake_fetch(self.PAIRS, fail_on)

    def test_two_assets_staged_and_downloads_removed(self):
        fetch = self.fetch()
        staged = supply.stage_assets([self.A1, self.A2], self.tmp, fetch=fetch,
                                     log=self.logs.append)
        self.assertEqual(sorted(staged), ["a1", "a2"])
        for asset, name in ((self.A1, "one"), (self.A2, "two")):
            with open(staged[asset.key], "rb") as stream:
                self.assertEqual(stream.read(), self.PAIRS[asset.url])
            self.assertEqual(os.path.basename(staged[asset.key]), name)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["one", "two"])
        self.assertEqual(fetch.calls, [self.A1.url, self.A2.url])

    def test_rolling_api_digest_differs_from_lock(self):
        other = b"fresh geo database from the rolling tag"
        stale = hashlib.sha256(b"stale geo database").hexdigest()
        asset = make_asset(key="geoip", rolling=True, digest=stale, size=len(other),
                           url="https://github.com/a/b/releases/download/latest/geoip.dat")
        release = {"assets": [{"name": "geoip.dat",
                               "digest": "sha256:" + hashlib.sha256(other).hexdigest()}]}
        staged = supply.stage_assets([asset], self.tmp,
                                     fetch=fake_fetch({asset.url: other}),
                                     fetch_release=lambda _url: release,
                                     log=self.logs.append)
        with open(staged["geoip"], "rb") as stream:
            self.assertEqual(stream.read(), other)
        self.assertTrue(any("lock digest stale" in line for line in self.logs))

    def test_rolling_api_failure_falls_back_to_lock(self):
        asset = make_asset(key="geoip", rolling=True, digest=PAYLOAD_SHA,
                           url="https://github.com/a/b/releases/download/latest/geoip.dat")

        def broken_release(_url):
            raise RuntimeError("api down")

        staged = supply.stage_assets([asset], self.tmp,
                                     fetch=fake_fetch({asset.url: PAYLOAD}),
                                     fetch_release=broken_release, log=self.logs.append)
        with open(staged["geoip"], "rb") as stream:
            self.assertEqual(stream.read(), PAYLOAD)
        self.assertTrue(any("api digest unavailable" in line for line in self.logs))
        self.assertFalse(any("lock digest stale" in line for line in self.logs))

    def test_second_asset_failure_deletes_the_first(self):
        broken = make_asset(key="a2", install_as="two", digest="cd" * 32,
                            size=len(self.PAIRS[self.A2.url]), url=self.A2.url)
        with self.assertRaises(supply.SupplyError) as caught:
            supply.stage_assets([self.A1, broken], self.tmp, fetch=self.fetch(),
                                log=self.logs.append)
        self.assertEqual(caught.exception.kind, "digest_mismatch")
        self.assertEqual(caught.exception.detail, "a2")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_fetch_failure_names_the_exception_class(self):
        fetch = fake_fetch({}, fail_on={self.A1.url})
        with self.assertRaises(supply.SupplyError) as caught:
            supply.stage_assets([self.A1], self.tmp, fetch=fetch, log=self.logs.append)
        self.assertEqual(caught.exception.kind, "download_failed")
        self.assertEqual(caught.exception.detail, "RuntimeError")
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertNotIn(MARKER, "\n".join(self.logs))

    def test_mirror_environment_variable_is_applied(self):
        mirrored = "https://mirror.example.invalid" + self.A1.url[len("https://github.com"):]
        with mock.patch.dict(os.environ,
                             {"MPM_GITHUB_MIRROR": "https://mirror.example.invalid"}):
            supply.stage_assets([self.A1], self.tmp, fetch=fake_fetch({mirrored: PAYLOAD}))
        self.assertEqual(os.listdir(self.tmp), ["one"])


class ErrorTextTests(TempDirTestCase):
    def test_str_format(self):
        self.assertEqual(str(supply.SupplyError("kind", "detail")), "supply kind (detail)")
        self.assertEqual(str(supply.SupplyError("kind")), "supply kind")

    def assert_clean(self, action):
        with self.assertRaises(supply.SupplyError) as caught:
            action()
        text = str(caught.exception) + " " + caught.exception.detail
        self.assertNotIn(MARKER, text)
        return caught.exception

    def test_verification_errors_never_carry_payload_text(self):
        oversized = make_asset(key="m", digest="cd" * 32, size=len(MARKER_PAYLOAD) + 1)
        self.assert_clean(lambda: supply.verify_payload(MARKER_PAYLOAD, oversized))
        misdigested = make_asset(key="m")
        self.assert_clean(lambda: supply.verify_payload(MARKER_PAYLOAD, misdigested))

    def test_archive_errors_never_carry_payload_text(self):
        gz_asset, _ = gzip_asset(payload=MARKER_PAYLOAD, key="m")
        self.assert_clean(lambda: supply.extract_member(MARKER_PAYLOAD, gz_asset))
        zip_a, zip_bytes = zip_asset({"no/match/" + MARKER: MARKER_PAYLOAD}, "tool", key="m")
        self.assert_clean(lambda: supply.extract_member(zip_bytes, zip_a))

    def test_download_and_stage_errors_never_carry_payload_text(self):
        fetch = fake_fetch({"u": MARKER_PAYLOAD}, fail_on={"u"})
        asset = make_asset(key="m")
        self.assert_clean(lambda: supply.stage_assets([asset], self.tmp, fetch=fetch))
        missing_dir = os.path.join(self.tmp, "absent")
        self.assert_clean(lambda: supply.stage_assets([asset], missing_dir,
                                                      fetch=fake_fetch({"u": MARKER_PAYLOAD})))


if __name__ == "__main__":
    unittest.main()
