"""deploy.fetch: atomic HTTPS download and release JSON, all offline."""

import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import fetch  # noqa: E402

URL = "https://github.com/example/tool/releases/download/v1/tool-v1"
DEST = "tool.bin"


class FakeResponse:
    """Stand-in for ``urllib.request.urlopen``'s return value."""

    def __init__(self, chunks, *, status=200, final_url=None, fail_on_read=0):
        self.status = status
        self._chunks = list(chunks)
        self._final_url = final_url if final_url is not None else URL
        self._fail_on_read = fail_on_read
        self._reads = 0

    def read(self, _size):
        self._reads += 1
        if self._fail_on_read and self._reads > self._fail_on_read:
            raise OSError("connection reset mid-body")
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def opener_of(response, calls=None):
    def opener(url, timeout):
        if calls is not None:
            calls.append((url, timeout))
        return response

    return opener


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fetch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dest = os.path.join(self.tmp, DEST)

    def temp_names(self):
        return os.listdir(self.tmp)


class FetchToFileTests(TempDirTestCase):
    def test_success_writes_every_chunk_atomically(self):
        response = FakeResponse([b"hello ", b"world"])
        result = fetch.fetch_to_file(URL, self.dest, opener=opener_of(response))
        self.assertEqual(result, self.dest)
        with open(self.dest, "rb") as stream:
            self.assertEqual(stream.read(), b"hello world")
        self.assertEqual(self.temp_names(), [DEST])  # no .tmp-* left behind

    def test_http_404_leaves_nothing(self):
        response = FakeResponse([], status=404)
        with self.assertRaises(ValueError) as caught:
            fetch.fetch_to_file(URL, self.dest, opener=opener_of(response))
        self.assertIn("http_404", str(caught.exception))
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.temp_names(), [])

    def test_http_500_is_an_error_too(self):
        response = FakeResponse([], status=500)
        with self.assertRaises(ValueError) as caught:
            fetch.fetch_to_file(URL, self.dest, opener=opener_of(response))
        self.assertIn("http_500", str(caught.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_opener_exceptions_propagate_unchanged(self):
        for exc in (urllib.error.URLError("dns down"), OSError("unreachable")):
            with self.subTest(exc=type(exc).__name__):

                def broken(_url, _timeout, _exc=exc):
                    raise _exc

                with self.assertRaises(type(exc)):
                    fetch.fetch_to_file(URL, self.dest, opener=broken)
                self.assertFalse(os.path.exists(self.dest))
                self.assertEqual(self.temp_names(), [])

    def test_https_downgrade_redirect_is_refused(self):
        response = FakeResponse([b"evil"], final_url="http://evil.example/tool")
        with self.assertRaises(ValueError) as caught:
            fetch.fetch_to_file(URL, self.dest, opener=opener_of(response))
        self.assertIn("https_downgrade", str(caught.exception))
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.temp_names(), [])

    def test_read_failure_mid_body_leaves_nothing(self):
        response = FakeResponse([b"first", b"second"], fail_on_read=1)
        with self.assertRaises(OSError):
            fetch.fetch_to_file(URL, self.dest, opener=opener_of(response))
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.temp_names(), [])

    def test_existing_file_survives_a_failed_download(self):
        with open(self.dest, "wb") as stream:
            stream.write(b"previous good bytes")
        response = FakeResponse([], status=404)
        with self.assertRaises(ValueError):
            fetch.fetch_to_file(URL, self.dest, opener=opener_of(response))
        with open(self.dest, "rb") as stream:
            self.assertEqual(stream.read(), b"previous good bytes")

    def test_existing_file_is_replaced_on_success(self):
        with open(self.dest, "wb") as stream:
            stream.write(b"stale")
        response = FakeResponse([b"fresh bytes"])
        fetch.fetch_to_file(URL, self.dest, opener=opener_of(response))
        with open(self.dest, "rb") as stream:
            self.assertEqual(stream.read(), b"fresh bytes")

    def test_missing_parent_directory_raises_oserror(self):
        target = os.path.join(self.tmp, "nope", DEST)
        response = FakeResponse([b"data"])
        with self.assertRaises(OSError):
            fetch.fetch_to_file(URL, target, opener=opener_of(response))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope")))


class FetchJsonTests(TempDirTestCase):
    def test_a_json_object_is_returned(self):
        response = FakeResponse([b'{"assets": [1], "tail": ', b'"x"}'])
        data = fetch.fetch_json(URL, opener=opener_of(response))
        self.assertEqual(data, {"assets": [1], "tail": "x"})

    def test_top_level_array_is_none(self):
        response = FakeResponse([b"[1, 2]"])
        self.assertIsNone(fetch.fetch_json(URL, opener=opener_of(response)))

    def test_invalid_json_is_none(self):
        response = FakeResponse([b"{nope"])
        self.assertIsNone(fetch.fetch_json(URL, opener=opener_of(response)))

    def test_http_403_is_none(self):
        response = FakeResponse([], status=403)
        self.assertIsNone(fetch.fetch_json(URL, opener=opener_of(response)))

    def test_opener_failure_is_none(self):
        def broken(_url, _timeout):
            raise urllib.error.URLError("down")

        self.assertIsNone(fetch.fetch_json(URL, opener=broken))

    def test_over_4mib_is_none(self):
        big = json.dumps({"blob": "x" * (fetch.MAX_JSON_BYTES + 10)}).encode()
        response = FakeResponse([big[:1024], big[1024:]])
        self.assertIsNone(fetch.fetch_json(URL, opener=opener_of(response)))

    def test_exactly_4mib_still_parses(self):
        payload = {"blob": "x" * (fetch.MAX_JSON_BYTES - 20)}
        body = json.dumps(payload).encode()
        self.assertLessEqual(len(body), fetch.MAX_JSON_BYTES)
        response = FakeResponse([body])
        self.assertEqual(fetch.fetch_json(URL, opener=opener_of(response)), payload)


class DefaultOpenerTests(unittest.TestCase):
    def test_user_agent_and_timeout_are_passed(self):
        calls = {}

        def fake_urllib_urlopen(request, timeout=None):
            calls["request"] = request
            calls["timeout"] = timeout
            return mock.MagicMock()

        with mock.patch("urllib.request.urlopen", fake_urllib_urlopen):
            response = fetch._default_opener(URL, timeout=7)
        request = calls["request"]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(request.get_header("User-agent"), fetch.USER_AGENT)
        self.assertEqual(request.full_url, URL)
        self.assertEqual(calls["timeout"], 7)
        # The response is a context manager like the real one.
        self.assertTrue(hasattr(response, "__enter__"))


if __name__ == "__main__":
    unittest.main()
