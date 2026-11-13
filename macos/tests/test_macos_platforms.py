"""Platform facts are injectable and never read the host in these tests."""

from __future__ import annotations

import os

import pytest

from mpm_common.platforms import (
    PlatformFacts,
    current_user_is_root,
    detect,
    normalize_arch,
    normalize_system,
)

pytestmark = pytest.mark.macos


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("arm64", "arm64"),
        ("aarch64", "arm64"),
        ("ARM64", "arm64"),
        ("arm64e", "arm64"),
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("i386", "x86"),
        ("", "unknown"),
        ("riscv64", "riscv64"),
    ],
)
def test_arch_normalisation(raw, expected):
    assert normalize_arch(raw) == expected


def test_system_normalisation():
    assert normalize_system("Darwin") == "darwin"
    assert normalize_system("") == "unknown"


def test_detect_uses_injected_values_only():
    facts = detect(
        system="Darwin",
        machine="aarch64",
        is_root=True,
        which=lambda name: f"/opt/{name}" if name == "mihomo" else None,
        release="14.5",
        commands=("mihomo", "launchctl"),
    )
    assert facts.system == "darwin"
    assert facts.machine == "arm64"
    assert facts.raw_machine == "aarch64"
    assert facts.is_root is True
    assert facts.has_command("mihomo")
    assert not facts.has_command("launchctl")
    assert facts.command("mihomo") == "/opt/mihomo"
    assert facts.release_major() == 14


def test_detect_linux_branch():
    facts = detect(system="Linux", machine="x86_64", is_root=False, which=lambda _n: None)
    assert facts.system == "linux"
    assert facts.machine == "x86_64"
    assert facts.is_windows is False
    assert facts.is_darwin is False


def test_windows_defaults_are_not_root():
    # A Windows host has no os.geteuid; the safe default is "not root", never 0.
    facts = PlatformFacts(system="windows", machine="x86_64")
    assert facts.is_root is False
    assert facts.is_windows is True
    assert detect(system="windows", is_root=None, which=lambda _n: None).is_root == current_user_is_root()


def test_current_user_is_root_matches_geteuid():
    expected = getattr(os, "geteuid", lambda: -1)() == 0
    assert current_user_is_root() is expected


def test_to_dict_does_not_leak_paths():
    facts = PlatformFacts(
        system="darwin",
        machine="arm64",
        commands={"mihomo": "/secret/location/mihomo", "networksetup": None},
    )
    payload = facts.to_dict()
    assert payload["commands"] == {"mihomo": True, "networksetup": False}
    assert "secret" not in str(payload)
