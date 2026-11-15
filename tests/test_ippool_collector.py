import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ippool-collector.py"
spec = importlib.util.spec_from_file_location("ippool_collector", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def windows_only(func):
    """Tests that really launch powershell.exe/cmd.exe; pure parsing stays."""
    func = pytest.mark.windows_only(func)
    return pytest.mark.skipif(
        sys.platform != "win32", reason="windows_only: real powershell.exe execution"
    )(func)


def node(name="西瓜-美国", server="us.example.indxg.com", port=46002):
    return {
        "name": name, "type": "trojan", "server": server, "port": port,
        "password": "SYNTHETIC-SECRET", "sni": "cover.example.com",
        "skip-cert-verify": True, "udp": True,
    }


class Args(SimpleNamespace):
    pass


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.yaml"
        self.state = self.root / "state" / "ippool.json"
        self.output = self.root / "provider.yaml"
        self.lock = self.root / "collector.lock"
        self.source.write_text(yaml.safe_dump({"proxies": [node()]}, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def args(self, mode="dry-run", validation=None):
        return Args(source_provider=str(self.source), state=str(self.state),
                    output_provider=str(self.output), lock=str(self.lock), mode=mode,
                    validate_command=validation, doh_endpoint=["fake:{name}"],
                    doh_repetitions=2, timeout=0.5, proxy_host="127.0.0.1",
                    proxy_port=7890, probe_interval=0, max_ips_per_node=3,
                    lock_stale_seconds=900)

    @staticmethod
    def resolver(host, endpoint, timeout):
        return {"1.1.1.1", "198.18.1.2"}

    @staticmethod
    def healthy(ip, node_data, proxy_host, proxy_port, timeout):
        return True, 12.0, "ok"

    @staticmethod
    def unhealthy(ip, node_data, proxy_host, proxy_port, timeout):
        return False, None, "timeout"

    def test_discover_aggregates_and_filters_fake_ip(self):
        result, errors = mod.discover({"a.example"}, ["x", "y"], 2, 1, self.resolver)
        self.assertEqual(errors, 0)
        self.assertEqual(result["a.example"], {"1.1.1.1"})
        self.assertFalse(mod.is_public_ip("198.18.1.2"))

    def test_two_successes_publish_and_state_has_no_secret(self):
        self.assertEqual(mod.run(self.args(), self.resolver, self.healthy), 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(mod.run(self.args(), self.resolver, self.healthy), 0)
        data = yaml.safe_load(self.output.read_text(encoding="utf-8"))
        self.assertEqual(data["proxies"][0]["server"], "1.1.1.1")
        self.assertEqual(data["proxies"][0]["password"], "SYNTHETIC-SECRET")
        state_text = self.state.read_text(encoding="utf-8")
        self.assertNotIn("SYNTHETIC-SECRET", state_text)
        self.assertNotIn("cover.example.com", state_text)

    def test_three_failures_remove_but_do_not_overwrite_previous(self):
        mod.run(self.args(), self.resolver, self.healthy)
        mod.run(self.args(), self.resolver, self.healthy)
        original = self.output.read_bytes()
        for _ in range(3):
            mod.run(self.args(), self.resolver, self.unhealthy)
        self.assertEqual(self.output.read_bytes(), original)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertFalse(state["hosts"]["us.example.indxg.com"]["ips"]["1.1.1.1"]["healthy"])

    def test_max_three_ips_per_node_and_fields_preserved(self):
        provider = mod.build_provider(
            [node()], {"us.example.indxg.com": ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.2.2.2"]}, 3)
        self.assertEqual(len(provider["proxies"]), 3)
        for item in provider["proxies"]:
            self.assertEqual(item["sni"], "cover.example.com")
            self.assertTrue(item["skip-cert-verify"])
            self.assertIn("@", item["name"])

    def test_atomic_unchanged_does_not_rewrite(self):
        target = self.root / "same.txt"
        self.assertTrue(mod.atomic_write_if_changed(target, b"same"))
        before = target.stat().st_mtime_ns
        time.sleep(0.01)
        self.assertFalse(mod.atomic_write_if_changed(target, b"same"))
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_lock_rejects_second_instance(self):
        with mod.FileLock(self.lock):
            with self.assertRaises(mod.AlreadyRunning):
                with mod.FileLock(self.lock):
                    pass

    def test_live_pid_old_lock_is_not_stolen(self):
        self.lock.write_text(json.dumps({"pid": os.getpid(), "token": "live-token"}), encoding="utf-8")
        old = time.time() - 10000
        os.utime(self.lock, (old, old))
        with self.assertRaises(mod.AlreadyRunning):
            with mod.FileLock(self.lock, stale_seconds=1):
                pass
        self.assertTrue(self.lock.exists())
        self.assertEqual(json.loads(self.lock.read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_live_pid_legacy_lock_without_token_is_not_stolen(self):
        self.lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        old = time.time() - 10000
        os.utime(self.lock, (old, old))
        with self.assertRaises(mod.AlreadyRunning):
            with mod.FileLock(self.lock, stale_seconds=1):
                pass
        self.assertTrue(self.lock.exists())

    def test_production_requires_validator_and_keeps_old(self):
        self.output.write_bytes(b"old-provider")
        mod.run(self.args(), self.resolver, self.healthy)
        args = self.args(mode="production", validation=None)
        self.assertEqual(mod.run(args, self.resolver, self.healthy), 4)
        self.assertEqual(self.output.read_bytes(), b"old-provider")

    def test_failed_validator_does_not_overwrite(self):
        self.output.write_bytes(b"old-provider")
        mod.run(self.args(), self.resolver, self.healthy)
        command = json.dumps([os.sys.executable, "-c", "raise SystemExit(7)", "{provider}"])
        self.assertEqual(mod.run(self.args(mode="production", validation=command), self.resolver, self.healthy), 4)
        self.assertEqual(self.output.read_bytes(), b"old-provider")

    def test_successful_unbound_validator_does_not_overwrite(self):
        self.output.write_bytes(b"old-provider")
        mod.run(self.args(), self.resolver, self.healthy)
        command = json.dumps([os.sys.executable, "-c", "raise SystemExit(0)"])
        self.assertEqual(
            mod.run(self.args(mode="production", validation=command), self.resolver, self.healthy),
            4,
        )
        self.assertEqual(self.output.read_bytes(), b"old-provider")

    def test_connect_and_tls_response_via_local_stub(self):
        ready = threading.Event()
        holder = {}

        def server():
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            holder["port"] = listener.getsockname()[1]
            listener.listen(1)
            ready.set()
            conn, _ = listener.accept()
            try:
                request = conn.recv(4096)
                self.assertIn(b"CONNECT 1.1.1.1:46002", request)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                conn.recv(4096)
                # Fatal TLS alert (record type 21, TLS 1.2, internal_error).
                conn.sendall(b"\x15\x03\x03\x00\x02\x02\x50")
            finally:
                conn.close()
                listener.close()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        ready.wait(2)
        ok, _latency, reason = mod.probe_connect_tls(
            "1.1.1.1", node(), "127.0.0.1", holder["port"], 1.0)
        thread.join(2)
        self.assertTrue(ok)
        self.assertEqual(reason, "tls_response")

    def test_load_nodes_excludes_direct_and_non_xigua(self):
        items = [node(), {"name": "direct", "type": "direct"},
                 node(name="other", server="other.example.com")]
        self.source.write_text(yaml.safe_dump({"proxies": items}, allow_unicode=True), encoding="utf-8")
        loaded = mod.load_nodes(self.source)
        self.assertEqual(len(loaded), 1)

    def test_two_hosts_never_cross_assign_ips(self):
        host_a = "a.example.indxg.com"
        host_b = "b.example.indxg.com"
        self.source.write_text(yaml.safe_dump({"proxies": [
            node(name="西瓜-A", server=host_a, port=41001),
            node(name="西瓜-B", server=host_b, port=42002),
        ]}, allow_unicode=True), encoding="utf-8")

        def resolver(host, endpoint, timeout):
            return {"1.1.1.1"} if host == host_a else {"8.8.8.8"}

        seen = []

        def prober(ip, item, proxy_host, proxy_port, timeout):
            seen.append((item["server"], ip, item["port"]))
            expected = {host_a: "1.1.1.1", host_b: "8.8.8.8"}
            return ip == expected[item["server"]], 10.0, "ok"

        mod.run(self.args(), resolver, prober)
        mod.run(self.args(), resolver, prober)
        output = yaml.safe_load(self.output.read_text(encoding="utf-8"))["proxies"]
        mapping = {item["name"].split("@")[0]: item["server"] for item in output}
        self.assertEqual(mapping, {"西瓜-A": "1.1.1.1", "西瓜-B": "8.8.8.8"})
        self.assertNotIn((host_a, "8.8.8.8", 41001), seen)
        self.assertNotIn((host_b, "1.1.1.1", 42002), seen)

    def test_empty_host_does_not_block_healthy_other_host(self):
        host_a = "a.example.indxg.com"
        host_b = "empty.example.indxg.com"
        self.source.write_text(yaml.safe_dump({"proxies": [
            node(name="西瓜-A", server=host_a),
            node(name="西瓜-empty", server=host_b),
        ]}, allow_unicode=True), encoding="utf-8")

        def resolver(host, endpoint, timeout):
            return {"1.1.1.1"} if host == host_a else set()

        mod.run(self.args(), resolver, self.healthy)
        self.assertEqual(mod.run(self.args(), resolver, self.healthy), 0)
        output = yaml.safe_load(self.output.read_text(encoding="utf-8"))["proxies"]
        self.assertEqual([item["name"].split("@")[0] for item in output], ["西瓜-A"])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["schema"], 2)
        self.assertIn(host_b, state["hosts"])

    def test_schema_v1_fails_closed(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"schema": 1, "ips": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported state schema"):
            mod.run(self.args(), self.resolver, self.healthy)
        self.assertFalse(self.output.exists())

    @windows_only
    def test_production_wrapper_requires_explicit_final_provider(self):
        wrapper = ROOT / "scripts" / "run-xigua-ippool.ps1"
        completed = subprocess.run([
            "powershell.exe", "-NoProfile", "-File", str(wrapper),
            "-SourceProvider", str(self.source), "-RuntimeRoot", str(self.root / "runtime"),
            "-Mode", "production",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("FinalProvider", completed.stdout + completed.stderr)

    @windows_only
    def test_stage2_failure_keeps_final_and_passes_bound_arguments(self):
        wrapper = ROOT / "scripts" / "run-xigua-ippool.ps1"
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        log = self.root / "argv.jsonl"
        stub = self.root / "python_stub.py"
        stub.write_text(
            "import json, pathlib, sys\n"
            f"log=pathlib.Path({str(log)!r})\n"
            "with log.open('a', encoding='utf-8') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "args=sys.argv[2:]\n"
            "if sys.argv[1].endswith('ippool-collector.py'):\n"
            " p=pathlib.Path(args[args.index('--output-provider')+1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text('proxies: []',encoding='utf-8'); raise SystemExit(0)\n"
            "if sys.argv[1].endswith('health-check-provider.py'): raise SystemExit(7)\n"
            "if sys.argv[1].endswith('sync-xigua-ippool-feishu.py'): raise SystemExit(0)\n"
            "raise SystemExit(9)\n",
            encoding="utf-8",
        )
        command = fake_bin / "python.cmd"
        command.write_text(
            f'@echo off\r\n"{os.sys.executable}" "{stub}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            encoding="utf-8",
        )
        final = self.root / "final.yaml"
        final.write_bytes(b"old-final")
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        def psq(value):
            return "'" + str(value).replace("'", "''") + "'"

        command_line = (
            f"& {psq(wrapper)} -SourceProvider {psq(self.source)} "
            f"-RuntimeRoot {psq(self.root / 'runtime')} -Mode production "
            f"-FinalProvider {psq(final)} -Core {psq(self.root / 'mihomo.exe')} "
            "-Target @('https://www.gstatic.com/generate_204','https://chatgpt.com/cdn-cgi/trace') "
            "-MixedPort 17891 -ControllerPort 19091 -TimeoutMs 4321 -Workers 3"
        )
        completed = subprocess.run([
            "powershell.exe", "-NoProfile", "-Command", command_line,
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(final.read_bytes(), b"old-final")
        if not log.exists():
            self.fail("stub was not invoked: " + completed.stdout + completed.stderr)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3)
        health = calls[1]
        self.assertTrue(health[0].endswith("health-check-provider.py"))
        self.assertEqual(health.count("--target"), 2)
        self.assertTrue(os.path.samefile(
            health[health.index("--candidate") + 1], self.root / "runtime" / "xigua-ippool.stage1.yaml"))
        self.assertTrue(os.path.samefile(health[health.index("--output") + 1], final))
        self.assertEqual(health[health.index("--minimum-matching") + 1], "1")
        wrapper_text = wrapper.read_text(encoding="utf-8")
        self.assertIn("'--required-name-regex', '西瓜'", wrapper_text)
        self.assertTrue(calls[2][0].endswith("sync-xigua-ippool-feishu.py"))
        self.assertTrue(os.path.samefile(calls[2][calls[2].index("--final-provider") + 1], final))

    @windows_only
    def test_task_installer_uses_short_launcher_and_whatif_has_no_side_effect(self):
        installer = ROOT / "scripts" / "install-xigua-ippool-task.ps1"
        runtime = self.root / "rt"
        final = self.root / ("f" * 80) / "final.yaml"
        log = self.root / "schtasks-args.json"

        def psq(value):
            return "'" + str(value).replace("'", "''") + "'"

        target1 = "https://example.com/" + "a" * 120
        target2 = "https://example.net/" + "b" * 120
        command = (
            f"function global:schtasks.exe {{ $args | ConvertTo-Json -Compress | "
            f"Set-Content -LiteralPath {psq(log)} -Encoding UTF8; $global:LASTEXITCODE=0 }}; "
            f"& {psq(installer)} -SourceProvider {psq(self.source)} -RuntimeRoot {psq(runtime)} "
            f"-Mode production -FinalProvider {psq(final)} -Core {psq(self.root / 'mihomo.exe')} "
            f"-Target @({psq(target1)},{psq(target2)})"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        launcher = runtime / "scheduled-run.ps1"
        self.assertTrue(launcher.exists())
        launcher_text = launcher.read_text(encoding="utf-8")
        self.assertIn(target1, launcher_text)
        self.assertIn(target2, launcher_text)
        self.assertIn(("f" * 80) + "\\final.yaml", launcher_text)
        args = json.loads(log.read_text(encoding="utf-8-sig"))
        action = args[args.index("/TR") + 1]
        self.assertLessEqual(len(action), 261)
        self.assertIn("scheduled-run.ps1", action)
        self.assertNotIn(target1, action)

        # A second install must atomically replace an existing launcher using
        # File.Replace with an explicit backup path (Windows PowerShell/.NET
        # Framework rejects the old null-backup overload in this environment).
        target3 = "https://example.org/" + "c" * 100
        update = (
            f"function global:schtasks.exe {{ $args | ConvertTo-Json -Compress | "
            f"Set-Content -LiteralPath {psq(log)} -Encoding UTF8; $global:LASTEXITCODE=0 }}; "
            f"& {psq(installer)} -SourceProvider {psq(self.source)} -RuntimeRoot {psq(runtime)} "
            f"-Mode production -FinalProvider {psq(final)} -Core {psq(self.root / 'mihomo.exe')} "
            f"-Target @({psq(target1)},{psq(target3)})"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", update], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        updated_text = launcher.read_text(encoding="utf-8")
        self.assertIn(target3, updated_text)
        self.assertNotIn(target2, updated_text)
        leftovers = list(runtime.glob(".scheduled-run.*.tmp"))
        self.assertEqual(leftovers, [])

        whatif_root = self.root / "whatif"
        whatif_log = self.root / "whatif-called"
        whatif = (
            f"function global:schtasks.exe {{ Set-Content -LiteralPath {psq(whatif_log)} x; "
            "$global:LASTEXITCODE=0 }; "
            f"& {psq(installer)} -SourceProvider {psq(self.source)} -RuntimeRoot {psq(whatif_root)} -WhatIf"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", whatif], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertFalse(whatif_root.exists())
        self.assertFalse(whatif_log.exists())

    @windows_only
    def test_task_installer_surfaces_schtasks_failure(self):
        installer = ROOT / "scripts" / "install-xigua-ippool-task.ps1"
        runtime = self.root / "failure-rt"

        def psq(value):
            return "'" + str(value).replace("'", "''") + "'"

        command = (
            "function global:schtasks.exe { Write-Host 'FAKE-SCHTASKS-ERROR'; "
            "$global:LASTEXITCODE=9 }; "
            f"& {psq(installer)} -SourceProvider {psq(self.source)} -RuntimeRoot {psq(runtime)}"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True, encoding="utf-8", errors="replace")
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("FAKE-SCHTASKS-ERROR", output)
        self.assertIn("schtasks failed with exit code 9", output)

    @windows_only
    def test_task_installer_passes_explicit_account_and_run_level(self):
        installer = ROOT / "scripts" / "install-xigua-ippool-task.ps1"
        runtime = self.root / "ru-rt"
        log = self.root / "schtasks-ru.json"

        def psq(value):
            return "'" + str(value).replace("'", "''") + "'"

        command = (
            f"function global:schtasks.exe {{ $args | ConvertTo-Json -Compress | "
            f"Set-Content -LiteralPath {psq(log)} -Encoding UTF8; $global:LASTEXITCODE=0 }}; "
            f"& {psq(installer)} -SourceProvider {psq(self.source)} -RuntimeRoot {psq(runtime)} "
            f"-RunAsUser 'SYNTHETIC\\Runner' -RunLevel HIGHEST"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        args = json.loads(log.read_text(encoding="utf-8-sig"))
        self.assertIn("/RU", args)
        self.assertIn("/RL", args)
        self.assertEqual(args[args.index("/RU") + 1], "SYNTHETIC\\Runner")
        self.assertEqual(args[args.index("/RL") + 1], "HIGHEST")


if __name__ == "__main__":
    unittest.main()
