import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_status_collect.py"
spec = importlib.util.spec_from_file_location("live_status_collect", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Captured before any test patches the module, so tests can exercise the real
# implementations while stub_collectors() keeps the rest hermetic.
REAL_COLLECT_PROCESS_INFO = mod.collect_process_info

FAKE_SECRET = "SUPER-SECRET-TOKEN-DO-NOT-LEAK"


def iso_minutes_ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def member(name, delay=None, minutes_ago=None, no_history=False):
    if no_history:
        return {"name": name, "type": "Trojan", "history": []}
    history = []
    if delay is not None:
        history.append({"time": iso_minutes_ago(minutes_ago or 0), "delay": delay})
    return {"name": name, "type": "Trojan", "history": history}


def fake_payload():
    members = {
        "美国1｜AI通用": member("美国1｜AI通用", delay=120, minutes_ago=1),
        "美国2｜AI通用": member("美国2｜AI通用", delay=0, minutes_ago=1),
        "日本1｜GPT": member("日本1｜GPT", delay=300, minutes_ago=60),
        "新加坡1": member("新加坡1", no_history=True),
    }
    payload = {"proxies": dict(members)}
    for group in mod.GROUPS:
        payload["proxies"][group] = {
            "type": "Fallback",
            "now": "美国1｜AI通用",
            "all": list(members.keys()),
        }
    return payload


def stub_collectors(case):
    """Patch process/task collectors so tests never touch the real system."""
    proc = mock.Mock(return_value=(
        {"mihomo_pid": 1234, "mihomo_start_time": "2026-11-05T08:00:00+08:00",
         "boot_time": "2026-11-01T07:00:00+08:00"}, []))
    tasks = mock.Mock(return_value=({name: None for name in mod.TASK_NAMES}, []))
    for target, replacement in (("collect_process_info", proc),
                                ("collect_scheduled_tasks", tasks)):
        patcher = mock.patch.object(mod, target, replacement)
        patcher.start()
        case.addCleanup(patcher.stop)
    # Nothing in a unit test may ever shell out to PowerShell.
    blocker = mock.Mock(side_effect=AssertionError("unexpected real PowerShell call"))
    ps_patcher = mock.patch.object(mod, "_run_powershell", blocker)
    ps_patcher.start()
    case.addCleanup(ps_patcher.stop)
    return proc, tasks


class HttpGetSpy:
    """Records every request; returns canned payloads. Asserts GET /proxies only."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.urls = []
        self.methods = []

    def __call__(self, url, secret=None, timeout=5):
        self.urls.append(url)
        self.methods.append("GET")
        if self.error is not None:
            raise self.error
        path = urllib.parse.urlsplit(url).path
        if path == "/proxies":
            return self.payload
        name = urllib.parse.unquote(path[len("/proxies/"):])
        proxies = (self.payload or {}).get("proxies", {})
        if name in proxies:
            return proxies[name]
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config = self.root / "config.yaml"
        self.config.write_text(
            f"external-controller: 127.0.0.1:9090\nsecret: '{FAKE_SECRET}'\n",
            encoding="utf-8")
        stub_collectors(self)

    def collect(self, http_get, **kwargs):
        return mod.collect(api_base="http://127.0.0.1:9090", config_path=str(self.config),
                           state_dir=str(self.state_dir), http_get=http_get, **kwargs)

    # --- normal path -------------------------------------------------------

    def test_normal_collection(self):
        spy = HttpGetSpy(payload=fake_payload())
        result = self.collect(spy)
        self.assertTrue(result["ok"])
        self.assertTrue(result["api"]["reachable"])
        self.assertEqual(result["schema_version"], 1)
        for group in mod.GROUPS:
            g = result["groups"][group]
            self.assertEqual(g["type"], "Fallback")
            self.assertEqual(g["now"], "美国1｜AI通用")
            self.assertEqual(g["member_count"], 4)
            self.assertEqual(g["members"], ["美国1｜AI通用", "美国2｜AI通用", "日本1｜GPT", "新加坡1"])
        gpt = result["groups"]["GPT"]
        by_name = {m["name"]: m for m in gpt["member_status"]}
        self.assertTrue(by_name["美国1｜AI通用"]["usable_by_history"])
        self.assertFalse(by_name["美国1｜AI通用"]["stale"])
        # fresh but delay == 0 -> unusable
        self.assertEqual(by_name["美国2｜AI通用"]["last_delay_ms"], 0)
        self.assertFalse(by_name["美国2｜AI通用"]["usable_by_history"])
        # delay > 0 but 60 min old -> stale, unusable
        self.assertTrue(by_name["日本1｜GPT"]["stale"])
        self.assertFalse(by_name["日本1｜GPT"]["usable_by_history"])
        # no history at all -> null delay, stale
        self.assertIsNone(by_name["新加坡1"]["last_delay_ms"])
        self.assertIsNone(by_name["新加坡1"]["last_time"])
        self.assertTrue(by_name["新加坡1"]["stale"])
        self.assertEqual(gpt["usable_count_by_history"], 1)
        self.assertIn("history", result["usable_count_note"])
        self.assertIn("utc", result["collected_at"])
        self.assertIn("beijing", result["collected_at"])

    def test_missing_local_files_do_not_crash(self):
        spy = HttpGetSpy(payload=fake_payload())
        result = self.collect(spy)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["state_files"]["ippool"])
        self.assertIsNone(result["state_files"]["feishu_gpt_history"])
        self.assertIsNone(result["state_files"]["feishu_bitable_sync_state"])
        joined = "\n".join(result["errors"])
        self.assertIn("ippool.json not found", joined)
        self.assertIn("feishu-gpt-history.json not found", joined)
        self.assertIn("feishu-bitable-sync-state.json not found", joined)

    def test_state_files_parsed_when_present(self):
        (self.state_dir / "xigua-ippool").mkdir()
        (self.state_dir / "xigua-ippool" / "ippool.json").write_text(json.dumps({
            "schema": 2, "updated_at": "2026-11-05T09:17:25+00:00",
            "hosts": {
                "h1": {"ips": {"1.1.1.1": {"healthy": True}, "2.2.2.2": {"healthy": False}}},
                "h2": {"ips": {"3.3.3.3": {"healthy": True}}},
            }}), encoding="utf-8")
        (self.root / "feishu-gpt-history.json").write_text(
            json.dumps({"version": 1, "nodes": {"a": {}, "b": {}}}), encoding="utf-8")
        (self.state_dir / "feishu-bitable-sync-state.json").write_text(
            json.dumps({"last_result": "ok", "last_success_time": "x", "extra": 1}),
            encoding="utf-8")
        spy = HttpGetSpy(payload=fake_payload())
        result = self.collect(spy)
        ippool = result["state_files"]["ippool"]
        self.assertEqual(ippool["host_count"], 2)
        self.assertEqual(ippool["healthy_ip_count"], 2)
        self.assertEqual(ippool["ip_total"], 3)
        self.assertEqual(ippool["updated_at"], "2026-11-05T09:17:25+00:00")
        gpt_hist = result["state_files"]["feishu_gpt_history"]
        self.assertEqual(gpt_hist["version"], 1)
        self.assertEqual(gpt_hist["node_count"], 2)
        sync_state = result["state_files"]["feishu_bitable_sync_state"]
        self.assertEqual(sync_state["summary"]["last_result"], "ok")
        self.assertNotIn("extra", sync_state["summary"])

    # --- API down ----------------------------------------------------------

    def test_api_unreachable(self):
        spy = HttpGetSpy(error=urllib.error.URLError("connection refused"))
        result = self.collect(spy)
        self.assertFalse(result["ok"])
        self.assertFalse(result["api"]["reachable"])
        self.assertTrue(result["api"]["errors"])
        for group in mod.GROUPS:
            self.assertIsNone(result["groups"][group])
        # local sections still produced
        self.assertIn("process", result)
        self.assertIn("state_files", result)
        self.assertIn("scheduled_tasks", result)
        joined = "\n".join(result["errors"])
        self.assertIn("URLError", joined)
        self.assertNotIn(FAKE_SECRET, joined)

    # --- secret hygiene ----------------------------------------------------

    def test_secret_never_leaks(self):
        spy = HttpGetSpy(payload=fake_payload())
        result = self.collect(spy)
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(FAKE_SECRET, blob)
        # also on the error path
        spy_err = HttpGetSpy(error=RuntimeError(FAKE_SECRET))
        result_err = self.collect(spy_err)
        blob_err = json.dumps(result_err, ensure_ascii=False)
        self.assertNotIn(FAKE_SECRET, blob_err)
        self.assertIn("RuntimeError", "\n".join(result_err["errors"]))

    def test_secret_read_from_config_and_regex_fallback(self):
        self.assertEqual(mod._read_secret_from_config(str(self.config)), FAKE_SECRET)
        broken = self.root / "broken.yaml"
        broken.write_text("\tsecret: 'x'\nnot: [valid: yaml\n", encoding="utf-8")
        # must not raise, must not echo content
        mod._read_secret_from_config(str(broken))
        missing = self.root / "missing.yaml"
        self.assertIsNone(mod._read_secret_from_config(str(missing)))

    # --- read-only enforcement ---------------------------------------------

    def test_never_calls_delay_or_switch_endpoints(self):
        spy = HttpGetSpy(payload=fake_payload())
        self.collect(spy)
        self.assertTrue(spy.urls, "expected at least one request")
        for url in spy.urls:
            self.assertTrue(url.startswith("http://127.0.0.1:9090/proxies"), url)
            self.assertNotIn("delay", url)
        for method in spy.methods:
            self.assertEqual(method, "GET")

    def test_http_get_json_rejects_non_proxies_paths(self):
        for bad in ("http://127.0.0.1:9090/proxies/GPT/delay",
                    "http://127.0.0.1:9090/group/GPT/delay",
                    "http://127.0.0.1:9090/configs",
                    "http://127.0.0.1:9090/providers",
                    "http://127.0.0.1:9090/proxies/GPT/healthcheck"):
            with self.assertRaises(ValueError):
                mod.http_get_json(bad)
        for good in ("/proxies", "/proxies/%E6%97%A5%E6%9C%AC1%EF%BD%9CGPT"):
            self.assertTrue(mod._is_allowed_path(good), good)

    def test_member_refetch_uses_quoted_path(self):
        payload = fake_payload()
        del payload["proxies"]["日本1｜GPT"]  # force per-member refetch
        spy = HttpGetSpy(payload=payload)
        result = self.collect(spy)
        refetch = [u for u in spy.urls if u != "http://127.0.0.1:9090/proxies"]
        self.assertEqual(len(refetch), 4)  # once per group containing the member
        for url in refetch:
            self.assertIn("/proxies/%E6%97%A5%E6%9C%AC1%EF%BD%9CGPT", url)
            self.assertNotIn("日本1", url)
            # the read-only guard must accept the URL the collector actually builds
            self.assertTrue(mod._is_allowed_path(urllib.parse.urlsplit(url).path), url)
        by_name = {m["name"]: m for m in result["groups"]["GPT"]["member_status"]}
        self.assertTrue(by_name["日本1｜GPT"]["stale"])

    # --- freshness window ---------------------------------------------------

    def test_freshness_window_configurable(self):
        spy = HttpGetSpy(payload=fake_payload())
        result = self.collect(spy, freshness_minutes=120)
        by_name = {m["name"]: m for m in result["groups"]["GPT"]["member_status"]}
        self.assertFalse(by_name["日本1｜GPT"]["stale"])
        self.assertTrue(by_name["日本1｜GPT"]["usable_by_history"])
        self.assertEqual(result["groups"]["GPT"]["usable_count_by_history"], 2)
        self.assertEqual(result["freshness_window_minutes"], 120)

    # --- group missing -------------------------------------------------------

    def test_group_missing_from_payload(self):
        payload = fake_payload()
        del payload["proxies"]["GROKAPI"]
        spy = HttpGetSpy(payload=payload)
        result = self.collect(spy)
        self.assertIsNone(result["groups"]["GROKAPI"])
        self.assertIn("groups.GROKAPI", "\n".join(result["errors"]))
        self.assertIsNotNone(result["groups"]["GPT"])


    def test_sanitize_error_drops_messages_but_keeps_powershell_detail(self):
        self.assertEqual(mod._sanitize_error(RuntimeError(FAKE_SECRET)), "RuntimeError")
        self.assertEqual(mod._sanitize_error(ValueError(FAKE_SECRET)), "ValueError")
        self.assertEqual(mod._sanitize_error(OSError(FAKE_SECRET)), "OSError")
        self.assertEqual(
            mod._sanitize_error(mod._PowerShellError(f"powershell exited 1: {FAKE_SECRET}")),
            f"powershell exited 1: {FAKE_SECRET}")
        self.assertEqual(mod._sanitize_error(
            urllib.error.HTTPError("http://x/proxies", 500, "boom", {}, None)), "HTTPError 500")

    def test_process_fallback_is_read_only(self):
        captured = []

        def spy(command, timeout=20):
            captured.append(command)
            return ""

        with mock.patch.object(mod, "_run_powershell", spy), \
             mock.patch.object(mod, "collect_process_info", REAL_COLLECT_PROCESS_INFO), \
             mock.patch.dict(sys.modules, {"psutil": None}):
            info, errors = mod.collect_process_info()
        self.assertIsNone(info["mihomo_pid"])
        joined = "\n".join(captured)
        self.assertIn("Get-Process", joined)
        self.assertNotIn("Stop-Process", joined)
        self.assertNotIn("Start-Process", joined)
        self.assertIsNone(info["mihomo_start_time"])
        self.assertIsNone(info["boot_time"])
        self.assertIsInstance(errors, list)


class ScheduledTaskTests(unittest.TestCase):
    def test_parses_powershell_json(self):
        payload = [
            {"name": "MihomoFeishuBitableSync", "exists": True, "state": "Running",
             "last_run_time": "2026-11-05T17:37:01.0000000+08:00",
             "last_task_result": 267009,
             "next_run_time": "2026-11-05T18:07:00.0000000+08:00"},
            {"name": "MihomoXiguaIpPool", "exists": False, "state": None,
             "last_run_time": None, "last_task_result": None, "next_run_time": None},
            {"name": "MihomoNinjaGrokFeishuSync", "exists": True, "state": "Ready",
             "last_run_time": "2026-11-05T17:48:25.0000000+08:00",
             "last_task_result": 1, "next_run_time": None},
        ]
        with mock.patch.object(mod, "_run_powershell", return_value=json.dumps(payload)):
            tasks, errors = mod.collect_scheduled_tasks()
        self.assertEqual(tasks["MihomoFeishuBitableSync"]["state"], "Running")
        self.assertEqual(tasks["MihomoFeishuBitableSync"]["last_task_result"], 267009)
        self.assertIsNone(tasks["MihomoXiguaIpPool"])
        self.assertIsNone(tasks["MihomoNinjaGrokFeishuSync"]["next_run_time"])
        self.assertIn("scheduled_tasks.MihomoXiguaIpPool", "\n".join(errors))

    def test_single_object_payload_is_normalized(self):
        payload = {"name": "MihomoXiguaIpPool", "exists": True, "state": "Ready",
                   "last_run_time": None, "last_task_result": 0, "next_run_time": None}
        with mock.patch.object(mod, "_run_powershell", return_value=json.dumps(payload)):
            tasks, errors = mod.collect_scheduled_tasks(["MihomoXiguaIpPool"])
        self.assertEqual(tasks["MihomoXiguaIpPool"]["state"], "Ready")
        self.assertEqual(errors, [])

    def test_powershell_failure_yields_nulls_and_error(self):
        with mock.patch.object(mod, "_run_powershell",
                               side_effect=mod._PowerShellError("powershell exited 1: boom")):
            tasks, errors = mod.collect_scheduled_tasks()
        self.assertEqual(tasks, {name: None for name in mod.TASK_NAMES})
        self.assertIn("boom", "\n".join(errors))

    def test_generated_powershell_is_balanced_and_quoted(self):
        captured = {}

        def spy(command, timeout=20):
            captured["cmd"] = command
            return "[]"

        with mock.patch.object(mod, "_run_powershell", spy):
            mod.collect_scheduled_tasks()
        cmd = captured["cmd"]
        self.assertEqual(cmd.count("{"), cmd.count("}"))
        self.assertEqual(cmd.count("("), cmd.count(")"))
        for name in mod.TASK_NAMES:
            self.assertIn(f"'{name}'", cmd)
        # read-only cmdlets only
        self.assertIn("Get-ScheduledTask", cmd)
        self.assertNotIn("Start-ScheduledTask", cmd)
        self.assertNotIn("Stop-ScheduledTask", cmd)
        self.assertNotIn("Set-ScheduledTask", cmd)
        self.assertNotIn("Register-ScheduledTask", cmd)


class MainTests(unittest.TestCase):
    def setUp(self):
        stub_collectors(self)

    def test_main_json_stdout_and_exit_zero_when_api_down(self):
        spy = HttpGetSpy(error=urllib.error.URLError("down"))
        with mock.patch.object(mod, "http_get_json", spy), \
             mock.patch.object(sys, "stdout", new=io.StringIO()) as out:
            code = mod.main(["--json", "--config", "nonexistent.yaml",
                             "--state-dir", "nonexistent-dir"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])

    def test_main_out_file(self):
        spy = HttpGetSpy(payload=fake_payload())
        with tempfile.TemporaryDirectory() as temp:
            out_path = Path(temp) / "status.json"
            with mock.patch.object(mod, "http_get_json", spy):
                code = mod.main(["--out", str(out_path), "--config", "nonexistent.yaml",
                                 "--state-dir", "nonexistent-dir"])
            self.assertEqual(code, 0)
            result = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
