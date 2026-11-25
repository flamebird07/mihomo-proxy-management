"""The node pipeline daemon: prerequisites, step argvs, presence flip, plist.

Host-free: subprocesses are injected, the layout is a sandbox, and the config
re-render is monkeypatched at the :mod:`mpm_macos.lifecycle` boundary.
"""

from __future__ import annotations

import os
import plistlib
from contextlib import contextmanager
from pathlib import Path

import pytest

from mpm_macos import lifecycle, node_pipeline, paths as paths_mod, plist as plist_mod

from macos_test_support import LaunchdHarness, make_context, prepare_install

pytestmark = pytest.mark.macos

SECRET_URL = "https://sub.example.invalid/subscription"
SECRET_VALUE = "canary-" + "3" * 16
FEISHU_LINES = (
    f"SUBSCRIPTION_URL={SECRET_URL}",
    f"SECRET={SECRET_VALUE}",
    "SUBSCRIPTION_INTERVAL=60",
    "FEISHU_APP_ID=cli_fake",
    "FEISHU_APP_SECRET=fake_secret",
)


def _sandbox(root: Path) -> paths_mod.Layout:
    return paths_mod.Layout(root=str(root))


def _make_binary(layout: paths_mod.Layout) -> None:
    os.makedirs(os.path.dirname(layout.binary), exist_ok=True)
    with open(layout.binary, "wb") as handle:
        handle.write(b"#!/bin/sh\nexit 0\n")
    os.chmod(layout.binary, 0o755)


def _make_installed_scripts(layout: paths_mod.Layout) -> None:
    os.makedirs(layout.scripts_dir, exist_ok=True)
    for name in (node_pipeline.NODE_PROVIDER_PACKAGE, node_pipeline.NODE_OPS_PACKAGE):
        os.makedirs(os.path.join(layout.scripts_dir, name), exist_ok=True)
        Path(os.path.join(layout.scripts_dir, name, "__init__.py")).write_text("")
    Path(os.path.join(layout.scripts_dir, node_pipeline.SWITCH_SCRIPT)).write_text("# stub\n")


def _write_secret(layout: paths_mod.Layout, lines=FEISHU_LINES) -> None:
    os.makedirs(layout.state_dir, exist_ok=True)
    Path(layout.secret_file).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ready(tmp_path: Path) -> paths_mod.Layout:
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_installed_scripts(layout)
    _write_secret(layout)
    # the deployed host always has the extracted nextin supplement
    os.makedirs(layout.providers, exist_ok=True)
    Path(os.path.join(layout.providers, "nextin.yaml")).write_text(
        "proxies: []\n", encoding="utf-8")
    return layout


# ---- interval ---------------------------------------------------------------


def test_pipeline_interval_defaults_and_clamps():
    assert node_pipeline.pipeline_interval_seconds({}) == 3600
    assert node_pipeline.pipeline_interval_seconds(
        {node_pipeline.PIPELINE_INTERVAL_ENV: "1800"}
    ) == 1800
    assert node_pipeline.pipeline_interval_seconds(
        {node_pipeline.PIPELINE_INTERVAL_ENV: "60"}
    ) == 600
    assert node_pipeline.pipeline_interval_seconds(
        {node_pipeline.PIPELINE_INTERVAL_ENV: "oops"}
    ) == 3600


# ---- prerequisites ----------------------------------------------------------


def test_prerequisites_fail_closed_without_the_installed_binary(tmp_path):
    layout = _sandbox(tmp_path)
    _make_installed_scripts(layout)
    _write_secret(layout)
    code, messages = node_pipeline.prerequisites(layout)
    assert code == node_pipeline.EXIT_PREREQUISITES_MISSING
    assert "mihomo binary missing" in messages[0]


