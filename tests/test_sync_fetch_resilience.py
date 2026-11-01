"""Tests for fetch resilience in scripts/sync-feishu-bitable.py (phase 2A).

Covered contracts:
  * collect_source retries each path (direct, then proxy) up to 3 times with a
    10s -> 20s exponential backoff, stops a path on the first success, and logs
    `source#N attempt=k path=direct|proxy class=...`
  * a healthy direct path never touches the proxy; a definitive http_403 is not
    retried, while 429/5xx is
  * _classify_reason splits network_error into tls_error / dns_error /
    connection_reset / connection_refused / timeout (TLS handshake failures —
    SSLEOFError, schannel rc=35, EOF during handshake — must be tls_error),
    while http_fetch keeps `http_N` for non-2xx
  * source 2/3 last-known-good cache: written on success, read back as
    result="缓存" on failure with the original error preserved, and the file
    only ever holds node fields (never the subscription URL, token, or `_` keys)
  * source 1 cache fallback list + CLI flags stay intact

No network: http_fetch, build_opener and time.sleep are stubbed.
"""

import importlib.util
import os
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "scripts", "sync-feishu-bitable.py")

spec = importlib.util.spec_from_file_location("sync_feishu_bitable", SCRIPT)
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)

SOURCE_URL = "https://订阅.example.com/sub?token=SUPERSECRET"
SUB_BODY = (
    "ss://YWVzLTI1Ni1nY206c2VjcmV0@1.2.3.4:8388#nodeA\n"
    "ss://YWVzLTI1Ni1nY206c2VjcmV0@5.6.7.8:8388#nodeB\n"
)
PROXY = "http://127.0.0.1:7890"


class RecordingLog:
    """Captures log lines instead of touching the real log file."""

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("INFO", message))

    def warn(self, message):
        self.lines.append(("WARN", message))

    def error(self, message):
        self.lines.append(("ERROR", message))

    def text(self):
        return "\n".join(message for _level, message in self.lines)


def attempt_lines(log):
    """[(attempt, path, class)] parsed from the attempt log lines."""
    parsed = []
    for _level, line in log.lines:
        if " attempt=" not in line or " path=" not in line:
            continue
        tokens = line.split()
        parsed.append((
            int(tokens[1].split("=", 1)[1]),
            tokens[2].split("=", 1)[1],
            tokens[3].split("=", 1)[1],
        ))
    return parsed


