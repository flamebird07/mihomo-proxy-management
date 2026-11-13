"""CLI compatibility guard for ``scripts/health-check-provider.py``.

The Windows scheduled tasks (``refresh-production.ps1``, ``run-xigua-ippool.ps1``)
call this script with the original arguments and parse its stdout, so the split
into importable functions must not change the surface:

* ``--help`` must match the snapshot taken from ``main@34b3b0e``;
* one mocked run must produce the same stdout payload (same keys, same order,
  same values apart from the content digest) and the same exit code;
* when ``MPM_HC_MAIN_SNAPSHOT`` points at the real main-version script, the same
  mock must drive both scripts to the same stdout and exit code (skipped
  otherwise: the split removed that file from the work tree).

All checks are host-independent: the isolated core, the controller and the
port probe are mocked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "health-check-provider.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HELP_SNAPSHOT = FIXTURES / "health_check_provider_main_help.txt"
RUN_SNAPSHOT = FIXTURES / "health_check_provider_main_run.json"

USAGE_PROG = re.compile(r"^usage:\s+\S+\s+", re.MULTILINE)


def _normalise(text: str) -> str:
    """Ignore wrapping/column widths and the program name, compare the words."""
    text = USAGE_PROG.sub("usage: ", text.strip(), count=1)
    return " ".join(text.split())


def _load_module():
    spec = importlib.util.spec_from_file_location("health_check_provider_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_matches_main_snapshot():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = HELP_SNAPSHOT.read_text(encoding="utf-8")
    assert _normalise(result.stdout) == _normalise(expected)


class _FakeProcess:
    """Stand-in for the isolated mihomo process."""

    def __init__(self, *args, **kwargs) -> None:
        self.returncode = None
        self.argv = args[0] if args else []

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_mocked_run_matches_main_snapshot(tmp_path, monkeypatch, capsys):
    module = _load_module()
    password = secrets.token_hex(8)  # fake value generated at runtime
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(
        yaml.safe_dump(
            {
                "proxies": [
                    {
                        "name": "美国-01",
                        "type": "ss",
                        "server": "192.0.2.10",
                        "port": 8388,
                        "cipher": "aes-128-gcm",
                        "password": password,
                    },
                    {
                        "name": "日本-01",
                        "type": "ss",
                        "server": "192.0.2.11",
                        "port": 8388,
                        "cipher": "aes-128-gcm",
                        "password": password,
                    },
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "provider.yaml"

    monkeypatch.setattr(module, "port_is_free", lambda port: True)
    monkeypatch.setattr(module, "wait_ready", lambda api, process: None)
    monkeypatch.setattr(
        module,
        "request_delay",
        lambda api, name, target, timeout_ms: 25 if name == "美国-01" else None,
    )
    monkeypatch.setattr(module.subprocess, "Popen", _FakeProcess)

    argv = [
        "--candidate",
        str(candidate),
        "--output",
        str(output),
        "--core",
        "/nonexistent/mihomo",
        "--target",
        "https://probe.example.invalid/204",
        "--mixed-port",
        "17890",
        "--controller-port",
        "19090",
    ]
    assert module.main(argv) == 0

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, "stdout must stay a single JSON line"
    payload = json.loads(lines[0])

    expected = json.loads(RUN_SNAPSHOT.read_text(encoding="utf-8"))
    assert list(payload) == list(expected), "JSON key order must not change"
    assert re.fullmatch(r"[0-9a-f]{64}", payload["output_sha256"])
    masked = dict(payload)
    masked["output_sha256"] = "<sha256>"
    assert masked == expected

    written = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert [item["name"] for item in written["proxies"]] == ["美国-01"]


def test_publish_blocked_exit_is_a_system_exit(tmp_path, monkeypatch):
    """The fail-closed path keeps its non-zero SystemExit semantics."""
    module = _load_module()
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(
        yaml.safe_dump(
            {"proxies": [{"name": "日本-01", "type": "ss", "server": "192.0.2.11", "port": 1}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "port_is_free", lambda port: True)
    monkeypatch.setattr(module, "wait_ready", lambda api, process: None)
    monkeypatch.setattr(module, "request_delay", lambda api, name, target, timeout_ms: 10)
    monkeypatch.setattr(module.subprocess, "Popen", _FakeProcess)

    argv = [
        "--candidate",
        str(candidate),
        "--output",
        str(tmp_path / "out.yaml"),
        "--core",
        "/nonexistent/mihomo",
        "--target",
        "https://probe.example.invalid/204",
        "--mixed-port",
        "17890",
        "--controller-port",
        "19090",
    ]
    with pytest.raises(SystemExit) as excinfo:
        module.main(argv)
    assert "publish blocked" in str(excinfo.value)
    assert not (tmp_path / "out.yaml").exists()


# ---------------------------------------------------------------------------
# Optional old-vs-new comparison against the real main-version script.
#
# The snapshot is produced by the caller, e.g.
#     git show 34b3b0e:scripts/health-check-provider.py > "$MPM_HC_MAIN_SNAPSHOT"
# and the test is skipped when the variable is unset: after the split the main
# version no longer exists in the work tree, so there is no baseline to run.
# ---------------------------------------------------------------------------

MAIN_SNAPSHOT_ENV = "MPM_HC_MAIN_SNAPSHOT"

# Run one copy of the script with every side effect mocked, inside a child
# interpreter.  Using a child keeps the same mock for the main version and the
# new one, and patching ``subprocess.Popen`` cannot break the parent's
# ``subprocess.run``.
_MOCK_DRIVER = """
import importlib.util
import subprocess
import sys