def test_prerequisites_fail_when_packages_or_switch_script_missing(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _write_secret(layout)
    code, messages = node_pipeline.prerequisites(layout)
    assert code == node_pipeline.EXIT_PREREQUISITES_MISSING
    assert "node_provider" in messages[0] and "node_ops" in messages[0]

    _make_installed_scripts(layout)
    Path(os.path.join(layout.scripts_dir, node_pipeline.SWITCH_SCRIPT)).unlink()
    code, messages = node_pipeline.prerequisites(layout)
    assert code == node_pipeline.EXIT_PREREQUISITES_MISSING
    assert "proxy-auto-switch.py" in messages[0]


def test_prerequisites_report_missing_feishu_credentials(tmp_path):
    layout = _sandbox(tmp_path)
    _make_binary(layout)
    _make_installed_scripts(layout)
    _write_secret(layout, lines=FEISHU_LINES[:3])  # no FEISHU_ keys
    code, messages = node_pipeline.prerequisites(layout)
    assert code == node_pipeline.EXIT_PREREQUISITES_MISSING
    assert "FEISHU_APP_ID" in messages[0] and SECRET_VALUE not in messages[0]


def test_prerequisites_pass_when_ready(tmp_path):
    code, messages = node_pipeline.prerequisites(_ready(tmp_path))
    assert code == node_pipeline.EXIT_OK and messages == []


# ---- step argv ---------------------------------------------------------------


def test_build_step_argv_uses_installed_paths_and_carries_no_secret_values(tmp_path):
    layout = _ready(tmp_path)
    steps = dict(zip(node_pipeline.STEP_LABELS, node_pipeline.build_step_argv(layout, binary=layout.binary)))

    collect = steps["collect"]
    assert collect[1:3] == ["-m", "node_provider"]
    assert collect[collect.index("--sources-env") + 1] == layout.secret_file
    assert collect[collect.index("--env-file") + 1] == layout.secret_file
    assert collect[collect.index("--fetch-proxy") + 1] == "http://127.0.0.1:7890"
    assert collect[collect.index("--from-app") + 1] == "source4"
    # supplements landing dir files (sorted) + the legacy nextin.yaml
    assert collect.count("--supplement-yaml") == 1
    assert collect[collect.index("--supplement-yaml") + 1] == os.path.join(
        layout.providers, "nextin.yaml")

    # a file dropped into the landing dir is picked up on the next tick
    os.makedirs(layout.supplements_dir, exist_ok=True)
    dropped = os.path.join(layout.supplements_dir, "wmsxwd-download.yaml")
    with open(dropped, "w", encoding="utf-8") as handle:
        handle.write("proxies: []\n")
    argv2 = node_pipeline.build_step_argv(layout, binary=layout.binary)[0]
    dropped_index = argv2.index("--supplement-yaml")
    assert argv2[dropped_index + 1] == dropped
    assert argv2.count("--supplement-yaml") == 2  # dropped + legacy nextin
    assert collect[collect.index("--lock-file") + 1].startswith(layout.node_state_dir)

    ops = steps["ops"]
    assert ops[1:3] == ["-m", "node_ops"]
    assert ops[ops.index("--core") + 1] == layout.binary

    provide = steps["provide"]
    assert provide[provide.index("--output") + 1] == layout.valid_provider_file
    assert provide[provide.index("--output") + 1].startswith(layout.mihomo_home)



# ---- run_steps ----------------------------------------------------------------


def test_run_steps_runs_every_step_even_when_one_fails(tmp_path):
    layout = _ready(tmp_path)
    seen: list[str] = []

    def runner(argv: list[str]) -> int:
        seen.append(argv[2] if argv[1] == "-m" else argv[1])
        return 1 if len(seen) == 2 else 0  # the ops step fails

    lines: list[str] = []
    code = node_pipeline.run_steps(layout, runner=runner, say=lines.append)

    assert code == node_pipeline.EXIT_STEP_FAILED
    assert seen == ["node_provider", "node_ops", "node_provider"]
    assert any("ops failed" in line for line in lines)


def test_run_steps_ok_when_all_pass(tmp_path):
    layout = _ready(tmp_path)
    code = node_pipeline.run_steps(layout, runner=lambda _argv: 0,
                                   say=lambda _m: None)
    assert code == node_pipeline.EXIT_OK


# ---- presence flip ------------------------------------------------------------


class _Guard:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Ctx:
    def __init__(self):
        self.guard_calls = 0

    def lifecycle_guard(self):
        self.guard_calls += 1
        return _Guard()


def _patch_configure(monkeypatch, recorded: list):
    def fake_configure(_ctx, **_kwargs):
        recorded.append("configure")
        report = lifecycle.Report(command="configure")
        report.status = "CHANGED"
        return report

    monkeypatch.setattr(lifecycle, "configure", fake_configure)
    monkeypatch.setattr(node_pipeline, "_reload_core", lambda *_a, **_k: True)


def test_apply_presence_renders_once_when_valid_provider_appears(tmp_path, monkeypatch):
    layout = _ready(tmp_path)
    recorded: list[str] = []
    _patch_configure(monkeypatch, recorded)
    ctx = _Ctx()
    lines: list[str] = []

    assert node_pipeline.apply_presence(layout, say=lines.append, context_factory=lambda: ctx) is False
    assert recorded == []  # no flip: nothing happened

    os.makedirs(layout.providers, exist_ok=True)
    Path(layout.valid_provider_file).write_text("proxies: []\n", encoding="utf-8")
    assert node_pipeline.apply_presence(layout, say=lines.append, context_factory=lambda: ctx) is True
    assert recorded == ["configure"]
    marker = os.path.join(layout.node_state_dir, node_pipeline.PRESENCE_MARKER)
    assert os.path.isfile(marker)
    assert ctx.guard_calls == 1

    # a second tick with no further flip is a no-op
    assert node_pipeline.apply_presence(layout, say=lines.append, context_factory=lambda: ctx) is False
    assert recorded == ["configure"]


def test_apply_presence_defers_when_the_render_fails(tmp_path, monkeypatch):
    layout = _ready(tmp_path)
    os.makedirs(layout.providers, exist_ok=True)
    Path(layout.valid_provider_file).write_text("proxies: []\n", encoding="utf-8")

    def failing(_ctx, **_kwargs):
        report = lifecycle.Report(command="configure")
        report.status = "FAILED"
        return report

    monkeypatch.setattr(lifecycle, "configure", failing)
    lines: list[str] = []
    assert node_pipeline.apply_presence(layout, say=lines.append, context_factory=lambda: _Ctx()) is False
    assert not os.path.isfile(
        os.path.join(layout.node_state_dir, node_pipeline.PRESENCE_MARKER)
    )
    assert any("re-render failed" in line for line in lines)


def test_apply_presence_retries_after_controller_reload_failure(tmp_path, monkeypatch):
    layout = _ready(tmp_path)
    Path(layout.valid_provider_file).write_text("proxies: []")
    _patch_configure(monkeypatch, [])
    reload_results = iter((False, True))
    monkeypatch.setattr(node_pipeline, "_reload_core", lambda *_a, **_k: next(reload_results))

    assert node_pipeline.apply_presence(layout, context_factory=_Ctx) is False
    assert node_pipeline.apply_presence(layout, context_factory=_Ctx) is True
    assert node_pipeline.apply_presence(layout, context_factory=_Ctx) is False


def test_apply_presence_handles_manual_removal(tmp_path, monkeypatch):
    layout = _ready(tmp_path)
    recorded: list[str] = []
    _patch_configure(monkeypatch, recorded)
    marker = os.path.join(layout.node_state_dir, node_pipeline.PRESENCE_MARKER)
    os.makedirs(layout.node_state_dir, exist_ok=True)
    Path(marker).write_text("active\n", encoding="utf-8")
    # marker present, file gone: flip back to subscription mode
    assert node_pipeline.apply_presence(layout, say=lambda _m: None, context_factory=lambda: _Ctx()) is True
    assert not os.path.isfile(marker)


# ---- main dry-run --------------------------------------------------------------


def test_main_dry_run_prints_prerequisites_and_never_runs(tmp_path, capsys):
    code = node_pipeline.main(["--root", str(tmp_path), "--dry-run"])
    assert code == node_pipeline.EXIT_PREREQUISITES_MISSING
    out = capsys.readouterr().out
    assert "node-pipeline" in out
    assert "argv[collect]" not in out


def test_main_dry_run_ok_prints_step_argv(tmp_path, capsys):
    layout = _ready(tmp_path)
    code = node_pipeline.main(["--root", str(tmp_path), "--dry-run"])
    assert code == node_pipeline.EXIT_OK
    out = capsys.readouterr().out
    assert "node-pipeline argv[collect]" in out
    assert "node-pipeline argv[provide]" in out


# ---- plist ---------------------------------------------------------------------


@pytest.fixture()
def sandbox_templates(tmp_path):
    import shutil

    layout = _sandbox(tmp_path)
    os.makedirs(layout.template_dir, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "templates"
    for name in os.listdir(source):
        shutil.copy(source / name, layout.template_dir)
    return layout


def _render_node(layout: paths_mod.Layout, **overrides) -> str:
    target = layout.to_target_path
    kwargs = dict(
        label=paths_mod.NODE_PIPELINE_LABEL,
        python=target("/usr/bin/python3"),
        package_dir=target(layout.share_python),
        home_dir=target(layout.mihomo_home),
        config=target(layout.config),
        binary=target(layout.binary),
        run_dir=target(layout.run_dir),
        log_out=target(layout.log_node_pipeline_out),
        log_err=target(layout.log_node_pipeline_err),
        sync_interval="3600",
    )
    kwargs.update(overrides)
    return plist_mod.render_from_file(layout.node_pipeline_plist_template, **kwargs)


def test_node_pipeline_plist_renders_and_audits(sandbox_templates):
    document = plistlib.loads(_render_node(sandbox_templates).encode("utf-8"))
    assert document["Label"] == paths_mod.NODE_PIPELINE_LABEL
    assert document["ProgramArguments"][1:] == ["-m", "mpm_macos.node_pipeline"]
    assert document["StartInterval"] == 3600
    assert "RunAtLoad" not in document
    assert "KeepAlive" not in document
    assert document["ProcessType"] == "Background"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update({"RunAtLoad": True}),
        lambda d: d.update({"KeepAlive": True}),
        lambda d: d.update({"StartInterval": 60}),
        lambda d: d.update({"ProcessType": "Interactive"}),
        lambda d: d["ProgramArguments"].__setitem__(2, "mpm_macos.boot"),
    ],
)
def test_node_pipeline_plist_audit_rejects_drift(sandbox_templates, mutation):
    document = plistlib.loads(_render_node(sandbox_templates).encode("utf-8"))
    mutation(document)
    with pytest.raises(Exception, match="plist audit failed"):
        plist_mod.audit(
            plistlib.dumps(document).decode("utf-8"),
            label=paths_mod.NODE_PIPELINE_LABEL,
        )


