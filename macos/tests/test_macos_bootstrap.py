"""Fresh-Mac bootstrap: interpreter discovery, the fixed venv and the deploy child.

Everything here is host-free: ``ensure_venv``/``bootstrap`` go through the
injected ``FakeExecutor``/``runner``, so no venv is created and no deploy child
is spawned on the test host.  The wrapper and CLI checks are plain regressions.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mpm_common.errors import ExitCode, FailClosed
from mpm_common.executor import FakeExecutor
from mpm_macos import cli, lifecycle

from macos_test_support import make_context

pytestmark = pytest.mark.macos

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SH = ROOT / "macos" / "scripts" / "bootstrap.sh"
COMMON_SH = ROOT / "macos" / "scripts" / "_common.sh"


class _NoPathExecutor(FakeExecutor):
    """FakeExecutor whose PATH lookup finds nothing on its own."""

    def which(self, name: str):
        return None


def _frameworks(tmp_path, *versions):
    base = tmp_path / "frameworks"
    for version in versions:
        python = base / version / "bin" / "python3"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
    return base


def _isolated(monkeypatch, tmp_path, *, fixed=()):
    monkeypatch.setattr(
        lifecycle, "PYTHON_FRAMEWORK_VERSIONS", str(tmp_path / "no-frameworks")
    )
    monkeypatch.setattr(lifecycle, "FIXED_BASE_PYTHONS", tuple(fixed))


# ---- interpreter discovery ------------------------------------------------


def test_base_python_order_is_override_frameworks_fixed_then_path(
    tmp_path, monkeypatch
):
    frameworks = _frameworks(tmp_path, "3.9.5", "3.13.0")
    monkeypatch.setattr(lifecycle, "PYTHON_FRAMEWORK_VERSIONS", str(frameworks))
    monkeypatch.setattr(
        lifecycle,
        "FIXED_BASE_PYTHONS",
        ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"),
    )
    ctx = make_context(
        tmp_path,
        environ={"MPM_BASE_PYTHON": "/custom/python3"},
        executor=_NoPathExecutor(),
    )
    assert lifecycle.base_python_candidates(ctx) == [
        "/custom/python3",
        str(frameworks / "3.13.0" / "bin" / "python3"),
        str(frameworks / "3.9.5" / "bin" / "python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]


def test_base_python_appends_the_context_path_last(tmp_path, monkeypatch):
    _isolated(monkeypatch, tmp_path)
    ctx = make_context(tmp_path)  # FakeExecutor.which -> /usr/bin/python3
    assert lifecycle.base_python_candidates(ctx) == ["/usr/bin/python3"]


def test_find_base_python_skips_a_too_old_framework(tmp_path, monkeypatch):
    frameworks = _frameworks(tmp_path, "3.13.0", "3.9.5")
    monkeypatch.setattr(lifecycle, "PYTHON_FRAMEWORK_VERSIONS", str(frameworks))
    monkeypatch.setattr(lifecycle, "FIXED_BASE_PYTHONS", ())
    new = str(frameworks / "3.13.0" / "bin" / "python3")
    old = str(frameworks / "3.9.5" / "bin" / "python3")
    executor = _NoPathExecutor()
    executor.set(f"{new} -c", (1, "3.7", ""))
    executor.set(f"{old} -c", (0, "3.9", ""))
    ctx = make_context(tmp_path, executor=executor)

    assert lifecycle.find_base_python(ctx) == old


def test_find_base_python_names_every_candidate_when_none_works(
    tmp_path, monkeypatch
):
    base = "/opt/homebrew/bin/python3"
    _isolated(monkeypatch, tmp_path, fixed=(base,))
    executor = _NoPathExecutor(default=(0, "", ""))
    executor.set(f"{base} -c", (1, "3.8", ""))
    ctx = make_context(tmp_path, executor=executor)

    with pytest.raises(FailClosed) as raised:
        lifecycle.find_base_python(ctx)

    message = raised.value.message
    assert "MPM_BASE_PYTHON" in message
    assert base in message
    assert "rc=1" in message


# ---- the fixed venv -------------------------------------------------------


def test_ensure_venv_reuses_a_venv_that_imports_yaml(tmp_path):
    ctx = make_context(tmp_path)  # FakeExecutor default: import yaml -> 0
    assert lifecycle.ensure_venv(ctx) == ctx.layout.venv_python
    assert ctx.executor.count(ctx.layout.venv_python, "-m", "venv") == 0
    assert ctx.executor.count(ctx.layout.venv_python, "-m", "pip") == 0


def test_ensure_venv_rebuilds_only_the_fixed_directory(tmp_path, monkeypatch):
    base = "/opt/homebrew/bin/python3"
    _isolated(monkeypatch, tmp_path, fixed=(base,))
    executor = _NoPathExecutor()
    ctx = make_context(tmp_path, executor=executor)
    venv_python = ctx.layout.venv_python
    probes = {"yaml": 0}

    def yaml_probe(_argv):
        probes["yaml"] += 1
        return (1, "", "") if probes["yaml"] == 1 else (0, "", "")

    executor.set(f"{base} -c", (0, "3.13", ""))
    executor.set(f"{venv_python} -c import yaml", yaml_probe)
    executor.set(f"{base} -m venv", (0, "", ""))
    executor.set(f"{venv_python} -m pip install", (0, "", ""))

    assert lifecycle.ensure_venv(ctx) == venv_python

    created = executor.find(base, "-m", "venv")
    assert created is not None
    assert created.argv == [
        base, "-m", "venv", "--clear", ctx.layout.libexec_venv
    ]
    installed = executor.find(venv_python, "-m", "pip", "install")
    assert installed is not None
    assert installed.argv[2:] == [
        "pip",
        "install",
        "--disable-pip-version-check",
        "--quiet",
        "pyyaml",
    ]
    assert probes["yaml"] >= 2  # initial check failed, the re-verify succeeded


def test_ensure_venv_reports_a_pip_failure(tmp_path, monkeypatch):
    base = "/opt/homebrew/bin/python3"
    _isolated(monkeypatch, tmp_path, fixed=(base,))
    executor = _NoPathExecutor()
    ctx = make_context(tmp_path, executor=executor)
    venv_python = ctx.layout.venv_python
    executor.set(f"{base} -c", (0, "3.13", ""))
    executor.set(f"{venv_python} -c import yaml", (1, "", ""))
    executor.set(f"{base} -m venv", (0, "", ""))
    executor.set(f"{venv_python} -m pip install", (1, "", "no network"))

    with pytest.raises(FailClosed) as raised:
        lifecycle.ensure_venv(ctx)
    assert "PyYAML" in raised.value.message
    assert "rc=1" in raised.value.message


def test_ensure_venv_refuses_a_non_directory_venv_path(tmp_path, monkeypatch):
    base = "/opt/homebrew/bin/python3"
    _isolated(monkeypatch, tmp_path, fixed=(base,))
    executor = _NoPathExecutor()
    ctx = make_context(tmp_path, executor=executor)
    Path(ctx.layout.libexec_venv).parent.mkdir(parents=True, exist_ok=True)
    Path(ctx.layout.libexec_venv).write_text("not a venv")
    executor.set(f"{base} -c", (0, "3.13", ""))
    executor.set(f"{ctx.layout.venv_python} -c import yaml", (1, "", ""))

    with pytest.raises(FailClosed) as raised:
        lifecycle.ensure_venv(ctx)
    assert "fixed venv directory" in raised.value.message
    assert executor.count(base, "-m", "venv") == 0


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs POSIX")
def test_ensure_venv_refuses_to_clear_through_a_symlink(tmp_path, monkeypatch):
    base = "/opt/homebrew/bin/python3"
    _isolated(monkeypatch, tmp_path, fixed=(base,))
    executor = _NoPathExecutor()
    ctx = make_context(tmp_path, executor=executor)
    venv_dir = Path(ctx.layout.libexec_venv)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    venv_dir.symlink_to(target, target_is_directory=True)
    executor.set(f"{base} -c", (0, "3.13", ""))
    executor.set(f"{ctx.layout.venv_python} -c import yaml", (1, "", ""))

    with pytest.raises(FailClosed) as raised:
        lifecycle.ensure_venv(ctx)
    assert "symlink" in raised.value.message
    assert executor.count(base, "-m", "venv") == 0


# ---- the deploy child -----------------------------------------------------


def _record_runner(code):
    record = {}

    def runner(argv, **kwargs):
        record["argv"] = list(argv)
        record.update(kwargs)
        return SimpleNamespace(returncode=code)

    return record, runner


def test_bootstrap_runs_deploy_with_argv_inherited_stdio_and_venv(tmp_path):
    ctx = make_context(tmp_path)
    record, runner = _record_runner(0)

    report = lifecycle.bootstrap(
        ctx,
        quiet=True,
        dry_run=True,
        start=False,
        skip_supply=True,
        skip_install=True,
        min_nodes=3,
        runner=runner,
    )

    venv_python = ctx.layout.venv_python
    deploy = os.path.join(lifecycle._repo_root(), "scripts", "deploy", "__main__.py")
    assert record["argv"] == [
        venv_python,
        deploy,
        "--platform",
        "macos",
        "--dry-run",
        "--no-start",
        "--skip-supply",
        "--skip-install",
        "--min-nodes",
        "3",
    ]
    assert record["cwd"] == lifecycle._repo_root()
    assert record["stdin"] is None
    assert record["stdout"] is None
    assert record["stderr"] is None
    assert record["env"]["MPM_PYTHON"] == venv_python
    assert report.status == "OK"
    assert report.exit_code is None
    assert report.data["deploy"]["exit_code"] == 0


def test_bootstrap_keeps_the_child_exit_code(tmp_path):
    ctx = make_context(tmp_path)
    _record, runner = _record_runner(7)

    report = lifecycle.bootstrap(ctx, quiet=True, runner=runner)

    assert report.status == "FAILED"
    assert report.exit_code == 7
    assert report.data["deploy"]["exit_code"] == 7


def test_bootstrap_requires_the_checkout_deploy_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_repo_root", lambda: str(tmp_path))
    ctx = make_context(tmp_path)

    with pytest.raises(FailClosed) as raised:
        lifecycle.bootstrap(ctx)

    assert "deploy entry" in raised.value.message


def test_bootstrap_requires_root_on_the_system_profile(tmp_path):
    ctx = make_context(tmp_path, system_profile=True, geteuid=lambda: 501)

    with pytest.raises(FailClosed, match="bootstrap requires root"):
        lifecycle.bootstrap(ctx)

    assert not ctx.executor.calls


def test_bootstrap_rejects_a_negative_min_nodes(tmp_path):
    ctx = make_context(tmp_path)
    with pytest.raises(FailClosed) as raised:
        lifecycle.bootstrap(ctx, min_nodes=-1)
    assert "--min-nodes" in raised.value.message


# ---- CLI and wrapper regressions -----------------------------------------


def test_cli_bootstrap_is_not_a_mutating_command():
    assert "bootstrap" not in cli.MUTATING_COMMANDS


def test_cli_bootstrap_forwards_its_flags(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    seen = {}

    def fake_bootstrap(_ctx, **kwargs):
        seen.update(kwargs)
        return lifecycle.Report(command="bootstrap")

    monkeypatch.setattr(lifecycle, "bootstrap", fake_bootstrap)
    code = cli.main(
        [
            "bootstrap",
            "--dry-run",
            "--no-start",
            "--skip-supply",
            "--skip-install",
            "--min-nodes",
            "2",
            "--quiet",
        ],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        context_factory=lambda _args: ctx,
    )

    assert code == 0
    assert seen == {
        "quiet": True,
        "dry_run": True,
        "start": False,
        "skip_supply": True,
        "skip_install": True,
        "min_nodes": 2,
    }


def test_cli_bootstrap_refuses_root(tmp_path):
    ctx = make_context(tmp_path)
    stderr = io.StringIO()

    code = cli.main(
        ["bootstrap", "--root", str(tmp_path)],
        stdout=io.StringIO(),
        stderr=stderr,
        context_factory=lambda _args: ctx,
    )

    assert code == ExitCode.FAIL_CLOSED
    assert "does not accept --root" in stderr.getvalue()


def test_cli_bootstrap_preserves_a_failed_exit_code(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)

    def fake_bootstrap(_ctx, **_kwargs):
        return lifecycle.Report(command="bootstrap", status="FAILED", exit_code=6)

    monkeypatch.setattr(lifecycle, "bootstrap", fake_bootstrap)
    code = cli.main(
        ["bootstrap", "--quiet"],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        context_factory=lambda _args: ctx,
    )
    assert code == 6


def test_bootstrap_wrapper_uses_root_check_and_checkout_cli():
    text = BOOTSTRAP_SH.read_text(encoding="utf-8")
    assert "require_root bootstrap" in text
    assert "run_cli bootstrap" in text
    if os.name != "nt":
        assert os.access(BOOTSTRAP_SH, os.X_OK)

    common = COMMON_SH.read_text(encoding="utf-8")
    assert "install|bootstrap" in common
    assert "MPM_BASE_PYTHON" in common