script = sys.argv[1]
spec = importlib.util.spec_from_file_location("health_check_provider_under_test", script)
module = importlib.util.module_from_spec(spec)
sys.modules["health_check_provider_under_test"] = module
spec.loader.exec_module(module)


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def fake_delay(api, name, target, timeout_ms):
    return 25 if name == "美国-01" else None


for attribute in ("main", "port_is_free", "wait_ready", "request_delay"):
    if not hasattr(module, attribute):
        raise SystemExit("mock driver: %s is missing from %s" % (attribute, script))

subprocess.Popen = FakeProcess
for attribute, value in (
    ("Popen", FakeProcess),
    ("port_is_free", lambda port: True),
    ("wait_ready", lambda api, process: None),
    ("request_delay", fake_delay),
):
    if hasattr(module, attribute):
        setattr(module, attribute, value)

sys.argv = [script] + sys.argv[2:]
raise SystemExit(module.main())
"""


def _run_mocked(script: Path, argv: list[str]) -> subprocess.CompletedProcess:
    """Run ``script`` in a child process with the shared mock applied."""
    return subprocess.run(
        [sys.executable, "-c", _MOCK_DRIVER, str(script), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def _single_payload(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, "stdout must stay a single JSON line"
    return json.loads(lines[0])


def _candidate(tmp_path: Path) -> Path:
    password = secrets.token_hex(8)  # fake value generated at runtime
    candidate = tmp_path / "candidate-main-compare.yaml"
    candidate.write_text(
        yaml.safe_dump(
            {
                "proxies": [
                    {
                        "name": "美国-01",
                        "type": "ss",
                        "server": "192.0.2.10",
                        "port": 8388,
                        "cipher": "aes-128-gcm",
                        "password": password,
                    },
                    {
                        "name": "日本-01",
                        "type": "ss",
                        "server": "192.0.2.11",
                        "port": 8388,
                        "cipher": "aes-128-gcm",
                        "password": password,
                    },
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return candidate


def test_mocked_run_matches_the_main_script(tmp_path):
    """Same mock, main version vs new version: identical stdout and exit code.

    Skipped when ``MPM_HC_MAIN_SNAPSHOT`` is unset: the split replaced the main
    version in the work tree, so without a snapshot file there is nothing to
    compare against.
    """
    snapshot = os.environ.get(MAIN_SNAPSHOT_ENV)
    if not snapshot:
        pytest.skip(
            "MPM_HC_MAIN_SNAPSHOT is not set: the main-version script is no longer in "
            "the work tree, so the old/new comparison has no baseline; point the "
            "variable at `git show 34b3b0e:scripts/health-check-provider.py` to enable it"
        )
    main_script = Path(snapshot)
    if not main_script.is_file():
        pytest.fail(f"{MAIN_SNAPSHOT_ENV} does not point at a file: {main_script}")

    candidate = _candidate(tmp_path)
    common = [
        "--candidate",
        str(candidate),
        "--core",
        "/nonexistent/mihomo",
        "--target",
        "https://probe.example.invalid/204",
        "--mixed-port",
        "17890",
        "--controller-port",
        "19090",
    ]
    main_output = tmp_path / "provider-main.yaml"
    new_output = tmp_path / "provider-new.yaml"
    main_run = _run_mocked(main_script, common + ["--output", str(main_output)])
    new_run = _run_mocked(SCRIPT, common + ["--output", str(new_output)])

    assert main_run.returncode == new_run.returncode, (
        f"exit codes differ (main={main_run.returncode}, new={new_run.returncode})\n"
        f"main stderr: {main_run.stderr}\nnew stderr: {new_run.stderr}"
    )
    assert main_run.returncode == 0, main_run.stderr

    main_payload = _single_payload(main_run.stdout)
    new_payload = _single_payload(new_run.stdout)
    assert list(main_payload) == list(new_payload), "JSON key order must not change"
    assert re.fullmatch(r"[0-9a-f]{64}", new_payload["output_sha256"])
    masked_main = dict(main_payload, output_sha256="<sha256>")
    masked_new = dict(new_payload, output_sha256="<sha256>")
    assert masked_main == masked_new, "stdout differs beyond the content digest"

    for written in (main_output, new_output):
        document = yaml.safe_load(written.read_text(encoding="utf-8"))
        assert [item["name"] for item in document["proxies"]] == ["美国-01"]
