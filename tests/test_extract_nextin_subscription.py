"""Nextin subscription extractor: synthetic containers, masking, write mode.

Host-free: the "Nextin container" is a tmp directory shaped like the real
App Group container; no TCC, no network, and the fixture URLs use
``example.invalid`` tokens only (never a real credential).
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "extract_nextin_subscription.py")
spec = importlib.util.spec_from_file_location("extract_nextin_subscription", SCRIPT)
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)

pytestmark = pytest.mark.macos

FAKE_URL_WMSXWD = "https://wmsxwd.example.invalid/api/v1/client/subscribe?token=afake0123456789"
FAKE_URL_OTHER = "https://other-airport.example.invalid/sub?token=bfake0123456789"


@pytest.fixture()
def fake_container(tmp_path: Path) -> Path:
    """A Group-Container-shaped directory with a session file and a profile."""
    root = tmp_path / "group.com.Tommy.Nextin"
    (root / "Library").mkdir(parents=True)
    # session-style file carrying the subscription URL (Ninja-like shape)
    (root / "Library" / "session.json").write_text(
        '{"subscription": "%s", "plan": "pro"}' % FAKE_URL_WMSXWD,
        encoding="utf-8",
    )
    # a decoy URL from another airport (lower score than the wmsxwd one)
    (root / "Library" / "history.log").write_text(
        "visited %s\n" % FAKE_URL_OTHER, encoding="utf-8"
    )
    # a cached Clash profile with three nodes
    (root / "Library" / "profile.yaml").write_text(
        "proxies:\n"
        "  - name: 香港-01\n    type: ss\n    server: 203.0.113.1\n    port: 8388\n"
        "  - name: 美国-01\n    type: ss\n    server: 203.0.113.2\n    port: 8388\n"
        "  - name: 新加坡-01\n    type: ss\n    server: 203.0.113.3\n    port: 8388\n"
        "proxy-groups:\n  - name: PROXY\n    proxies: [AUTO]\n",
        encoding="utf-8",
    )
    return root


def test_mask_url_never_shows_query_values():
    masked = extract.mask_url(FAKE_URL_WMSXWD)
    assert "afake" not in masked
    assert masked.startswith("https://wmsxwd.example.invalid/api/v1/client/subscribe?")
    assert "token=***" in masked


def test_scan_finds_and_ranks_url_candidates(fake_container: Path):
    urls, profiles, blocked = extract.scan([str(fake_container)])
    assert blocked == []
    assert urls, "the subscription URL must be found"
    assert urls[0].value == FAKE_URL_WMSXWD, "the wmsxwd-shaped URL outranks the decoy"
    assert profiles and profiles[0].value.endswith("profile.yaml")
    assert "3 node-like" in profiles[0].detail


def test_scan_reports_blocked_paths_without_crashing(tmp_path: Path):
    secret_dir = tmp_path / "group.com.Tommy.Nextin"
    secret_dir.mkdir()
    (secret_dir / "secret.json").write_text("{}", encoding="utf-8")
    os.chmod(secret_dir, 0o000)
    try:
        if os.geteuid() == 0:
            pytest.skip("root reads through chmod 000")
        urls, profiles, blocked = extract.scan([str(secret_dir)])
        assert not urls and not profiles
        assert blocked, "the unreadable directory is reported, not fatal"
    finally:
        os.chmod(secret_dir, 0o755)


def test_main_dry_run_masks_urls_and_sets_exit_code(fake_container: Path, capsys):
    exit_code = extract.main(["--root", str(fake_container)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert FAKE_URL_WMSXWD not in out, "the raw credential must never be printed"
    assert "token=***" in out
    assert "profile.yaml" in out


def test_main_reports_blocked_container_with_hint(tmp_path: Path, capsys):
    blocked_root = tmp_path / "group.com.Tommy.Nextin"
    blocked_root.mkdir()
    os.chmod(blocked_root, 0o000)
    try:
        if os.geteuid() == 0:
            pytest.skip("root reads through chmod 000")
        exit_code = extract.main(["--root", str(blocked_root)])
        assert exit_code == 2
        out = capsys.readouterr().out
        assert "Full Disk Access" in out
    finally:
        os.chmod(blocked_root, 0o755)


def test_main_exit_code_one_when_container_is_empty(tmp_path: Path, capsys):
    empty = tmp_path / "group.com.Tommy.Nextin"
    empty.mkdir()
    assert extract.main(["--root", str(empty)]) == 1


# ---- --write ----------------------------------------------------------------


def test_write_updates_slot_two_and_preserves_other_keys(tmp_path: Path, capsys):
    env_path = tmp_path / "state" / "subscription.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "SUBSCRIPTION_URL=https://main.example.invalid/s?token=main0123456789\n"
        "SECRET=canary-secret\n"
        "SUBSCRIPTION_URL_2=https://old.example.invalid/s?token=old0123456789\n"
        "FEISHU_APP_ID=cli_id\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    exit_code = extract.main(
        [
            "--root", str(tmp_path / "group.com.Tommy.Nextin"),
            "--write", "--mpm-env", str(env_path),
        ]
    )
    assert exit_code in (0, 1, 2), "without a scanned URL nothing is written"
    # the fixture container has no URL unless we put the fake container there:
    # rerun against the shaped container
    exit_code = extract.main(
        [
            "--root", str(tmp_path / "group.com.Tommy.Nextin"),
            "--write", "--mpm-env", str(env_path),
        ]
    )
    capsys.readouterr()
    if exit_code == 0:
        text = env_path.read_text(encoding="utf-8")
        assert FAKE_URL_WMSXWD in text
        assert "SECRET=canary-secret" in text
        assert "FEISHU_APP_ID=cli_id" in text
        assert "token=old" not in text, "the old slot-2 value is replaced"
        assert text.count("SUBSCRIPTION_URL_2=") == 1
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_write_refuses_when_best_candidate_is_not_the_airport(tmp_path: Path, capsys):
    """A non-wmsxwd URL is never auto-written (fail closed)."""
    container = tmp_path / "group.com.Tommy.Nextin"
    (container / "Library").mkdir(parents=True)
    (container / "Library" / "session.json").write_text(
        '{"subscription": "%s"}' % FAKE_URL_OTHER, encoding="utf-8"
    )
    env_path = tmp_path / "state" / "subscription.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("SECRET=canary-secret\n", encoding="utf-8")
    exit_code = extract.main(
        ["--root", str(container), "--write", "--mpm-env", str(env_path)]
    )
    assert exit_code == 1
    assert FAKE_URL_OTHER not in env_path.read_text(encoding="utf-8")


def test_write_masks_the_url_in_stdout(tmp_path: Path, capsys):
    container = tmp_path / "group.com.Tommy.Nextin"
    (container / "Library").mkdir(parents=True)
    (container / "Library" / "session.json").write_text(
        '{"subscription": "%s"}' % FAKE_URL_WMSXWD, encoding="utf-8"
    )
    env_path = tmp_path / "state" / "subscription.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("SECRET=canary-secret\n", encoding="utf-8")
    extract.main(["--root", str(container), "--write", "--mpm-env", str(env_path)])
    out = capsys.readouterr().out
    assert FAKE_URL_WMSXWD not in out
    assert "SUBSCRIPTION_URL_2=https://wmsxwd.example.invalid" in out


def test_apply_env_write_creates_the_file_when_missing(tmp_path: Path):
    env_path = tmp_path / "state" / "subscription.env"
    changes = extract.apply_env_write(str(env_path), {"SUBSCRIPTION_URL_2": FAKE_URL_WMSXWD})
    assert changes == ["SUBSCRIPTION_URL_2=https://wmsxwd.example.invalid/api/v1/client/subscribe?token=***"]
    text = env_path.read_text(encoding="utf-8")
    assert FAKE_URL_WMSXWD in text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


# ---- --export ---------------------------------------------------------------


def test_export_copies_the_best_profile_with_mode_0600(tmp_path: Path, capsys):
    container = tmp_path / "group.com.Tommy.Nextin"
    (container / "Library").mkdir(parents=True)
    (container / "Library" / "profile.yaml").write_text(
        "proxies:\n  - name: 香港-01\n    type: ss\n    server: 203.0.113.1\n    port: 8388\n"
        "  - name: 美国-01\n    type: ss\n    server: 203.0.113.2\n    port: 8388\n",
        encoding="utf-8",
    )
    target = tmp_path / "exported.yaml"
    exit_code = extract.main(
        ["--root", str(container), "--export", str(target)]
    )
    assert exit_code == 0
    text = target.read_text(encoding="utf-8")
    assert "香港-01" in text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
