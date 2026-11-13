"""Darwin preflight decisions - fully host-free, no TUN device probing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mpm_common.errors import Unsupported
from mpm_common.platforms import PlatformFacts
from mpm_macos import preflight

from macos_test_support import darwin_facts

pytestmark = pytest.mark.macos


def test_darwin_arm64_root_is_supported():
    check = preflight.evaluate(darwin_facts())
    assert check.supported is True
    assert check.mode == "tun"
    assert check.reasons == []
    assert all(check.commands.values())


def test_x86_64_is_rejected():
    check = preflight.evaluate(darwin_facts(machine="x86_64", raw_machine="x86_64"))
    assert check.supported is False
    assert check.mode == "mixed"
    assert any("arm64" in reason for reason in check.reasons)


def test_non_darwin_is_rejected():
    check = preflight.evaluate(darwin_facts(system="linux"))
    assert check.supported is False
    assert any("Darwin" in reason for reason in check.reasons)


def test_non_root_is_rejected():
    check = preflight.evaluate(darwin_facts(is_root=False))
    assert check.supported is False
    assert any("root" in reason for reason in check.reasons)


def test_missing_required_command_is_fatal():
    commands = {name: f"/usr/bin/{name}" for name in preflight.REQUIRED_COMMANDS}
    commands.pop("networksetup")
    facts = PlatformFacts(system="darwin", machine="arm64", is_root=True, commands=commands)
    check = preflight.evaluate(facts)
    assert check.supported is False
    assert any("networksetup" in reason for reason in check.reasons)


def test_missing_optional_command_is_only_a_warning():
    commands = {name: f"/usr/bin/{name}" for name in preflight.REQUIRED_COMMANDS}
    facts = PlatformFacts(system="darwin", machine="arm64", is_root=True, commands=commands)
    check = preflight.evaluate(facts)
    assert check.supported is True
    assert any("caffeinate" in warning for warning in check.warnings)


def test_unknown_release_is_a_warning_not_an_error():
    check = preflight.evaluate(darwin_facts(release=""))
    assert check.supported is True
    assert any("version unknown" in warning for warning in check.warnings)


def test_old_release_is_a_warning_not_an_error():
    check = preflight.evaluate(darwin_facts(release="11.7"))
    assert check.supported is True
    assert any("untested" in warning for warning in check.warnings)


def test_strict_mode_raises_unsupported():
    with pytest.raises(Unsupported):
        preflight.preflight(darwin_facts(is_root=False))


def test_no_tun_device_probe(monkeypatch):
    """macOS uses the utun API; a /dev/net/tun probe must never happen."""

    def boom(*_args, **_kwargs):
        raise AssertionError("the Linux TUN device node must not be probed on Darwin")

    monkeypatch.setattr(os.path, "exists", boom)
    monkeypatch.setattr(os.path, "isdir", boom)
    check = preflight.evaluate(darwin_facts())
    assert check.supported is True


def test_source_never_mentions_the_linux_tun_device():
    text = Path(preflight.__file__).read_text(encoding="utf-8")
    assert "/dev/net/tun" not in text
