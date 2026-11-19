"""The ``groups`` diagnostic: region match counts, alive members, selection.

Host-free: the provider file, the rendered config and the controller are all
sandbox/fake artifacts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from mpm_macos import config as config_mod, lifecycle

from macos_test_support import CONFIG_TEMPLATE, FakeController, make_context

pytestmark = pytest.mark.macos

SECRET = "canary-" + "1" * 16
URL = "https://sub.example.invalid/subscription"


def _setup(tmp_path, node_names, controller=None):
    ctx = make_context(tmp_path)
    os.makedirs(ctx.layout.providers, exist_ok=True)
    os.makedirs(ctx.layout.state_dir, exist_ok=True)
    Path(ctx.layout.provider_file).write_text(
        yaml.safe_dump(
            {"proxies": [{"name": name, "type": "ss"} for name in node_names]},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    text = config_mod.render(
        CONFIG_TEMPLATE.read_text(encoding="utf-8"),
        secret=SECRET,
        subscription_url=URL,
        interval_minutes=60,
        mode="tun",
    )
    Path(ctx.layout.config).write_text(text, encoding="utf-8")
    ctx.controller_factory = lambda _secret: controller or FakeController()
    return ctx


def test_groups_counts_region_matches_and_reports_alive_state(tmp_path):
    ctx = _setup(
        tmp_path,
        ["香港-01", "HK-02", "美国-01", "新加坡-01", "日本-01"],
        controller=FakeController(
            group_alive={"AUTO-FOREIGN": True, "GPT": True, "GROKBOT": True}
        ),
    )

    report = lifecycle.groups(ctx)

    assert report.status == "OK"
    rows = {row["name"]: row for row in report.data["groups"]}
    assert report.data["provider_nodes"] == 5
    assert rows["GROKBOT-US"]["matched_nodes"] == 1
    assert rows["GROKBOT-SG"]["matched_nodes"] == 1
    assert rows["GROKBOT-JP"]["matched_nodes"] == 1
    assert rows["CODEBUDDY-HK"]["matched_nodes"] == 2
    assert rows["AUTO-FOREIGN"]["alive"] is True
    assert rows["GROKBOT"]["alive"] is True
    # provider-backed groups carry no literal member list
    assert rows["GROKBOT-US"]["proxies"] == []
    # parent groups report their member liveness
    assert rows["GROKBOT"]["members_alive"]["AUTO-FOREIGN"] is True
    assert not [warning for warning in report.warnings if "GROKBOT" in warning]


def test_groups_warns_when_a_region_pool_is_empty(tmp_path):
    ctx = _setup(tmp_path, ["香港-01", "HK-02", "美国-01", "日本-01"])

    report = lifecycle.groups(ctx)

    assert report.status == "OK"
    rows = {row["name"]: row for row in report.data["groups"]}
    assert rows["GROKBOT-SG"]["matched_nodes"] == 0
    assert "COMPATIBLE" in rows["GROKBOT-SG"]["warning"]
    assert any("GROKBOT-SG" in warning for warning in report.warnings)


def test_groups_still_reports_match_counts_when_the_controller_is_down(tmp_path):
    from mpm_common.controller_health import ControllerError

    class _DownController(FakeController):
        def proxies(self, **_kwargs):
            raise ControllerError("controller unreachable")

    ctx = _setup(
        tmp_path,
        ["香港-01", "美国-01", "新加坡-01", "日本-01"],
        controller=_DownController(),
    )

    report = lifecycle.groups(ctx)

    assert report.status == "OK"
    rows = {row["name"]: row for row in report.data["groups"]}
    assert rows["GROKBOT-US"]["matched_nodes"] == 1
    assert "now" not in rows["AUTO-FOREIGN"]
    assert any("controller unavailable" in warning for warning in report.warnings)


def test_groups_reports_the_current_selection(tmp_path):
    class _RecordingController(FakeController):
        def proxies(self, **_kwargs):
            payload = super().proxies()
            payload["proxies"]["AUTO-FOREIGN"]["now"] = "香港-01"
            return payload

    ctx = _setup(tmp_path, ["香港-01", "美国-01", "日本-01"], controller=_RecordingController())

    report = lifecycle.groups(ctx)

    rows = {row["name"]: row for row in report.data["groups"]}
    assert rows["AUTO-FOREIGN"]["now"] == "香港-01"


def test_groups_fails_closed_on_an_unreadable_config(tmp_path):
    ctx = _setup(tmp_path, ["香港-01"])
    Path(ctx.layout.config).write_text("[unclosed", encoding="utf-8")

    with pytest.raises(Exception):
        lifecycle.groups(ctx)