class FakeFetcher:
    """Stands in for http_fetch; records (proxy) per call, replays a script."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, ua, timeout, proxy):
        self.calls.append(proxy)
        outcome = self.outcomes.pop(0) if self.outcomes else (0, "", {}, "network_error")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run_collect(fetcher, log, index=2, url=SOURCE_URL, fallback_proxy=PROXY, sleeps=None):
    """collect_source with http_fetch + time.sleep stubbed out."""
    if sleeps is None:
        sleeps = []
    patches = [
        patch.object(sync, "http_fetch", fetcher),
        patch.object(sync.time, "sleep", lambda seconds: sleeps.append(seconds)),
    ]
    for item in patches:
        item.start()
    try:
        result = sync.collect_source(index, url, "UA", 30, fallback_proxy, log)
    finally:
        for item in reversed(patches):
            item.stop()
    return result, sleeps


class RetryPolicyTests(unittest.TestCase):
    def test_defaults(self):
        import inspect

        params = inspect.signature(sync.collect_source).parameters
        self.assertEqual(params["attempts"].default, 3)
        self.assertEqual(params["backoff_base"].default, 10.0)
        self.assertEqual(sync.FETCH_MAX_ATTEMPTS, 3)
        self.assertEqual(sync.FETCH_BACKOFF_BASE_S, 10.0)
        self.assertEqual(sync.FETCH_BACKOFF_FACTOR, 2.0)

    def test_tls_failures_then_success_on_third_attempt(self):
        log = RecordingLog()
        fetcher = FakeFetcher([
            (0, "", {}, "tls_error"),
            (0, "", {}, "tls_error"),
            (200, SUB_BODY, {}, ""),
        ])
        result, sleeps = run_collect(fetcher, log, fallback_proxy=None)

        self.assertEqual(result.result, "成功")
        self.assertEqual(len(result.nodes), 2)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(sleeps, [10.0, 20.0])
        self.assertEqual(
            attempt_lines(log),
            [(1, "direct", "tls_error"), (2, "direct", "tls_error"), (3, "direct", "ok")],
        )
        # every attempt used the direct path; proxy never needed
        self.assertEqual(fetcher.calls, [None, None, None])

    def test_stops_retrying_after_success(self):
        log = RecordingLog()
        fetcher = FakeFetcher([(200, SUB_BODY, {}, "")])
        _result, sleeps = run_collect(fetcher, log, fallback_proxy=PROXY)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(attempt_lines(log), [(1, "direct", "ok")])

    def test_direct_exhausted_then_proxy_succeeds(self):
        log = RecordingLog()
        tls = (0, "", {}, "tls_error")
        fetcher = FakeFetcher([tls, tls, tls, (200, SUB_BODY, {}, "")])
        result, sleeps = run_collect(fetcher, log)

        self.assertEqual(result.result, "成功")
        self.assertEqual(fetcher.calls, [None, None, None, PROXY])
        # backoff runs inside each path: direct sleeps twice, proxy needed none
        self.assertEqual(sleeps, [10.0, 20.0])
        self.assertEqual(attempt_lines(log), [
            (1, "direct", "tls_error"), (2, "direct", "tls_error"),
            (3, "direct", "tls_error"), (1, "proxy", "ok"),
        ])
        self.assertIn("source#2 direct exhausted class=tls_error", log.text())

    def test_both_paths_exhausted_caps_at_six_attempts(self):
        log = RecordingLog()
        tls = (0, "", {}, "tls_error")
        fetcher = FakeFetcher([tls] * 6)
        result, sleeps = run_collect(fetcher, log)

        self.assertEqual(result.result, "失败")
        self.assertEqual(result.error, "tls_error")
        self.assertEqual(len(fetcher.calls), 6)
        self.assertEqual(fetcher.calls.count(None), 3)
        self.assertEqual(fetcher.calls.count(PROXY), 3)
        self.assertEqual(sleeps, [10.0, 20.0, 10.0, 20.0])
        self.assertEqual([path for _k, path, _c in attempt_lines(log)],
                         ["direct"] * 3 + ["proxy"] * 3)

    def test_http_403_is_not_retried(self):
        log = RecordingLog()
        forbidden = (403, "", {}, "http_403")
        fetcher = FakeFetcher([forbidden, forbidden])
        result, sleeps = run_collect(fetcher, log)

        self.assertEqual(result.result, "失败")
        self.assertEqual(result.error, "http_403")
        self.assertEqual(result.http_status, 403)
        self.assertEqual(sleeps, [])
        self.assertEqual(attempt_lines(log), [(1, "direct", "http_403"), (1, "proxy", "http_403")])

    def test_http_503_is_retried(self):
        log = RecordingLog()
        unavailable = (503, "", {}, "http_503")
        fetcher = FakeFetcher([unavailable, unavailable, (200, SUB_BODY, {}, "")])
        result, sleeps = run_collect(fetcher, log, fallback_proxy=None)

        self.assertEqual(result.result, "成功")
        self.assertEqual(sleeps, [10.0, 20.0])

    def test_empty_body_is_not_retried_and_reports(self):
        log = RecordingLog()
        fetcher = FakeFetcher([(200, "   ", {}, "")])
        result, _sleeps = run_collect(fetcher, log, fallback_proxy=None)
        self.assertEqual(result.result, "失败")
        self.assertEqual(result.error, "empty_body")

    def test_url_missing_does_not_fetch(self):
        log = RecordingLog()
        fetcher = FakeFetcher([])
        _result, _sleeps = run_collect(fetcher, log, url="", fallback_proxy=None)
        self.assertEqual(fetcher.calls, [])
        self.assertIn("url_missing", log.text())


class ClassificationTests(unittest.TestCase):
    def test_tls_handshake_failures_are_tls_error(self):
        cases = [
            ssl.SSLEOFError("EOF occurred in violation of protocol (_ssl.c:1129)"),
            urllib.error.URLError(
                reason=ssl.SSLEOFError("EOF occurred in violation of protocol")
            ),
            urllib.error.URLError(
                reason=ssl.SSLZeroReturnError("TLS/SSL connection has been closed (EOF)")
            ),
            ssl.SSLError(1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure"),
            ssl.SSLCertVerificationError("certificate verify failed"),
            urllib.error.URLError(
                reason=OSError("schannel: failed to receive handshake, "
                               "SSL/TLS connection established rc=35")
            ),
            OSError("[Errno -9801] schannel: security library rc=35"),
            urllib.error.URLError(reason=OSError("tunnel connection failed: "
                                                 "TLS handshake EOF during handshake")),
        ]
        for exc in cases:
            self.assertEqual(sync._classify_reason(exc), "tls_error", repr(exc))

    def test_timeout_variants(self):
        cases = [
            socket.timeout("timed out"),
            TimeoutError(),
            urllib.error.URLError(reason=socket.timeout("timed out")),
            urllib.error.URLError(reason=TimeoutError("The read operation timed out")),
        ]
        for exc in cases:
            self.assertEqual(sync._classify_reason(exc), "timeout", repr(exc))

    def test_dns_reset_and_refused(self):
        self.assertEqual(
            sync._classify_reason(urllib.error.URLError(
                reason=socket.gaierror(11001, "getaddrinfo failed"))),
            "dns_error",
        )
        self.assertEqual(
            sync._classify_reason(urllib.error.URLError(
                reason=OSError("Name or service not known"))),
            "dns_error",
        )
        self.assertEqual(
            sync._classify_reason(urllib.error.URLError(
                reason=ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接"))),
            "connection_reset",
        )
        self.assertEqual(
            sync._classify_reason(urllib.error.URLError(
                reason=OSError(10054, "connection reset by peer"))),
            "connection_reset",
        )
        self.assertEqual(
            sync._classify_reason(urllib.error.URLError(
                reason=ConnectionRefusedError(10061, "Connection refused"))),
            "connection_refused",
        )

    def test_unknown_still_network_error(self):
        self.assertEqual(
            sync._classify_reason(urllib.error.URLError(reason=OSError("banana"))),
            "network_error",
        )

    def test_http_fetch_maps_non_2xx_to_http_n(self):
        error = urllib.error.HTTPError(SOURCE_URL, 403, "Forbidden", {}, None)

        class _Opener:
            def open(self, _request, timeout=None):
                raise error

        with patch.object(sync.urllib.request, "build_opener", lambda *_h: _Opener()):
            status, text, headers, err = sync.http_fetch(SOURCE_URL, "UA", 5, None)
        self.assertEqual((status, text, headers, err), (403, "", {}, "http_403"))

    def test_http_fetch_classifies_tls_failure(self):
        error = urllib.error.URLError(
            reason=ssl.SSLEOFError("EOF occurred in violation of protocol"))

        class _Opener:
            def open(self, _request, timeout=None):
                raise error

        with patch.object(sync.urllib.request, "build_opener", lambda *_h: _Opener()):
            status, _text, _headers, err = sync.http_fetch(SOURCE_URL, "UA", 5, PROXY)
        self.assertEqual((status, err), (0, "tls_error"))


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sync-cache-test-")
        self.log = RecordingLog()

    def nodes(self):
        nodes, _stats = sync.convert_sub.convert_content(SUB_BODY, warn=lambda _m: None)
        for node in nodes:
            node["_delay"] = 123
        return nodes

    def cache_file(self, index=2):
        return sync.source_cache_path(index, self.dir)

    def test_success_writes_readable_cache(self):
        written = sync.write_source_cache(2, self.nodes(), self.log, cache_dir=self.dir)
        self.assertEqual(written, self.cache_file(2))
        self.assertTrue(self.cache_file(2).exists())
        self.assertIn("source#2 cache written nodes=2", self.log.text())

        # failure -> cache fallback keeps the original error visible
        result = sync.SourceResult(2, sync.SOURCE_NAMES[2])
        result.result = "失败"
        result.error = "tls_error"
        self.assertTrue(sync.apply_source_cache(result, [str(self.cache_file(2))], self.log))
        self.assertEqual(result.result, "缓存")
        self.assertEqual(len(result.nodes), 2)
        self.assertTrue(result.error.startswith("tls_error"))
        self.assertIn("using cache", result.error)
        self.assertIn("source#2 cache_fallback nodes=2", self.log.text())

    def test_cache_holds_only_node_fields(self):
        nodes = self.nodes()
        nodes[0]["subscription-url"] = SOURCE_URL
        nodes[0]["_internal"] = "x"
        nodes[0]["headers"] = {"Authorization": "SUPERSECRET"}
        nodes[1]["name"] = f"nodeB {SOURCE_URL}"
        written = sync.write_source_cache(
            3, nodes, self.log, cache_dir=self.dir, guard_tokens=(SOURCE_URL, "SUPERSECRET")
        )
        self.assertEqual(written, sync.source_cache_path(3, self.dir))
        content = written.read_text(encoding="utf-8")
        for forbidden in ("SUPERSECRET", "订阅.example", "token=", "sub?",
                          "_internal", "subscription-url", "Authorization", "_delay"):
            self.assertNotIn(forbidden, content)

        loaded = sync.yaml.safe_load(content)
        # nodeB lost its name to the token guard, so it is not cache-usable at all
        self.assertEqual([p["name"] for p in loaded["proxies"]], ["nodeA"])
        self.assertEqual(
            sorted(loaded["proxies"][0]),
            sorted(["cipher", "name", "password", "port", "server", "type", "udp"]),
        )

    def test_guard_tokens_include_subscription_url_and_token_value(self):
        tokens = sync._guard_tokens_for("https://sub.example.com/a?token=ABC123456&x=1")
        self.assertIn("https://sub.example.com/a?token=ABC123456&x=1", tokens)
        self.assertIn("ABC123456", tokens)
        # short values are noise, not secrets worth blocking on
        self.assertNotIn("1", tokens)
        self.assertEqual(sync._guard_tokens_for(""), ())

    def test_token_bearing_node_field_is_never_written(self):
        nodes = [{"name": f"HK-1 {SOURCE_URL}", "type": "ss", "server": "1.2.3.4",
                  "port": 8388, "password": "p"}]
        log = RecordingLog()
        written = sync.write_source_cache(
            2, nodes, log, cache_dir=self.dir, guard_tokens=sync._guard_tokens_for(SOURCE_URL)
        )
        self.assertIsNone(written)
        self.assertIn("source#2 cache write skipped reason=no_valid_nodes", log.text())
        self.assertFalse(self.cache_file(2).exists())

    def test_no_valid_nodes_skips_write(self):
        log = RecordingLog()
        self.assertIsNone(sync.write_source_cache(2, [{"_delay": 1}], log, cache_dir=self.dir))
        self.assertIn("source#2 cache write skipped reason=no_valid_nodes", log.text())
        self.assertFalse(self.cache_file(2).exists())

    def test_missing_cache_file_means_no_fallback(self):
        result = sync.SourceResult(3, sync.SOURCE_NAMES[3])
        result.error = "dns_error"
        self.assertFalse(sync.apply_source_cache(
            result, [str(sync.source_cache_path(3, self.dir))], self.log))
        self.assertEqual(result.result, "失败")
        self.assertEqual(result.error, "dns_error")

    def test_paths_default_to_live_provider_dir(self):
        self.assertEqual(
            str(sync.source_cache_path(2)),
            os.path.join(r"C:\mihomo\providers", "source2-cache.yaml"),
        )
        self.assertEqual(str(sync.source_cache_path(3)), str(
            sync.Path(sync.DEFAULT_PROVIDER_DIR) / "source3-cache.yaml"))

    def test_cli_cache_flags(self):
        args = sync.parse_args([])
        self.assertIsNone(args.source1_cache)
        self.assertIsNone(args.source2_cache)
        self.assertIsNone(args.source3_cache)
        args = sync.parse_args(["--source2-cache", "a.yaml", "--source2-cache", "b.yaml"])
        self.assertEqual(args.source2_cache, ["a.yaml", "b.yaml"])

    def test_source1_cache_candidates_untouched(self):
        """Phase 2A must not regress the existing source 1 fallback paths."""
        import inspect

        source = inspect.getsource(sync.main)
        for expected in (
            r"C:\mihomo\sub1_check.txt",
            r"C:\mihomo\providers\sub1-cache.yaml",
            r"C:\mihomo\providers\sub1_check.yaml",
            r"C:\mihomo\providers\source1.yaml",
            "args.source1_cache",
        ):
            self.assertIn(expected, source)


if __name__ == "__main__":
    unittest.main()
