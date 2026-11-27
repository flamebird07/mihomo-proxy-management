"""deploy.feishu_url: the Bitable address a user pastes (pure, no network)."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import feishu_url  # noqa: E402

TOKEN = "fakeBaseToken1234567890abcd"
WIKI = "fakeWikiToken1234567890xyz"
HOST = "example.feishu.cn"
BASE_URL = f"https://{HOST}/base/{TOKEN}"
WIKI_URL = f"https://{HOST}/wiki/{WIKI}"

#: (input, expected FeishuUrlError.kind) - none of these carries a usable address.
BAD_ADDRESSES = (
    ("", "empty"),
    ("   \n\t ", "empty"),
    ("not a url", "not_a_url"),
    ("example.feishu.cn/base/" + TOKEN, "not_a_url"),      # no scheme
    ("ftp://" + HOST + "/base/" + TOKEN, "unsupported_scheme"),
    ("https://example.com/base/" + TOKEN, "not_feishu_host"),
    ("https://notfeishu.cn/base/" + TOKEN, "not_feishu_host"),
    ("https://" + HOST + "/", "unknown_path"),
    ("https://" + HOST + "/sheets/abc", "unknown_path"),
    ("https://" + HOST + "/BASE/" + TOKEN, "unknown_path"),  # path is case-sensitive
    ("https://" + HOST + "/base/to", "bad_token"),
    ("https://" + HOST + "/base/<script>alert(1)</script>", "bad_token"),
    ("https://user:pw@" + HOST + "/base/" + TOKEN, "has_userinfo"),
)


class ParseTests(unittest.TestCase):
    def test_base_url_with_table_and_view(self):
        parsed = feishu_url.parse_bitable_url(BASE_URL + "?table=tblFake123&view=vewFake456")
        self.assertEqual(parsed.kind, "base")
        self.assertEqual(parsed.token, TOKEN)
        self.assertEqual(parsed.host, HOST)
        self.assertEqual(parsed.table_id, "tblFake123")
        self.assertEqual(parsed.scheme, "https")

    def test_extra_path_segments_are_ignored_and_table_comes_from_the_query(self):
        parsed = feishu_url.parse_bitable_url(f"{BASE_URL}/table/tblFromPath456")
        self.assertEqual((parsed.kind, parsed.token), ("base", TOKEN))
        self.assertEqual(parsed.table_id, "")

    def test_wiki_url_keeps_the_node_token(self):
        parsed = feishu_url.parse_bitable_url(WIKI_URL + "?from=space")
        self.assertEqual((parsed.kind, parsed.token), ("wiki", WIKI))
        self.assertEqual(parsed.host, HOST)

    def test_bare_token(self):
        parsed = feishu_url.parse_bitable_url(TOKEN)
        self.assertEqual((parsed.kind, parsed.token, parsed.host), ("token", TOKEN, ""))
        self.assertEqual(parsed.masked(), feishu_url.mask_token(TOKEN))

    def test_scheme_and_host_are_lowercased_path_is_not(self):
        parsed = feishu_url.parse_bitable_url(f"HTTP://EXAMPLE.Feishu.CN/base/{TOKEN}")
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.host, HOST)

    def test_surrounding_whitespace_is_stripped(self):
        parsed = feishu_url.parse_bitable_url(f"\n  {BASE_URL}  \t")
        self.assertEqual(parsed.token, TOKEN)

    def test_invalid_addresses_report_their_kind(self):
        for raw, kind in BAD_ADDRESSES:
            with self.subTest(kind=kind):
                with self.assertRaises(feishu_url.FeishuUrlError) as caught:
                    feishu_url.parse_bitable_url(raw)
                self.assertEqual(caught.exception.kind, kind)

    def test_malformed_netloc_is_not_a_url(self):
        with self.assertRaises(feishu_url.FeishuUrlError):
            feishu_url.parse_bitable_url("https://[::1/base/" + TOKEN)

    def test_errors_never_echo_the_token_or_the_input(self):
        secrets_in_input = (TOKEN, "user:pw", "<script>alert(1)</script>")
        for raw, _kind in BAD_ADDRESSES:
            with self.subTest(input=raw[:24]):
                try:
                    feishu_url.parse_bitable_url(raw)
                except feishu_url.FeishuUrlError as exc:
                    text = str(exc)
                    for secret in secrets_in_input:
                        self.assertNotIn(secret, text)
                else:
                    self.fail(f"{raw!r} was accepted")

    def test_unusable_table_query_is_ignored(self):
        parsed = feishu_url.parse_bitable_url(BASE_URL + "?table=tbl%3Cbad%3E&view=x")
        self.assertEqual(parsed.table_id, "")
        parsed = feishu_url.parse_bitable_url(BASE_URL + "?table=tblOk")
        self.assertEqual(parsed.table_id, "tblOk")


class MaskTests(unittest.TestCase):
    def test_short_tokens_vanish(self):
        for token in ("", "short", "123456789"):
            self.assertEqual(feishu_url.mask_token(token), "<redacted>")

    def test_long_token_keeps_both_ends_only(self):
        token = "fakeBaseToken1234567890abcd"
        masked = feishu_url.mask_token(token)
        self.assertEqual(masked, "fake…bcd")
        self.assertEqual(masked.count("…"), 1)
        self.assertNotIn(token[4:-3], masked)

    def test_masked_address_hides_the_token(self):
        for url in (BASE_URL, WIKI_URL):
            masked = feishu_url.parse_bitable_url(url).masked()
            self.assertNotIn(TOKEN, masked)
            self.assertNotIn(WIKI, masked)
            self.assertIn(feishu_url.mask_token(TOKEN if url is BASE_URL else WIKI), masked)
            self.assertTrue(masked.startswith(f"https://{HOST}/"))


if __name__ == "__main__":
    unittest.main()