# ---- stale ops lock + provider refresh ------------------------------------------


def test_stale_ops_lock_removed_when_no_process(tmp_path, monkeypatch):
    layout = _ready(tmp_path)
    os.makedirs(layout.node_state_dir, exist_ok=True)
    lock = os.path.join(layout.node_state_dir, "node_ops.lock")
    Path(lock).write_text("{}")
    os.utime(lock, (0, 0))  # far older than 60s
    monkeypatch.setattr(node_pipeline, "_node_ops_alive", lambda: False)
    lines: list[str] = []
    assert node_pipeline.clear_stale_ops_lock(layout, say=lines.append) is True
    assert not os.path.exists(lock)
    assert any("stale" in line for line in lines)


def test_stale_ops_lock_kept_while_process_alive_or_fresh(tmp_path, monkeypatch):
    layout = _ready(tmp_path)
    os.makedirs(layout.node_state_dir, exist_ok=True)
    lock = os.path.join(layout.node_state_dir, "node_ops.lock")
    Path(lock).write_text("{}")  # fresh: age < 60s
    monkeypatch.setattr(node_pipeline, "_node_ops_alive", lambda: True)
    assert node_pipeline.clear_stale_ops_lock(layout, say=lambda _m: None) is False
    assert os.path.exists(lock)
    os.utime(lock, (0, 0))
    assert node_pipeline.clear_stale_ops_lock(layout, say=lambda _m: None) is False, (
        "a live process keeps even an old lock (fail-closed)")
    assert os.path.exists(lock)


