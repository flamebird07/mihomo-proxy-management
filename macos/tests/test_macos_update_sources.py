"""Optional subscription sources 2..4 and the ``update-sources`` candidate pool.

Host-free: the converter is injected (its ``main`` recorded), the secret file
lives in a sandbox, and no network is touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mpm_macos import cli, lifecycle, paths as paths_mod

from macos_test_support import make_context

pytestmark = pytest.mark.macos

SECRET_URL = "https://sub.example.invalid/subscription"
SOURCE_2_URL = "https://wmsxwd.example.invalid/api/v1/client/subscribe?token=fake-token"
SECRET_VALUE = "canary-" + "2" * 16


def _sandbox(root: Path) -> paths_mod.Layout:
    return paths_mod.Layout(root=str(root))


def _write_secret_file(layout: paths_mod.Layout, lines: list[str]) -> None:
    os.makedirs(layout.state_dir, exist_ok=True)
    Path(layout.secret_file).write_text("\n".join(lines) + "\n", encoding="utf-8")


class _FakeConverter:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def main(self, argv):
        self.calls.append(list(argv))
        if self.returncode in (0, 5):
            # mimic the converter's atomic success write
            output = argv[argv.index("-o") + 1]
            os.makedirs(os.path.dirname(output), exist_ok=True)
            Path(output).write_text("proxies: []\n", encoding="utf-8")
        return self.returncode


# ---- secrets: extra sources ------------------------------------------------


def test_load_secrets_reads_extra_source_urls_from_file_and_env(tmp_path):
    layout = _sandbox(tmp_path)
    _write_secret_file(
        layout,
        [
            f"SUBSCRIPTION_URL={SECRET_URL}",
            f"SECRET={SECRET_VALUE}",
            "SUBSCRIPTION_INTERVAL=60",
            f"SUBSCRIPTION_URL_1={SECRET_URL}",
            f"SUBSCRIPTION_URL_2={SOURCE_2_URL}",
        ],
    )
    secrets = lifecycle.load_secrets(layout, environ={}, system_profile=False)
    assert secrets.extra_subscription_urls == {2: SOURCE_2_URL}

    secrets = lifecycle.load_secrets(
        layout,
        environ={"MPM_SUBSCRIPTION_URL_2": "https://src2.example.invalid/other"},
        system_profile=False,
    )
    assert secrets.extra_subscription_urls == {2: "https://src2.example.invalid/other"}


def test_save_secrets_preserves_hand_added_sources_and_ua(tmp_path):
    """A hand-added SUBSCRIPTION_URL_2 (e.g. the Nextin/wmsxwd link) survives re-install."""
    layout = _sandbox(tmp_path)
    _write_secret_file(
        layout,
        [
            f"SUBSCRIPTION_URL={SECRET_URL}",
            f"SECRET={SECRET_VALUE}",
            "SUBSCRIPTION_INTERVAL=60",
            f"SUBSCRIPTION_URL_2={SOURCE_2_URL}",
            "SUBSCRIPTION_UA_2=ClashMeta/1.18.0",
        ],
    )
    secrets = lifecycle.Secrets(
        subscription_url=SECRET_URL, controller_secret=SECRET_VALUE, interval_minutes=60
    )
    lifecycle.save_secrets(layout, secrets)

    text = Path(layout.secret_file).read_text(encoding="utf-8")
    assert f"SUBSCRIPTION_URL_2={SOURCE_2_URL}" in text
    assert "SUBSCRIPTION_UA_2=ClashMeta/1.18.0" in text
    # no duplicate keys (the converter's env parser rejects duplicates)
    assert text.count("SUBSCRIPTION_URL_2=") == 1

    # an install that carries a new value replaces the preserved one
    secrets = lifecycle.Secrets(
        subscription_url=SECRET_URL,
        controller_secret=SECRET_VALUE,
        interval_minutes=60,
        extra_subscription_urls={2: "https://src2.example.invalid/new"},
    )
    lifecycle.save_secrets(layout, secrets)
    text = Path(layout.secret_file).read_text(encoding="utf-8")
    assert "SUBSCRIPTION_URL_2=https://src2.example.invalid/new" in text
    assert "SUBSCRIPTION_UA_2=ClashMeta/1.18.0" in text


# ---- update-sources command -------------------------------------------------


def _ctx(tmp_path):
    ctx = make_context(tmp_path)
    os.makedirs(ctx.layout.providers, exist_ok=True)
    return ctx


def test_update_sources_invokes_the_installed_converter_with_the_secret_file(tmp_path):
    ctx = _ctx(tmp_path)
    converter = _FakeConverter(returncode=0)

    report = lifecycle.update_sources(ctx, load_module=lambda _path: converter)

    assert report.status == "OK"
    assert len(converter.calls) == 1
    argv = converter.calls[0]
    assert argv[0] == "--sources-env-file"
    assert argv[1] == ctx.layout.secret_file, "the env *file* is named, never the URL"
    joined = " ".join(argv)
    assert SOURCE_2_URL not in joined and SECRET_URL not in joined
    assert argv[argv.index("-o") + 1] == os.path.join(ctx.layout.providers, "candidate.yaml")
    assert report.data["exit_code"] == 0


def test_update_sources_maps_partial_and_empty_exit_codes(tmp_path):
    ctx = _ctx(tmp_path)
    report = lifecycle.update_sources(
        ctx, load_module=lambda _path: _FakeConverter(returncode=5)
    )
    assert report.status == "DEGRADED"
    assert report.exit_code == 5
    assert cli.emit(ctx, report, as_json=True) == 5
    assert any("exit 5" in warning for warning in report.warnings)

    report = lifecycle.update_sources(
        ctx, load_module=lambda _path: _FakeConverter(returncode=1)
    )
    assert report.status == "FAILED"
    assert report.exit_code == 1
    assert "no candidate pool written" in " ".join(report.messages)


def test_update_sources_fails_closed_on_success_without_a_file(tmp_path):
    """A converter claiming success but writing nothing is a failure, not OK."""
    ctx = _ctx(tmp_path)

    class _Liar:
        def main(self, _argv):
            return 0  # claims success, writes nothing

    with pytest.raises(Exception, match="wrote no candidate file"):
        lifecycle.update_sources(ctx, load_module=lambda _path: _Liar())


def test_update_sources_reports_failure_exception_sanitized(tmp_path):
    ctx = _ctx(tmp_path)

    class _Broken:
        def main(self, _argv):
            raise RuntimeError("boom " + SECRET_URL)  # a leaky converter message

    with pytest.raises(Exception) as excinfo:
        lifecycle.update_sources(ctx, load_module=lambda _path: _Broken())
    assert SECRET_URL not in str(excinfo.value)


def test_update_sources_honours_min_nodes(tmp_path):
    ctx = _ctx(tmp_path)
    converter = _FakeConverter(returncode=0)
    lifecycle.update_sources(ctx, min_nodes=20, load_module=lambda _path: converter)
    argv = converter.calls[0]
    assert argv[argv.index("--min-nodes") + 1] == "20"


def test_update_sources_adds_local_source1_without_overwriting_it(tmp_path):
    ctx = _ctx(tmp_path)
    supplement = Path(ctx.layout.providers) / "nextin.yaml"
    supplement.write_text("proxies: []\n", encoding="utf-8")
    converter = _FakeConverter()
    lifecycle.update_sources(ctx, load_module=lambda _path: converter)
    argv = converter.calls[0]
    assert argv[argv.index("--source1-supplement") + 1] == str(supplement)

    with pytest.raises(lifecycle.FailClosed, match="must not replace"):
        lifecycle.update_sources(ctx, output=str(supplement), load_module=lambda _path: converter)
    assert len(converter.calls) == 1
