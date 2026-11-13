"""LaunchDaemon plist: crash-restart semantics, no shell, no secrets."""

from __future__ import annotations

import plistlib

import pytest

from mpm_common.errors import FailClosed
from mpm_macos import plist

from macos_test_support import PLIST_TEMPLATE

pytestmark = pytest.mark.macos

STATE_DIR = "/Library/Application Support/mihomo-proxy-management"
RENDER_ARGS = {
    "python": "/usr/bin/python3",
    "package_dir": "/usr/local/share/mihomo-proxy-management/python",
    "state_dir": STATE_DIR,
    "config": STATE_DIR + "/config.yaml",
    "binary": "/usr/local/libexec/mihomo-proxy-management/current/mihomo",
    "run_dir": "/var/run/mihomo-proxy-management",
    "log_out": STATE_DIR + "/logs/core.out.log",
    "log_err": STATE_DIR + "/logs/core.err.log",
}


def _rendered() -> str:
    return plist.render_from_file(str(PLIST_TEMPLATE), **RENDER_ARGS)


def test_plist_is_parseable_and_complete():
    document = plistlib.loads(_rendered().encode("utf-8"))
    assert document["Label"] == "com.mihomo-proxy-management.core"
    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert document["ThrottleInterval"] == 5
    assert document["ProcessType"] == "Interactive"
    assert document["WorkingDirectory"] == STATE_DIR
    assert document["StandardOutPath"].endswith("core.out.log")
    assert document["StandardErrorPath"].endswith("core.err.log")


def test_program_arguments_never_use_a_shell_or_caffeinate():
    document = plistlib.loads(_rendered().encode("utf-8"))
    arguments = document["ProgramArguments"]
    assert arguments[0] == "/usr/bin/python3"
    assert "-m" in arguments
    assert "mpm_macos.runner" in arguments
    for token in ("sh", "bash", "zsh", "-c", "caffeinate"):
        assert token not in arguments
    assert "caffeinate" not in _rendered()


def test_plist_contains_no_url_or_secret():
    text = _rendered()
    document = plistlib.loads(text.encode("utf-8"))

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, str):
            yield value

    values = " ".join(walk(document)).lower()
    for token in ("://", "secret", "token", "password", "subscription"):
        assert token not in values
    assert "EnvironmentVariables" in document


def test_keep_alive_audit_rejects_unconditional_restart():
    document = plistlib.loads(_rendered().encode("utf-8"))
    document["KeepAlive"] = True
    with pytest.raises(FailClosed):
        plist.audit(document)


def test_audit_rejects_missing_throttle():
    document = plistlib.loads(_rendered().encode("utf-8"))
    document.pop("ThrottleInterval")
    with pytest.raises(FailClosed):
        plist.audit(document)


def test_audit_rejects_a_shell_entrypoint():
    document = plistlib.loads(_rendered().encode("utf-8"))
    document["ProgramArguments"] = ["/bin/sh", "-c", "mihomo"]
    with pytest.raises(FailClosed):
        plist.audit(document)


def test_audit_rejects_caffeinate_in_program_arguments():
    document = plistlib.loads(_rendered().encode("utf-8"))
    document["ProgramArguments"] = ["/usr/bin/caffeinate", "-i", "/usr/local/bin/mihomo"]
    with pytest.raises(FailClosed):
        plist.audit(document)


def test_audit_text_rejects_a_leaked_url():
    document = plistlib.loads(_rendered().encode("utf-8"))
    document["StandardOutPath"] = "https://example.invalid/log"
    text = plistlib.dumps(document).decode("utf-8")
    with pytest.raises(FailClosed):
        plist.audit_text(text)


def test_audit_rejects_a_host_specific_working_directory():
    """The plist is a macOS document: a Windows path is never acceptable."""
    document = plistlib.loads(_rendered().encode("utf-8"))
    document["WorkingDirectory"] = r"C:\Library\Application Support\mihomo-proxy-management"
    with pytest.raises(FailClosed):
        plist.audit(document)


def test_foreign_label_is_refused():
    with pytest.raises(FailClosed):
        plist.render_from_file(
            str(PLIST_TEMPLATE), label="com.example.other", **RENDER_ARGS
        )


def test_render_is_deterministic():
    assert _rendered() == _rendered()