def test_provider_refresh_only_on_content_change(tmp_path):
    layout = _ready(tmp_path)
    os.makedirs(layout.node_state_dir, exist_ok=True)
    os.makedirs(layout.providers, exist_ok=True)
    Path(layout.valid_provider_file).write_text("proxies: []\n", encoding="utf-8")
    calls: list[str] = []

    def reload_provider(_secret: str):
        calls.append("reload")
        return 204

    assert node_pipeline.maybe_refresh_provider(
        layout, say=lambda _m: None, reload_provider=reload_provider) is True
    assert calls == ["reload"]
    # unchanged content: no second reload
    assert node_pipeline.maybe_refresh_provider(
        layout, say=lambda _m: None, reload_provider=reload_provider) is False
    assert calls == ["reload"]
    # changed content: reload again, hash persisted after success
    Path(layout.valid_provider_file).write_text("proxies:\n  - name: x\n", encoding="utf-8")
    assert node_pipeline.maybe_refresh_provider(
        layout, say=lambda _m: None, reload_provider=reload_provider) is True
    assert calls == ["reload", "reload"]


def test_provider_refresh_failure_is_retried_next_tick(tmp_path):
    layout = _ready(tmp_path)
    os.makedirs(layout.node_state_dir, exist_ok=True)
    os.makedirs(layout.providers, exist_ok=True)
    Path(layout.valid_provider_file).write_text("proxies: []\n", encoding="utf-8")
    lines: list[str] = []
    assert node_pipeline.maybe_refresh_provider(
        layout, say=lines.append, reload_provider=lambda _s: None) is False
    assert any("retried" in line for line in lines)
    marker = os.path.join(layout.node_state_dir, "provider.sha256")
    assert not os.path.exists(marker), "hash persisted only after success"


