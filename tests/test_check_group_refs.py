import os
import sys

import pytest

LIB = os.path.join(os.path.dirname(__file__), "..", "scripts", "lib")
sys.path.insert(0, os.path.abspath(LIB))

import check_group_refs as cgr  # noqa: E402


def _cfg(groups, proxies=None, providers=None):
    return {
        "proxies": proxies or [],
        "proxy-groups": groups,
        "proxy-providers": providers or {},
    }


def test_all_valid_leaf_group_and_builtin():
    cfg = _cfg(
        proxies=[{"name": "日本 06-×1"}, {"name": "US-01"}],
        groups=[
            {"name": "AUTO", "proxies": ["日本 06-×1", "US-01"]},
            {"name": "PROXY", "proxies": ["AUTO", "DIRECT", "REJECT"]},
        ],
    )
    assert cgr.find_missing_group_refs(cfg) == []


def test_space_variant_is_dangling():
    # node declared WITH a space; group references it WITHOUT the space.
    cfg = _cfg(
        proxies=[{"name": "日本 06-×1"}],
        groups=[{"name": "PROXY", "proxies": ["日本06-×1"]}],
    )
    missing = cgr.find_missing_group_refs(cfg)
    assert missing == [("PROXY", "日本06-×1")]


def test_space_variant_reverse_is_dangling():
    # node declared WITHOUT space; group references WITH space.
    cfg = _cfg(
        proxies=[{"name": "日本06-×1"}],
        groups=[{"name": "GPT", "proxies": ["日本 06-×1"]}],
    )
    assert cgr.find_missing_group_refs(cfg) == [("GPT", "日本 06-×1")]


def test_multiplication_sign_vs_letter_x():
    # '×' (U+00D7) vs 'x' (U+0078) must not be treated as equal.
    cfg = _cfg(
        proxies=[{"name": "日本06-×1"}],
        groups=[{"name": "PROXY", "proxies": ["日本06-x1"]}],
    )
    assert cgr.find_missing_group_refs(cfg) == [("PROXY", "日本06-x1")]


def test_builtins_always_valid_without_proxies():
    cfg = _cfg(groups=[{"name": "GLOBAL", "proxies": ["DIRECT", "REJECT", "COMPATIBLE", "PASS"]}])
    assert cgr.find_missing_group_refs(cfg) == []


def test_group_referencing_other_group_valid():
    cfg = _cfg(
        groups=[
            {"name": "US-FAST", "proxies": ["DIRECT"]},
            {"name": "PROXY", "proxies": ["US-FAST"]},
        ]
    )
    assert cgr.find_missing_group_refs(cfg) == []


def test_dict_member_form():
    cfg = _cfg(
        proxies=[{"name": "NODE-A"}],
        groups=[{"name": "PROXY", "proxies": [{"name": "NODE-A"}, {"name": "NODE-MISSING"}]}],
    )
    assert cgr.find_missing_group_refs(cfg) == [("PROXY", "NODE-MISSING")]


def test_multiple_missing_across_groups_preserves_pairs():
    cfg = _cfg(
        proxies=[{"name": "OK"}],
        groups=[
            {"name": "G1", "proxies": ["OK", "BAD1"]},
            {"name": "G2", "proxies": ["BAD2"]},
        ],
    )
    assert cgr.find_missing_group_refs(cfg) == [("G1", "BAD1"), ("G2", "BAD2")]


def test_provider_cache_names_are_valid(tmp_path):
    # A group using a proxy-provider may list provider node names; those are
    # learned from the provider cache file so they are NOT false-flagged.
    prov_dir = tmp_path
    cache = prov_dir / "sub.yaml"
    cache.write_text("proxies:\n  - name: PROVIDER-NODE-1\n  - name: PROVIDER-NODE-2\n", encoding="utf-8")
    cfg = _cfg(
        groups=[{"name": "US-FAST", "use": ["sub"], "proxies": ["PROVIDER-NODE-1", "DIRECT"]}],
        providers={"sub": {"path": "./sub.yaml"}},
    )
    assert cgr.find_missing_group_refs(cfg, config_dir=str(prov_dir)) == []


def test_provider_cache_missing_file_still_flags():
    # If the cache file does not exist, provider-only names cannot be validated
    # and are reported as dangling (conservative).
    cfg = _cfg(
        groups=[{"name": "US-FAST", "use": ["sub"], "proxies": ["GHOST-NODE"]}],
        providers={"sub": {"path": "./does-not-exist.yaml"}},
    )
    assert cgr.find_missing_group_refs(cfg, config_dir=".") == [("US-FAST", "GHOST-NODE")]


def test_main_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.yaml"
    good.write_text(
        "proxies:\n  - name: N1\nproxy-groups:\n  - name: G\n    proxies:\n      - N1\n      - DIRECT\n",
        encoding="utf-8",
    )
    assert cgr.main([str(good)]) == 0
    capsys.readouterr()  # clear buffer before the JSON case

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "proxies:\n  - name: 日本 06-×1\nproxy-groups:\n  - name: G\n    proxies:\n      - 日本06-×1\n",
        encoding="utf-8",
    )
    rc = cgr.main([str(bad), "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    import json
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["missing"] == [{"group": "G", "member": "日本06-×1"}]
