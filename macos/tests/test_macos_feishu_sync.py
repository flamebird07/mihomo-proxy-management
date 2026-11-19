"""The Feishu speedtest sync daemon: argv, prerequisites, plist and install wiring.

All host-free: the sync script itself is never executed - ``run_sync`` takes an
injectable module loader, and the daemon's prerequisites are checked against a
sandbox layout.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest
import yaml

from mpm_macos import feishu_sync, lifecycle, paths as paths_mod, plist as plist_mod

from macos_test_support import (
    LaunchdHarness,
    assert_posix_mode,
    make_context,
    prepare_install,
)

pytestmark = pytest.mark.macos

SECRET_URL = "https://sub.example.invalid/subscription"
SECRET_VALUE = "canary-" + "0" * 16


# ---- interval ---------------------------------------------------------------


def test_sync_interval_defaults_and_clamps():
    assert feishu_sync.sync_interval_seconds({}) == 600
    assert feishu_sync.sync_interval_seconds({feishu_sync.SYNC_INTERVAL_ENV: "900"}) == 900
    assert feishu_sync.sync_interval_seconds({feishu_sync.SYNC_INTERVAL_ENV: "10"}) == 300
    assert (
        feishu_sync.sync_interval_seconds({feishu_sync.SYNC_INTERVAL_ENV: "999999"}) == 86400
    )
    assert feishu_sync.sync_interval_seconds({feishu_sync.SYNC_INTERVAL_ENV: "abc"}) == 600


# ---- argv -------------------------------------------------------------------


def _sandbox(root: Path) -> paths_mod.Layout:
    return paths_mod.Layout(root=str(root))


def test_build_sync_argv_uses_installed_paths_and_carries_no_secret_values(tmp_path):
    layout = _sandbox(tmp_path)
    binary = layout.binary
    argv = feishu_sync.build_sync_argv(layout, binary=binary)

    assert "--speedtest-write" not in argv
    assert "--no-speedtest" not in argv
    assert any(value == "--source1-cache" and argv[i + 1] == layout.provider_file
               for i, value in enumerate(argv))
    assert argv[argv.index("--core") + 1] == binary
    assert argv[argv.index("--config") + 1] == layout.config
    assert argv[argv.index("--env-file") + 1] == layout.secret_file
    assert argv[argv.index("--sources-env") + 1] == layout.secret_file
    assert argv[argv.index("--live-env") + 1] == layout.secret_file
    assert argv[argv.index("--log-file") + 1] == os.path.join(layout.logs, "feishu-bitable-sync.log")
    assert argv[argv.index("--lock-file") + 1] == os.path.join(
        layout.feishu_state_dir, "feishu-bitable-sync.lock"
    )
    # values stay in the 0600 file; the command line names only the file
    joined = " ".join(argv)
    assert SECRET_VALUE not in joined
    assert SECRET_URL not in joined
    # every overridden path lives inside the project layout
    for flag in ("--state-file", "--sync-state-file", "--gpt-history-file", "--lock-file"):
        assert argv[argv.index(flag) + 1].startswith(layout.feishu_state_dir)


# ---- prerequisites ----------------------------------------------------------


def _make_binary(layout: paths_mod.Layout) -> None:
    os.makedirs(os.path.dirname(layout.binary), exist_ok=True)
    with open(layout.binary, "wb") as handle:
        handle.write(b"#!/bin/sh\nexit 0\n")
    os.chmod(layout.binary, 0o755)


def _make_sync_script(layout: paths_mod.Layout) -> None:
    os.makedirs(layout.scripts_dir, exist_ok=True)
    with open(layout.sync_script, "w", encoding="utf-8") as handle:
        handle.write("# installed sync script placeholder\n")


def _write_secret_file(layout: paths_mod.Layout, **extra: str) -> None:
    os.makedirs(layout.state_dir, exist_ok=True)
    lines = [
        f"SUBSCRIPTION_URL={SECRET_URL}",
        f"SECRET={SECRET_VALUE}",
        "SUBSCRIPTION_INTERVAL=60",
        *extra.values(),
    ]
    with open(layout.secret_file, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def test_prerequisites_fail_closed_without_the_installed_binary(tmp_path):
    code, messages = feishu_sync.prerequisites(_sandbox(tmp_path), environ={})
    assert code == feishu_sync.EXIT_PREREQUISITES_MISSING
    assert "mihomo binary missing" in messages[0]


def test_prerequisites_fail_when_the_sync_script_is_not_installed(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    code, messages = feishu_sync.prerequisites(layout, environ={})
    assert code == feishu_sync.EXIT_PREREQUISITES_MISSING
    assert "sync-feishu-bitable.py is not installed" in messages[0]


def test_prerequisites_report_missing_credentials_without_crashing(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_sync_script(layout)
    _write_secret_file(layout)
    code, messages = feishu_sync.prerequisites(layout, environ={})
    assert code == feishu_sync.EXIT_CREDENTIALS_MISSING
    joined = "\n".join(messages)
    for key in feishu_sync.CREDENTIAL_KEYS:
        assert key in joined
    # the hint says where the values belong, without containing any value
    assert "~/.hermes/.env" in joined
    assert SECRET_VALUE not in joined


def test_prerequisites_pass_once_credentials_exist(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_sync_script(layout)
    _write_secret_file(
        layout,
        app_id="FEISHU_APP_ID=cli_fake_id",
        app_secret="FEISHU_APP_SECRET=fake_secret",
        token="FEISHU_BITABLE_APP_TOKEN=fake_token",
    )
    code, messages = feishu_sync.prerequisites(layout, environ={})
    assert code == feishu_sync.EXIT_OK, messages
    assert messages == []


# ---- run_sync ---------------------------------------------------------------


class _FakeSyncModule:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def main(self, argv):
        self.calls.append(list(argv))
        return self.returncode


def test_run_sync_invokes_the_installed_script_with_the_built_argv(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_sync_script(layout)
    _write_secret_file(
        layout,
        app_id="FEISHU_APP_ID=cli_fake_id",
        app_secret="FEISHU_APP_SECRET=fake_secret",
        token="FEISHU_BITABLE_APP_TOKEN=fake_token",
    )
    module = _FakeSyncModule(returncode=0)
    lines: list[str] = []

    code = feishu_sync.run_sync(
        layout, environ={}, say=lines.append, load_module=lambda _path: module
    )

    assert code == 0
    assert len(module.calls) == 1
    assert "--speedtest-write" not in module.calls[0]
    assert "--no-speedtest" not in module.calls[0]
    assert lines == []


def test_run_sync_propagates_the_script_exit_code_and_reports_failures(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_sync_script(layout)
    _write_secret_file(
        layout,
        app_id="FEISHU_APP_ID=cli_fake_id",
        app_secret="FEISHU_APP_SECRET=fake_secret",
        token="FEISHU_BITABLE_APP_TOKEN=fake_token",
    )
    module = _FakeSyncModule(returncode=1)
    lines: list[str] = []
    code = feishu_sync.run_sync(
        layout, environ={}, say=lines.append, load_module=lambda _path: module
    )
    assert code == 1

    def broken(_path):
        raise RuntimeError("boom <detail>")

    lines.clear()
    code = feishu_sync.run_sync(
        layout, environ={}, say=lines.append, load_module=broken
    )
    assert code == feishu_sync.EXIT_SYNC_FAILED
    assert any("run failed" in line for line in lines)


def test_run_sync_reports_missing_credentials_and_never_loads_the_script(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_sync_script(layout)
    _write_secret_file(layout)
    lines: list[str] = []

    def unexpected(_path):
        raise AssertionError("the sync script must not load without credentials")

    code = feishu_sync.run_sync(
        layout, environ={}, say=lines.append, load_module=unexpected
    )
    assert code == feishu_sync.EXIT_CREDENTIALS_MISSING
    assert any("FEISHU_BITABLE_APP_TOKEN" in line for line in lines)


def test_main_dry_run_prints_prerequisites_and_never_runs(tmp_path, capsys):
    code = feishu_sync.main(["--root", str(tmp_path), "--dry-run"])
    assert code == feishu_sync.EXIT_PREREQUISITES_MISSING
    out = capsys.readouterr().out
    assert "feishu-sync" in out
    assert "feishu-sync argv:" not in out, "dry-run without prerequisites must not print argv"


def test_main_dry_run_prints_argv_when_prerequisites_pass(tmp_path, capsys):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_sync_script(layout)
    _write_secret_file(
        layout,
        app_id="FEISHU_APP_ID=cli_fake_id",
        app_secret="FEISHU_APP_SECRET=fake_secret",
        token="FEISHU_BITABLE_APP_TOKEN=fake_token",
    )
    code = feishu_sync.main(["--root", str(tmp_path), "--dry-run"])
    assert code == feishu_sync.EXIT_OK
    out = capsys.readouterr().out
    assert "feishu-sync argv: --env-file" in out


# ---- plist template ---------------------------------------------------------


@pytest.fixture()
def sandbox_templates(tmp_path):
    import shutil

    layout = _sandbox(tmp_path)
    os.makedirs(layout.template_dir, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "templates"
    for name in os.listdir(source):
        shutil.copy(source / name, layout.template_dir)
    return layout


def _render_feishu(layout: paths_mod.Layout, **overrides) -> str:
    target = layout.to_target_path
    kwargs = dict(
        label=paths_mod.FEISHU_SYNC_LABEL,
        python=target("/usr/bin/python3"),
        package_dir=target(layout.share_python),
        home_dir=target(layout.mihomo_home),
        config=target(layout.config),
        binary=target(layout.binary),
        run_dir=target(layout.run_dir),
        log_out=target(layout.log_feishu_out),
        log_err=target(layout.log_feishu_err),
        sync_interval="600",
    )
    kwargs.update(overrides)
    return plist_mod.render_from_file(layout.feishu_sync_plist_template, **kwargs)


def test_feishu_sync_plist_renders_and_audits(sandbox_templates):
    text = _render_feishu(sandbox_templates)
    document = plistlib.loads(text.encode("utf-8"))
    assert document["Label"] == paths_mod.FEISHU_SYNC_LABEL
    assert document["ProgramArguments"][1:] == ["-m", "mpm_macos.feishu_sync"]
    assert document["StartInterval"] == 600
    assert "RunAtLoad" not in document
    assert "KeepAlive" not in document
    assert document["ProcessType"] == "Background"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update({"RunAtLoad": True}),
        lambda d: d.update({"KeepAlive": {"SuccessfulExit": False}}),
        lambda d: d.update({"StartInterval": 60}),
        lambda d: d.update({"StartInterval": "600"}),
        lambda d: d.update({"ProcessType": "Interactive"}),
        lambda d: d.update({"ProgramArguments": ["/bin/sh", "-c", "echo hi"]}),
        lambda d: d["ProgramArguments"].__setitem__(2, "mpm_macos.boot"),
    ],
)
def test_feishu_sync_plist_audit_rejects_drift(sandbox_templates, mutation):
    document = plistlib.loads(_render_feishu(sandbox_templates).encode("utf-8"))
    mutation(document)
    with pytest.raises(Exception, match="plist audit failed"):
        plist_mod.audit(
            plistlib.dumps(document).decode("utf-8"),
            label=paths_mod.FEISHU_SYNC_LABEL,
        )


# ---- install / uninstall wiring --------------------------------------------


def _installed(tmp_path, **environ):
    ctx = make_context(tmp_path, environ=environ)
    harness = LaunchdHarness.attached(ctx.executor)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
    return ctx, harness, report


def test_install_registers_the_feishu_sync_job(tmp_path):
    ctx, harness, report = _installed(tmp_path)
    layout = ctx.layout
    assert os.path.isfile(layout.feishu_sync_plist)
    assert os.path.isfile(layout.sync_script), "install copies sync-feishu-bitable.py"
    assert_posix_mode(layout.sync_script, 0o755)
    health_script = Path(layout.scripts_dir) / "health-check-provider.py"
    assert health_script.is_file()
    assert_posix_mode(str(health_script), 0o755)
    assert os.path.isdir(layout.feishu_state_dir)
    assert harness.feishu_loaded is True
    assert harness.feishu_running is False, "no RunAtLoad: the first tick fires later"
    assert harness.feishu_disabled is False
    assert any("feishu-sync" in message for message in report.messages)
    document = plistlib.loads(Path(layout.feishu_sync_plist).read_bytes())
    assert document["StartInterval"] == 600


def test_install_honours_the_configured_sync_interval(tmp_path):
    ctx = make_context(tmp_path, environ={"MPM_FEISHU_SYNC_INTERVAL_SECONDS": "900"})
    LaunchdHarness.attached(ctx.executor)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
    document = plistlib.loads(Path(ctx.layout.feishu_sync_plist).read_bytes())
    assert document["StartInterval"] == 900


def test_uninstall_removes_the_feishu_sync_job(tmp_path):
    ctx, harness, _report = _installed(tmp_path)
    report = lifecycle.uninstall(ctx, quiet=True)
    assert report.status == "OK"
    assert not os.path.isfile(ctx.layout.feishu_sync_plist)
    assert harness.feishu_loaded is False
    assert harness.feishu_disabled is True


def test_install_is_idempotent_for_the_feishu_job(tmp_path):
    ctx, harness, _report = _installed(tmp_path)
    first = Path(ctx.layout.feishu_sync_plist).read_bytes()
    second = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert second.status == "OK"
    assert Path(ctx.layout.feishu_sync_plist).read_bytes() == first
    assert harness.feishu_loaded is True


# ---- secrets: FEISHU keys + the SUBSCRIPTION_URL_1 mirror -------------------


def test_save_secrets_derives_subscription_url_1_and_preserves_feishu_keys(tmp_path):
    layout = _sandbox(tmp_path)
    os.makedirs(layout.state_dir, exist_ok=True)
    _write_secret_file(
        layout,
        app_id="FEISHU_APP_ID=cli_fake_existing",
        app_secret="FEISHU_APP_SECRET=fake_kept_secret",
        token="FEISHU_BITABLE_APP_TOKEN=fake_kept_token",
    )
    secrets = lifecycle.Secrets(
        subscription_url=SECRET_URL, controller_secret=SECRET_VALUE, interval_minutes=60
    )
    lifecycle.save_secrets(layout, secrets)

    values = lifecycle.parse_env(Path(layout.secret_file).read_text(encoding="utf-8"))
    assert values["SUBSCRIPTION_URL_1"] == SECRET_URL
    assert values["FEISHU_APP_ID"] == "cli_fake_existing"
    assert values["FEISHU_APP_SECRET"] == "fake_kept_secret"
    assert values["FEISHU_BITABLE_APP_TOKEN"] == "fake_kept_token"
    assert values["SUBSCRIPTION_URL"] == SECRET_URL
    assert_posix_mode(layout.secret_file, 0o600)

    # a fresh install that carries new credentials replaces them
    updated = lifecycle.Secrets(
        subscription_url=SECRET_URL,
        controller_secret=SECRET_VALUE,
        interval_minutes=60,
        feishu_app_id="cli_fake_new",
    )
    lifecycle.save_secrets(layout, updated)
    values = lifecycle.parse_env(Path(layout.secret_file).read_text(encoding="utf-8"))
    assert values["FEISHU_APP_ID"] == "cli_fake_new"
    assert values["FEISHU_APP_SECRET"] == "fake_kept_secret"


def test_load_secrets_reads_feishu_keys_from_env_and_file(tmp_path):
    layout = _sandbox(tmp_path)
    os.makedirs(layout.state_dir, exist_ok=True)
    _write_secret_file(
        layout,
        app_id="FEISHU_APP_ID=cli_fake_file",
        app_secret="FEISHU_APP_SECRET=fake_file_secret",
        token="FEISHU_BITABLE_APP_TOKEN=fake_file_token",
    )
    secrets = lifecycle.load_secrets(layout, environ={}, system_profile=False)
    assert secrets.feishu_app_id == "cli_fake_file"
    assert secrets.feishu_bitable_app_token == "fake_file_token"

    secrets = lifecycle.load_secrets(
        layout,
        environ={"MPM_FEISHU_APP_ID": "cli_env"},
        system_profile=False,
    )
    assert secrets.feishu_app_id == "cli_env"
    assert secrets.feishu_bitable_app_token == "fake_file_token"
def test_xigua_schedule_is_opt_in_and_removed_with_its_source(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    layout = ctx.layout
    prepare_install(ctx)

    calls = []

    class Job:
        def bootout(self):
            calls.append("bootout")

        def disable(self):
            calls.append("disable")

        def enable(self):
            calls.append("enable")

        def bootstrap(self):
            calls.append("bootstrap")

    monkeypatch.setattr(lifecycle, "_xigua_ippool_launchd", lambda _ctx: Job())
    report = lifecycle.Report(command="install")
    lifecycle._install_xigua_ippool_job(ctx, report)
    assert calls == []

    source = Path(layout.providers) / "xigua-source.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("proxies: []\n", encoding="utf-8")
    source.chmod(0o600)
    lifecycle._install_xigua_ippool_job(ctx, report)
    path = Path(layout.xigua_ippool_plist)
    job = plistlib.loads(path.read_bytes())
    assert job["StartInterval"] == 300 and "RunAtLoad" not in job
    assert "--require-project-core" in job["ProgramArguments"]
    assert calls == ["bootout", "enable", "bootstrap"]

    source.unlink()
    lifecycle._install_xigua_ippool_job(ctx, report)
    assert calls[-2:] == ["bootout", "disable"]
    assert not path.exists()