# ---- switch_gpt -----------------------------------------------------------------


class _FakeAPI:
    """Live controller stand-in that records production changes."""

    def __init__(self, members, delays, now="AUTO-FOREIGN", gtype="Selector"):
        self.members = members
        self.delays = delays
        self.now = now
        self.gtype = gtype
        self.puts: list[str] = []

    def json(self, path, method="GET", body=None):
        if path == "/proxies/GPT" and method == "PUT":
            self.now = body["name"]
            self.puts.append(self.now)
            return {}
        if path == "/proxies/GPT":
            return {"type": self.gtype, "now": self.now, "all": list(self.members)}
        assert path.startswith("/group/GPT/delay?")
        assert "expected=200" in path
        return self.delays


@pytest.fixture
def gpt_exits(monkeypatch):
    exits = {}

    @contextmanager
    def session(_layout, names):
        assert all(name in exits for name in names)
        yield exits.get

    monkeypatch.setattr(node_pipeline, "_gpt_probe_session", session)
    return exits


def test_switch_gpt_ignores_advertisement_and_unsupported_members(tmp_path, gpt_exits):
    layout = _ready(tmp_path)
    delays = {"★!!!网不通重新更新订阅": 50, "V1-香港02": 60, "🇭🇰 HK Ristretto": 70,
              "AUTO-FOREIGN": 80, "leaf-not-in-gpt": 90, "V1-美国02": 240}
    fake = _FakeAPI(list(delays)[:-2] + ["V1-美国02"], delays)
    gpt_exits.update({"V1-美国02": "US"})
    assert node_pipeline.switch_gpt(layout, api_json=fake.json) == "V1-美国02"
    assert fake.puts == ["V1-美国02"]


