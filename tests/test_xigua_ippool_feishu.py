import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-xigua-ippool-feishu.py"
spec = importlib.util.spec_from_file_location("sync_xigua_ippool_feishu", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def windows_only(func):
    """Tests that really launch powershell.exe/cmd.exe; pure parsing stays."""
    func = pytest.mark.windows_only(func)
    return pytest.mark.skipif(
        sys.platform != "win32", reason="windows_only: real powershell.exe/cmd.exe execution"
    )(func)


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, message): self.lines.append(("INFO", message))
    def warn(self, message): self.lines.append(("WARN", message))
    def error(self, message): self.lines.append(("ERROR", message))


class FakeClient:
    def __init__(self, fail_batch=False, bad_readback=False):
        self.table_id = "tbl-xigua"
        self.tables = []
        self.fields = []
        self.records = [{"record_id": "old-1", "fields": {"唯一键": "old|1.1.1.1"}}]
        self.batch_creates = 0
        self.batch_deletes = 0
        self.next_id = 1
        self.fail_batch = fail_batch
        self.bad_readback = bad_readback

    def list_tables(self): return list(self.tables)

    def create_table(self, name, fields):
        self.tables = [{"name": name, "table_id": self.table_id}]
        self.fields = [{"field_name": f[0], "field_id": f"f-{i}", "type": f[1],
                        "property": f[2] or {}} for i, f in enumerate(fields)]
        return self.table_id

    def list_fields(self, table_id): return list(self.fields)
    def create_field(self, table_id, name, ftype, prop):
        self.fields.append({"field_name": name, "field_id": "new", "type": ftype, "property": prop or {}})
    def update_field(self, *args): pass
    def delete_field(self, *args): pass

    def list_records(self, table_id):
        if self.bad_readback and self.batch_creates:
            return []
        return list(self.records)

    def batch_create(self, table_id, records):
        if self.fail_batch:
            raise sync.base.SyncError("api_error", "synthetic_batch_failure")
        self.batch_creates += 1
        created = []
        for record in records:
            created.append({"record_id": f"new-{self.next_id}", "fields": record["fields"]})
            self.next_id += 1
        self.records.extend(created)
        return len(records)

    def batch_delete(self, table_id, ids):
        self.batch_deletes += 1
        self.records = [r for r in self.records if r.get("record_id") not in set(ids)]
        return len(ids)


def state_document():
    updated = "2026-11-04T00:00:00+00:00"
    return {
        "schema": 2, "updated_at": updated,
        "hosts": {
            "a.indxg.com": {"zero_healthy_rounds": 0, "ips": {
                "1.1.1.1": {"first_seen": "2026-11-03T23:00:00Z", "last_seen": updated,
                            "success_total": 4, "failure_total": 1, "success_streak": 2,
                            "failure_streak": 0, "healthy": True, "latency_ms": 123.4,
                            "last_result": "ok"}}},
            "b.indxg.com": {"zero_healthy_rounds": 1, "ips": {
                "8.8.8.8": {"first_seen": "bad-date", "last_seen": "2026-11-03T22:00:00Z",
                            "success_total": 1, "failure_total": 3, "success_streak": 0,
                            "failure_streak": 3, "healthy": False, "latency_ms": None,
                            "last_result": "timeout"}}},
        },
    }


class XiguaFeishuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "ippool.json"
        self.provider = self.root / "final.yaml"
        self.cursor = self.root / "cursor.json"
        self.state.write_text(json.dumps(state_document(), ensure_ascii=False), encoding="utf-8")
        self.provider.write_text(yaml.safe_dump({"proxies": [{
            "name": "西瓜-A@a.indxg.com@1.1.1.1", "server": "1.1.1.1",
            "password": "SYNTHETIC-PASSWORD", "sni": "secret-sni.example",
        }]}, allow_unicode=True), encoding="utf-8")
        self.log = FakeLog()

    def tearDown(self): self.temp.cleanup()

    def test_schema_v2_rows_dates_current_and_final_mapping(self):
        state = sync.load_state(self.state)
        mapping = sync.final_mapping(self.provider, state)
        records = sync.build_records(state, mapping)
        self.assertEqual(len(records), 2)
        rows = {r["fields"]["唯一键"]: r["fields"] for r in records}
        a = rows["a.indxg.com|1.1.1.1"]
        b = rows["b.indxg.com|8.8.8.8"]
        self.assertTrue(a["本轮发现"])
        self.assertTrue(a["入口健康"])
        self.assertTrue(a["最终可用"])
        self.assertEqual(a["最终节点名"], "西瓜-A@a.indxg.com@1.1.1.1")
        self.assertIsInstance(a["首次发现"], int)
        self.assertFalse(b["本轮发现"])
        self.assertFalse(b["最终可用"])
        self.assertNotIn("首次发现", b)
        self.assertEqual(b["连续失败"], 3)

    def test_hosts_do_not_cross_even_when_provider_ip_is_ambiguous(self):
        data = state_document()
        data["hosts"]["b.indxg.com"]["ips"]["1.1.1.1"] = data["hosts"]["b.indxg.com"]["ips"].pop("8.8.8.8")
        self.state.write_text(json.dumps(data), encoding="utf-8")
        state = sync.load_state(self.state)
        mapping = sync.final_mapping(self.provider, state)
        self.assertIn(("a.indxg.com", "1.1.1.1"), mapping)
        self.assertNotIn(("b.indxg.com", "1.1.1.1"), mapping)

    def test_sensitive_provider_fields_never_enter_rows_or_log(self):
        state = sync.load_state(self.state)
        records = sync.build_records(state, sync.final_mapping(self.provider, state))
        text = json.dumps(records, ensure_ascii=False) + json.dumps(self.log.lines, ensure_ascii=False)
        self.assertNotIn("SYNTHETIC-PASSWORD", text)
        self.assertNotIn("secret-sni.example", text)

    def test_bad_or_empty_state_refuses_before_table_write(self):
        client = FakeClient()
        for payload in ({"schema": 1, "hosts": {}}, {"schema": 2, "hosts": {}},
                        {"schema": 2, "hosts": {"a": {"ips": []}}}):
            self.state.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(sync.base.SyncError):
                sync.sync_snapshot(client, self.state, self.provider, self.cursor, self.log)
        self.assertEqual(client.batch_creates, 0)
        self.assertEqual(client.batch_deletes, 0)

    def test_changed_writes_and_readback_then_unchanged_skips(self):
        client = FakeClient()
        self.assertEqual(sync.sync_snapshot(client, self.state, self.provider, self.cursor, self.log), "written")
        self.assertEqual(client.batch_creates, 1)
        cursor_before = json.loads(self.cursor.read_text(encoding="utf-8"))
        self.assertEqual(cursor_before["row_count"], 2)
        self.assertEqual(sync.sync_snapshot(client, self.state, self.provider, self.cursor, self.log), "unchanged")
        self.assertEqual(client.batch_creates, 1)
        self.assertTrue(any("unchanged" in line for _level, line in self.log.lines))

    def test_provider_change_triggers_write(self):
        client = FakeClient()
        sync.sync_snapshot(client, self.state, self.provider, self.cursor, self.log)
        self.provider.write_text("proxies: []\n", encoding="utf-8")
        sync.sync_snapshot(client, self.state, self.provider, self.cursor, self.log)
        self.assertEqual(client.batch_creates, 2)

    def test_readback_mismatch_fails_and_cursor_not_updated(self):
        client = FakeClient(bad_readback=True)
        with self.assertRaisesRegex(sync.base.SyncError, "write_verify_failed"):
            sync.sync_snapshot(client, self.state, self.provider, self.cursor, self.log)
        self.assertFalse(self.cursor.exists())

    def test_batch_failure_does_not_update_cursor(self):
        client = FakeClient(fail_batch=True)
        with self.assertRaises(sync.base.SyncError):
            sync.sync_snapshot(client, self.state, self.provider, self.cursor, self.log)
        self.assertFalse(self.cursor.exists())

    def test_lock_conflict_returns_lock_busy_without_client(self):
        class BusyLock:
            def __init__(self, *args, **kwargs): pass
            def acquire(self): raise sync.base.SyncError("lock_busy", "synthetic")

        with mock.patch.object(sync.base, "SyncLock", BusyLock), \
             mock.patch.object(sync.base, "release_sync_lock"), \
             mock.patch.object(sync.base, "FeishuClient") as client:
            code = sync.main(["--ippool-state", str(self.state), "--final-provider", str(self.provider),
                              "--cursor", str(self.cursor), "--lock", str(self.root / "lock"),
                              "--log-file", str(self.root / "log")])
        self.assertEqual(code, 3)
        client.assert_not_called()

    def test_main_passes_independent_mutex_name(self):
        captured = {}

        class CapturingLock:
            def __init__(self, path, log, **kwargs):
                captured.update(kwargs)
            def acquire(self): raise sync.base.SyncError("lock_busy", "stop")

        with mock.patch.object(sync.base, "SyncLock", CapturingLock), \
             mock.patch.object(sync.base, "release_sync_lock"), \
             mock.patch.object(sync.base, "FeishuClient") as client:
            code = sync.main(["--ippool-state", str(self.state), "--final-provider", str(self.provider),
                              "--cursor", str(self.cursor), "--lock", str(self.root / "lock"),
                              "--log-file", str(self.root / "log")])
        self.assertEqual(code, 3)
        client.assert_not_called()
        self.assertEqual(captured.get("mutex_name"), sync.XIGUA_SYNC_MUTEX_NAME)
        self.assertNotEqual(sync.XIGUA_SYNC_MUTEX_NAME, sync.base.SYNC_MUTEX_NAME)

    @windows_only
    def test_wrapper_exit_priority_and_sync_order_matrix(self):
        wrapper = ROOT / "scripts" / "run-xigua-ippool.ps1"
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        stub = self.root / "wrapper_stub.py"
        log = self.root / "wrapper-calls.jsonl"
        stub.write_text(
            "import json, os, pathlib, sys\n"
            f"log=pathlib.Path({str(log)!r})\n"
            "name=pathlib.Path(sys.argv[1]).name\n"
            "with log.open('a',encoding='utf-8') as f:f.write(json.dumps(name)+'\\n')\n"
            "if name=='ippool-collector.py':\n"
            " p=pathlib.Path(sys.argv[sys.argv.index('--state')+1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text('{\"schema\":2,\"updated_at\":null,\"hosts\":{}}'); raise SystemExit(int(os.environ['COLLECTOR_EXIT']))\n"
            "if name=='health-check-provider.py': raise SystemExit(int(os.environ['HEALTH_EXIT']))\n"
            "if name=='sync-xigua-ippool-feishu.py': raise SystemExit(int(os.environ['FEISHU_EXIT']))\n"
            "raise SystemExit(99)\n",
            encoding="utf-8",
        )
        (fake_bin / "python.cmd").write_text(
            f'@echo off\r\n"{os.sys.executable}" "{stub}" %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")

        cases = [
            ("dry_ok", "dry-run", 0, 0, 0, 0, ["ippool-collector.py", "sync-xigua-ippool-feishu.py"]),
            ("dry_immature", "dry-run", 2, 0, 0, 2, ["ippool-collector.py", "sync-xigua-ippool-feishu.py"]),
            ("dry_feishu_fail", "dry-run", 0, 0, 6, 6, ["ippool-collector.py", "sync-xigua-ippool-feishu.py"]),
            ("dry_pool_and_feishu_fail", "dry-run", 2, 0, 6, 2, ["ippool-collector.py", "sync-xigua-ippool-feishu.py"]),
            ("prod_ok", "production", 0, 0, 0, 0, ["ippool-collector.py", "health-check-provider.py", "sync-xigua-ippool-feishu.py"]),
            ("prod_stage2_fail", "production", 0, 7, 0, 7, ["ippool-collector.py", "health-check-provider.py", "sync-xigua-ippool-feishu.py"]),
            ("prod_stage2_and_feishu_fail", "production", 0, 7, 6, 7, ["ippool-collector.py", "health-check-provider.py", "sync-xigua-ippool-feishu.py"]),
            ("prod_feishu_fail", "production", 0, 0, 6, 6, ["ippool-collector.py", "health-check-provider.py", "sync-xigua-ippool-feishu.py"]),
            ("prod_immature", "production", 2, 0, 0, 2, ["ippool-collector.py", "sync-xigua-ippool-feishu.py"]),
        ]
        for name, mode, collector_exit, health_exit, feishu_exit, expected, order in cases:
            with self.subTest(name=name):
                log.unlink(missing_ok=True)
                runtime = self.root / name
                env = os.environ.copy()
                env.update({"PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
                            "COLLECTOR_EXIT": str(collector_exit), "HEALTH_EXIT": str(health_exit),
                            "FEISHU_EXIT": str(feishu_exit)})
                command = ["powershell.exe", "-NoProfile", "-File", str(wrapper),
                           "-SourceProvider", str(self.state), "-RuntimeRoot", str(runtime), "-Mode", mode]
                if mode == "production":
                    # Use -Command so the string-array Target binds exactly.
                    def q(value): return "'" + str(value).replace("'", "''") + "'"
                    expression = (
                        f"& {q(wrapper)} -SourceProvider {q(self.state)} -RuntimeRoot {q(runtime)} "
                        f"-Mode production -FinalProvider {q(self.root / 'final-out.yaml')} "
                        f"-Core {q(self.root / 'mihomo.exe')} -Target @('https://a.example','https://b.example'); "
                        "exit $LASTEXITCODE"
                    )
                    command = ["powershell.exe", "-NoProfile", "-Command", expression]
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
                self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
                calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(calls, order)
                self.assertIn("POOL_RESULT", completed.stdout)
                self.assertIn("FEISHU_RESULT", completed.stdout)
                self.assertIn(f"ROUND_RESULT exit={expected}", completed.stdout)

    @windows_only
    def test_log_failure_is_visible_and_nonzero(self):
        wrapper = ROOT / "scripts" / "run-xigua-ippool.ps1"
        fake_bin = self.root / "fake-bin-logfail"
        fake_bin.mkdir()
        stub = self.root / "logfail_stub.py"
        stub.write_text(
            "import pathlib, sys\n"
            "name=pathlib.Path(sys.argv[1]).name\n"
            "if name=='ippool-collector.py':\n"
            " p=pathlib.Path(sys.argv[sys.argv.index('--state')+1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text('{\"schema\":2,\"updated_at\":null,\"hosts\":{}}'); raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        (fake_bin / "python.cmd").write_text(
            f'@echo off\r\n"{os.sys.executable}" "{stub}" %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        log_path = self.root / "log-is-a-directory"
        log_path.mkdir()
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        completed = subprocess.run([
            "powershell.exe", "-NoProfile", "-File", str(wrapper),
            "-SourceProvider", str(self.state), "-RuntimeRoot", str(self.root / "logfail-runtime"),
            "-Mode", "dry-run", "-LogFile", str(log_path),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(completed.returncode, 8, completed.stdout + completed.stderr)
        self.assertIn("XIGUA_LOG_FAILED", completed.stderr)
        self.assertIn("ROUND_RESULT exit=8", completed.stdout)

    @windows_only
    def test_wrapper_log_records_stages_without_secrets(self):
        wrapper = ROOT / "scripts" / "run-xigua-ippool.ps1"
        fake_bin = self.root / "fake-bin-logok"
        fake_bin.mkdir()
        stub = self.root / "logok_stub.py"
        stub.write_text(
            "import pathlib, sys\n"
            "name=pathlib.Path(sys.argv[1]).name\n"
            "if name=='ippool-collector.py':\n"
            " p=pathlib.Path(sys.argv[sys.argv.index('--state')+1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text('{\"schema\":2,\"updated_at\":null,\"hosts\":{}}'); raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        (fake_bin / "python.cmd").write_text(
            f'@echo off\r\n"{os.sys.executable}" "{stub}" %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        runtime = self.root / "logok-runtime"
        completed = subprocess.run([
            "powershell.exe", "-NoProfile", "-File", str(wrapper),
            "-SourceProvider", str(self.state), "-RuntimeRoot", str(runtime), "-Mode", "dry-run",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        log_text = (runtime / "xigua-ippool.log").read_text(encoding="utf-8")
        for marker in ("round start", "stage collector exit=0", "stage health skipped",
                       "stage feishu exit=0", "round end"):
            self.assertIn(marker, log_text)
        for secret in ("SYNTHETIC-PASSWORD", "secret-sni.example", "FEISHU_APP_SECRET"):
            self.assertNotIn(secret, log_text)


class NinjaAndBatExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def psq(value):
        return "'" + str(value).replace("'", "''") + "'"

    @windows_only
    def test_ninja_installer_inherits_main_principal(self):
        installer = ROOT / "scripts" / "install-ninja-grok-sync-task.ps1"
        captured = self.root / "principal.json"
        expression = (
            "$ErrorActionPreference='Stop'; "
            "function global:Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) "
            "[pscustomobject]@{ Principal=[pscustomobject]@{ UserId='SYNTHETIC\\User'; LogonType='S4U'; RunLevel='Highest' } } }; "
            "function global:Get-Command { [CmdletBinding()] param([string]$Name) [pscustomobject]@{ Source='C:\\fake\\python.exe' } }; "
            "function global:Test-Path { param($LiteralPath,$PathType) return $true }; "
            "function global:New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) [pscustomobject]@{} }; "
            "function global:New-ScheduledTaskTrigger { param([switch]$Once,$At,$RepetitionInterval) [pscustomobject]@{} }; "
            "function global:New-ScheduledTaskSettingsSet { param($MultipleInstances,[switch]$StartWhenAvailable,$ExecutionTimeLimit) [pscustomobject]@{} }; "
            "function global:New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) [pscustomobject]@{ UserId=$UserId; LogonType=$LogonType; RunLevel=$RunLevel } }; "
            f"function global:Register-ScheduledTask {{ [CmdletBinding()] param($TaskName,$Action,$Trigger,$Principal,$Settings) "
            f"$Principal | ConvertTo-Json -Compress | Set-Content -LiteralPath {self.psq(captured)} -Encoding UTF8; [pscustomobject]@{{}} }}; "
            f"& {self.psq(installer)}"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", expression], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        principal = json.loads(captured.read_text(encoding="utf-8-sig"))
        self.assertEqual(principal["UserId"], "SYNTHETIC\\User")
        self.assertEqual(principal["LogonType"], "S4U")
        self.assertEqual(principal["RunLevel"], "Highest")

    @windows_only
    def test_ninja_installer_permission_failure_is_nonzero(self):
        installer = ROOT / "scripts" / "install-ninja-grok-sync-task.ps1"
        expression = (
            "function global:Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) throw 'Access is denied' }; "
            f"& {self.psq(installer)}"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", expression], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Access is denied", completed.stdout + completed.stderr)

    @windows_only
    def test_bat_any_stage_failure_makes_final_nonzero(self):
        bat = ROOT / "scripts" / "run-feishu-bitable-sync.bat"
        site_dir = self.root / "sitecustomize"
        site_dir.mkdir()
        (site_dir / "sitecustomize.py").write_text(
            "import os, sys\n"
            "names = set()\n"
            "for value in list(getattr(sys, 'argv', [])) + list(getattr(sys, 'orig_argv', [])):\n"
            "    if value:\n"
            "        names.add(os.path.basename(value))\n"
            "if 'sync-feishu-bitable.py' in names:\n"
            "    os._exit(int(os.environ['NINJA_TEST_SYNC_RC']))\n"
            "if 'proxy-auto-switch.py' in names:\n"
            "    os._exit(int(os.environ['NINJA_TEST_SWITCH_RC']))\n",
            encoding="utf-8",
        )
        log_dir = self.root / "logs"
        log_dir.mkdir()
        cases = [
            ("ok", 0, 0, 0),
            ("sync_fail", 5, 0, 5),
            ("switch_fail", 0, 7, 7),
            ("both_fail", 5, 7, 5),
        ]
        for name, sync_exit, switch_exit, expected in cases:
            with self.subTest(name=name):
                (log_dir / "feishu-bitable-sync.stdout.log").unlink(missing_ok=True)
                (log_dir / "proxy-auto-switch-stdout.log").unlink(missing_ok=True)
                env = os.environ.copy()
                env["BILL_LOG_DIR"] = str(log_dir)
                env["NINJA_TEST_SYNC_RC"] = str(sync_exit)
                env["NINJA_TEST_SWITCH_RC"] = str(switch_exit)
                env["PYTHONPATH"] = str(site_dir) + os.pathsep + env.get("PYTHONPATH", "")
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                completed = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(bat)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
                log_text = ""
                for log_name in ("feishu-bitable-sync.stdout.log", "proxy-auto-switch-stdout.log"):
                    log_path = log_dir / log_name
                    if log_path.exists():
                        log_text += "\n--- " + log_name + " ---\n" + log_path.read_text(encoding="utf-8", errors="replace")
                self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr + log_text)
                self.assertIn(f"SYNC_RESULT exit={sync_exit}", completed.stdout)
                self.assertIn(f"SWITCH_RESULT exit={switch_exit}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