@pytest.mark.parametrize(
    ("us_exit", "sg_exit", "expected"),
    [("US", "SG", "unknown-name"), (None, "SG", "SG Nitro"), ("HK", None, "JP Cappuccino"),
     ("JP", "SG", "SG Nitro")],
)
def test_switch_gpt_prefers_verified_egress_over_latency_and_name(
    tmp_path, gpt_exits, us_exit, sg_exit, expected,
):
    layout = _ready(tmp_path)
    delays = {"SG Nitro": 72, "JP Cappuccino": 137, "US Trace Only": 277, "unknown-name": 300}
    fake = _FakeAPI(list(delays), delays)
    gpt_exits.update({"SG Nitro": sg_exit, "JP Cappuccino": "JP",
                      "US Trace Only": None, "unknown-name": us_exit})
    assert node_pipeline.switch_gpt(layout, api_json=fake.json) == expected
    assert fake.puts == [expected]


def test_switch_gpt_picks_fastest_verified_member_within_region(tmp_path, gpt_exits):
    layout = _ready(tmp_path)
    fake = _FakeAPI(["US slow", "US fast", "US offline"],
                    {"US slow": 500, "US fast": 210, "US offline": 0}, now="US slow")
    gpt_exits.update({"US slow": "US", "US fast": "US"})
    assert node_pipeline.switch_gpt(layout, api_json=fake.json) == "US fast"
    assert fake.puts == ["US fast"]


def test_switch_gpt_keeps_current_when_no_candidate_passes(tmp_path, gpt_exits):
    layout = _ready(tmp_path)
    fake = _FakeAPI(["US offline", "US trace only"], {"US offline": 0, "US trace only": 300})
    gpt_exits["US trace only"] = None
    assert node_pipeline.switch_gpt(layout, api_json=fake.json) is None
    assert fake.puts == []


def test_switch_gpt_skips_non_selector(tmp_path):
    layout = _ready(tmp_path)
    fake = _FakeAPI(["GPT-US"], {}, gtype="Fallback")
    lines: list[str] = []
    assert node_pipeline.switch_gpt(layout, say=lines.append, api_json=fake.json) is None
    assert any("not a selector" in line for line in lines)


def test_switch_gpt_defers_when_live_provider_reload_is_pending(tmp_path):
    layout = _ready(tmp_path)
    Path(layout.valid_provider_file).write_text("proxies: []")
    fake = _FakeAPI(["US fast"], {"US fast": 210})
    lines: list[str] = []
    assert node_pipeline.switch_gpt(layout, say=lines.append, api_json=fake.json) is None
    assert "reload pending" in lines[-1]
    assert fake.puts == []


@pytest.mark.parametrize(
    ("response", "accepted"),
    [(b"HTTP/2 200\r\ncontent-type: text/html\r\n\r\n<body>ChatGPT</body>", True),
     (b"HTTP/2 200\r\nCf-Mitigated: challenge\r\n\r\nchallenge", False),
     (b"HTTP/2 403\r\n\r\nblocked", False),
     (b"HTTP/2 302\r\nlocation: https://example.invalid\r\n\r\nredirect", False)],
)
def test_gpt_probe_http_requires_200_and_rejects_mitigation(
    monkeypatch, response, accepted,
):
    def run(argv, **_kwargs):
        assert "--suppress-connect-headers" in argv
        assert "http://127.0.0.1:17890" in argv
        return node_pipeline.subprocess.CompletedProcess(argv, 0, stdout=response)

    monkeypatch.setattr(node_pipeline.subprocess, "run", run)
    assert bool(node_pipeline._gpt_http_body(17890, "https://chatgpt.com/")) is accepted


@pytest.mark.parametrize(
    ("trace", "homepage", "verified"),
    [(b"loc=US\n", b"<html>ChatGPT</html>", "US"),
     (b"loc=US\n", b"<script>window._cf_chl_opt</script>", None),
     (b"loc=US\n", b"", None),
     (b"loc=HK\n", b"<html>ChatGPT</html>", None),
     (b"", b"<html>ChatGPT</html>", None)],
)
def test_gpt_probe_session_requires_supported_exit_and_unblocked_homepage(
    tmp_path, monkeypatch, trace, homepage, verified,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from node_ops import probe

    layout = _ready(tmp_path)
    Path(layout.valid_provider_file).write_text("proxies: [{name: candidate, type: direct}]")
    monkeypatch.setattr(node_pipeline, "_default_interface", lambda: "en0")

    @contextmanager
    def isolated_session(_self, nodes, names, *, interface):
        assert [node["name"] for node in nodes] == names == ["candidate"]
        assert interface == "en0"
        yield "http://127.0.0.1:17891", 17890

    def http_body(port, url):
        assert port == 17890
        return {node_pipeline.config.GPT_URL: trace, "https://chatgpt.com/": homepage}[url]

    monkeypatch.setattr(probe.IsolatedProber, "session", isolated_session)
    monkeypatch.setattr(probe, "select_and_verify",
                        lambda api, group, name: group == "TEST" and name == "candidate")
    monkeypatch.setattr(node_pipeline, "_gpt_http_body", http_body)
    with node_pipeline._gpt_probe_session(layout, ["candidate"]) as verify:
        assert verify("candidate") == verified


def test_main_switches_against_refreshed_provider(tmp_path, monkeypatch):
    layout = _ready(tmp_path)
    calls = []
    for name in ("clear_stale_ops_lock", "run_steps", "apply_presence",
                 "maybe_refresh_provider", "switch_gpt"):
        monkeypatch.setattr(node_pipeline, name,
                            lambda *_a, step=name, **_k: calls.append(step) or 0)
    assert node_pipeline.main([], context_factory=lambda: type("Ctx", (), {"layout": layout})()) == 0
    assert calls == ["clear_stale_ops_lock", "run_steps", "apply_presence",
                     "maybe_refresh_provider", "switch_gpt"]


# ---- install / uninstall wiring -------------------------------------------------


def _installed(tmp_path, **environ):
    ctx = make_context(tmp_path, environ=environ)
    harness = LaunchdHarness.attached(ctx.executor)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
    return ctx, harness, report


def test_install_registers_the_node_pipeline_job(tmp_path):
    ctx, harness, report = _installed(tmp_path)
    layout = ctx.layout
    assert os.path.isfile(layout.node_pipeline_plist)
    assert os.path.isdir(os.path.join(layout.scripts_dir, "node_provider"))
    assert os.path.isdir(os.path.join(layout.scripts_dir, "node_ops"))
    for name in ("proxy-auto-switch.py", "gpt_switch_entry.py", "gpt_stability_policy.py", "named_mutex_lock.py"):
        assert os.path.isfile(os.path.join(layout.scripts_dir, name)), name
    assert harness.node_loaded is True
    assert harness.node_running is False, "no RunAtLoad: the first tick fires later"
    assert harness.node_disabled is False
    assert any("node-pipeline registered" in message for message in report.messages)
    document = plistlib.loads(Path(layout.node_pipeline_plist).read_bytes())
    assert document["StartInterval"] == 3600


def test_install_honours_the_configured_interval(tmp_path):
    ctx = make_context(tmp_path, environ={"MPM_NODE_PIPELINE_INTERVAL_SECONDS": "1800"})
    LaunchdHarness.attached(ctx.executor)
    prepare_install(ctx)
    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages
    document = plistlib.loads(Path(ctx.layout.node_pipeline_plist).read_bytes())
    assert document["StartInterval"] == 1800


def test_uninstall_removes_the_node_pipeline_job(tmp_path):
    ctx, harness, _report = _installed(tmp_path)
    report = lifecycle.uninstall(ctx, quiet=True)
    assert report.status == "OK"
    assert not os.path.isfile(ctx.layout.node_pipeline_plist)
    assert harness.node_loaded is False
    assert harness.node_disabled is True
