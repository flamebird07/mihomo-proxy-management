"""Shared install context: CLI, mpm-switch and boot agree on one absolute binary.

Regression for the 2026-11-15 ``launchctl kickstart`` failure: the boot entry
built its facts from a bare PATH lookup while the CLI registered the installed
``Layout.binary``, so preflight refused with "required command not found:
mihomo" under the LaunchDaemon's minimal PATH.  Every test here is host-free:
facts are injected and binaries are throwaway files under ``tmp_path``.
"""

from __future__ import annotations

import argparse
import io
import os
import plistlib
from pathlib import Path

import pytest

from mpm_common.errors import ExitCode, FailClosed, Unsupported
from mpm_macos import cli, context, lifecycle, preflight, switch
from mpm_macos.paths import Layout

from macos_test_support import (
    LAUNCHD_PATH,
    FakeController,
    LaunchdHarness,
    darwin_facts,
    launchd_facts,
    make_context,
    prepare_install,
    write_provider,
)

pytestmark = pytest.mark.macos


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _cli_context() -> lifecycle.Context:
    return cli.make_context(
        argparse.Namespace(root=None), stdout=io.StringIO(), stderr=io.StringIO()
    )


# ---- one context for every entry ------------------------------------------


def test_cli_switch_and_boot_contexts_agree_under_the_launchd_path(tmp_path, monkeypatch):
    layout = Layout(str(tmp_path))
    _write_executable(Path(layout.binary))
    monkeypatch.setattr(preflight, "collect_facts", launchd_facts)
    monkeypatch.setattr(os, "environ", {"PATH": LAUNCHD_PATH, "MPM_ROOT": str(tmp_path)})

    cli_ctx = _cli_context()
    switch_ctx = switch.build_context()
    boot_ctx = context.build_context()

    assert cli_ctx.facts.commands["mihomo"] == layout.binary
    assert cli_ctx.facts == switch_ctx.facts == boot_ctx.facts
    assert cli_ctx.layout == switch_ctx.layout == boot_ctx.layout
    check = preflight.evaluate(boot_ctx.facts)
    assert check.supported is True, check.reasons
    assert check.commands["mihomo"] is True


def test_installed_binary_wins_over_env_override_and_path(tmp_path):
    layout = Layout(str(tmp_path))
    _write_executable(Path(layout.binary))
    other = _write_executable(tmp_path / "elsewhere" / "mihomo")
    path_dir = tmp_path / "path-bin"
    _write_executable(path_dir / "mihomo")
    env = {"PATH": f"{path_dir}{os.pathsep}{LAUNCHD_PATH}", context.BINARY_ENV: str(other)}

    facts = context.register_binary(darwin_facts(), layout)

    assert facts.commands["mihomo"] == layout.binary
    # the override is a valid install source, yet never the runtime capability
    assert context.resolve_install_source(env) == str(other)


# ---- fail closed -----------------------------------------------------------


def test_missing_binary_drops_the_path_entry_and_fails_closed(tmp_path, monkeypatch):
    # collect_facts "found" mihomo on the interactive PATH; nothing is installed.
    monkeypatch.setattr(preflight, "collect_facts", darwin_facts)
    monkeypatch.setattr(os, "environ", {"PATH": LAUNCHD_PATH, "MPM_ROOT": str(tmp_path)})

    for ctx in (_cli_context(), switch.build_context(), context.build_context()):
        assert "mihomo" not in ctx.facts.commands
        check = preflight.evaluate(ctx.facts)
        assert check.supported is False
        assert preflight.MIHOMO_MISSING_REASON in check.reasons


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bits are not emulated on Windows")
def test_non_executable_installed_binary_fails_closed(tmp_path):
    layout = Layout(str(tmp_path))
    binary = Path(layout.binary)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o644)

    facts = context.register_binary(darwin_facts(), layout)

    assert "mihomo" not in facts.commands
    assert preflight.evaluate(facts).supported is False


def test_directory_at_the_binary_path_is_not_a_binary(tmp_path):
    layout = Layout(str(tmp_path))
    Path(layout.binary).mkdir(parents=True)

    assert context.resolve_runtime_binary(layout) == ""


def test_unusable_override_fails_closed(tmp_path):
    layout = Layout(str(tmp_path))
    env = {context.BINARY_ENV: str(tmp_path / "missing-mihomo")}

    facts = context.register_binary(launchd_facts(), layout)

    assert "mihomo" not in facts.commands
    assert preflight.evaluate(facts).supported is False
    assert context.resolve_install_source(env) == ""


# ---- the install-source override -------------------------------------------


def test_override_is_an_install_source_not_a_runtime_capability(tmp_path, monkeypatch):
    """A valid override alone must never satisfy the runtime check.

    The core runner executes ``layout.binary``; registering the override would
    let preflight/``mihomo -t`` pass on one file while the start runs another.
    """
    layout = Layout(str(tmp_path))
    source = _write_executable(tmp_path / "source-mihomo")
    monkeypatch.chdir(tmp_path)

    for override in (str(source), "source-mihomo"):
        facts = context.register_binary(launchd_facts(), layout)
        assert "mihomo" not in facts.commands
        assert preflight.evaluate(facts).supported is False
        assert context.resolve_runtime_binary(layout) == ""
        # the same value still resolves as the install source
        assert context.resolve_install_source({context.BINARY_ENV: override}) == str(source)


# ---- verification and start use the same file ------------------------------


def _add_geodata(directory: Path) -> None:
    for name in ("GeoIP.dat", "GeoSite.dat"):
        (directory / name).write_bytes(b"geo-" + name.encode())


def test_configure_validates_the_installed_binary_not_the_override(tmp_path):
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)
    _add_geodata(Path(ctx.environ["MPM_MIHOMO_BINARY"]).parent)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    other = _write_executable(tmp_path / "other-mihomo")
    ctx.environ["MPM_MIHOMO_BINARY"] = str(other)
    ctx.executor.calls.clear()

    assert lifecycle.configure(ctx, quiet=True).status in ("CHANGED", "UNCHANGED")
    assert ctx.executor.find(ctx.layout.binary, "-t") is not None
    assert ctx.executor.find(str(other), "-t") is None


def test_configure_before_install_fails_closed_instead_of_validating_the_source(tmp_path):
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)
    source = Path(ctx.environ["MPM_MIHOMO_BINARY"])
    home = Path(ctx.layout.mihomo_home)
    home.mkdir(parents=True, exist_ok=True)
    _add_geodata(home)
    # configure needs provider nodes; seed them without running a full install
    ctx.converter(ctx.environ["MPM_SUBSCRIPTION_URL"], ctx.layout.provider_file)

    with pytest.raises(FailClosed):
        lifecycle.configure(ctx, quiet=True)

    # the install source is never executed in the runtime's place
    assert ctx.executor.find(str(source), "-t") is None
    assert ctx.executor.find(ctx.layout.binary, "-t") is None
    assert not os.path.exists(ctx.layout.config)


def test_installed_plists_inject_the_absolute_installed_binary(tmp_path):
    ctx = make_context(tmp_path)
    prepare_install(ctx)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    expected = ctx.layout.to_target_path(ctx.layout.binary)
    for path in (ctx.layout.plist, ctx.layout.boot_plist):
        document = plistlib.loads(Path(path).read_bytes())
        assert document["EnvironmentVariables"][context.BINARY_ENV] == expected
    core = plistlib.loads(Path(ctx.layout.plist).read_bytes())
    arguments = core["ProgramArguments"]
    assert arguments[arguments.index("--binary") + 1] == expected


# ---- install-source resolution (separate from the runtime capability) ------


def test_install_source_override_wins_over_path(tmp_path):
    override = _write_executable(tmp_path / "override-mihomo")
    path_dir = tmp_path / "path-bin"
    _write_executable(path_dir / "mihomo")
    env = {"PATH": str(path_dir), context.BINARY_ENV: str(override)}

    assert context.resolve_install_source(env) == str(override)


@pytest.mark.skipif(os.name == "nt", reason="shutil.which applies PATHEXT on Windows")
def test_install_source_uses_the_context_path_not_the_process_path(tmp_path, monkeypatch):
    path_dir = tmp_path / "path-bin"
    source = _write_executable(path_dir / "mihomo")
    monkeypatch.setattr(os, "environ", {"PATH": str(tmp_path / "not-on-the-context")})

    assert context.resolve_install_source({"PATH": str(path_dir)}) == str(source)


@pytest.mark.skipif(os.name == "nt", reason="shutil.which applies PATHEXT on Windows")
def test_install_source_without_path_never_falls_back_to_the_process_path(tmp_path, monkeypatch):
    path_dir = tmp_path / "path-bin"
    _write_executable(path_dir / "mihomo")
    monkeypatch.setattr(os, "environ", {"PATH": str(path_dir)})

    assert context.resolve_install_source({}) == ""


def test_install_source_relative_override_becomes_absolute(tmp_path, monkeypatch):
    source = _write_executable(tmp_path / "source-mihomo")
    monkeypatch.chdir(tmp_path)

    assert context.resolve_install_source({context.BINARY_ENV: "source-mihomo"}) == str(source)


def test_unusable_install_sources_resolve_to_nothing(tmp_path):
    assert context.resolve_install_source({}) == ""
    assert context.resolve_install_source({"PATH": ""}) == ""
    assert (
        context.resolve_install_source({context.BINARY_ENV: str(tmp_path / "missing")}) == ""
    )
    directory = tmp_path / "dir-mihomo"
    directory.mkdir()
    assert context.resolve_install_source({context.BINARY_ENV: str(directory)}) == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bits are not emulated on Windows")
def test_non_executable_install_sources_resolve_to_nothing(tmp_path):
    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    plain = path_dir / "mihomo"
    plain.write_bytes(b"#!/bin/sh\nexit 0\n")
    plain.chmod(0o644)

    assert context.resolve_install_source({"PATH": str(path_dir)}) == ""
    assert context.resolve_install_source({context.BINARY_ENV: str(plain)}) == ""


# ---- first install from a PATH-only source (Codex path1 regression) ---------


def _path_only_context(tmp_path):
    """Fresh-install inputs: no override, nothing installed, mihomo on PATH only.

    The runtime facts are the LaunchDaemon view (no mihomo capability): the
    shared context never registers a PATH find, so ``install`` must resolve
    the source itself instead of being refused by the platform preflight.
    """
    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    source = _write_executable(path_dir / "mihomo")
    _add_geodata(path_dir)
    ctx = make_context(
        tmp_path,
        environ={"PATH": str(path_dir)},
        facts=launchd_facts(),
        validator=None,
    )
    ctx.converter = write_provider
    ctx.controller_factory = lambda _secret: FakeController()
    return ctx, source


def _assert_plists_use_the_installed_binary(ctx, source) -> None:
    expected = ctx.layout.to_target_path(ctx.layout.binary)
    assert expected != ctx.layout.to_target_path(str(source))
    for path in (ctx.layout.plist, ctx.layout.boot_plist):
        text = Path(path).read_text(encoding="utf-8")
        document = plistlib.loads(text.encode("utf-8"))
        assert document["EnvironmentVariables"][context.BINARY_ENV] == expected
        assert str(source) not in text
    core = plistlib.loads(Path(ctx.layout.plist).read_bytes())
    arguments = core["ProgramArguments"]
    assert arguments[arguments.index("--binary") + 1] == expected


@pytest.mark.skipif(os.name == "nt", reason="shutil.which applies PATHEXT on Windows")
def test_first_install_from_path_only_succeeds_and_uses_the_installed_file(tmp_path):
    ctx, source = _path_only_context(tmp_path)
    assert "mihomo" not in ctx.facts.commands
    assert context.resolve_runtime_binary(ctx.layout) == ""
    assert not os.path.exists(ctx.layout.binary)

    report = lifecycle.install(ctx, quiet=True, skip_start=True)
    assert report.status == "OK", report.messages

    assert Path(ctx.layout.binary).read_bytes() == source.read_bytes()
    # the runtime capability is re-registered as the installed absolute path
    assert ctx.facts.commands["mihomo"] == ctx.layout.binary
    assert preflight.evaluate(ctx.facts).supported is True
    # config verification ran against the installed file, never the PATH source
    assert ctx.executor.find(ctx.layout.binary, "-t") is not None
    assert ctx.executor.find(str(source), "-t") is None
    _assert_plists_use_the_installed_binary(ctx, source)


@pytest.mark.skipif(os.name == "nt", reason="shutil.which applies PATHEXT on Windows")
def test_first_install_from_path_only_starts_the_installed_file(tmp_path):
    ctx, source = _path_only_context(tmp_path)
    observations = iter(
        (
            lifecycle.TunObservation(("en0",), "en0"),
            lifecycle.TunObservation(("en0", "utun7"), "utun7"),
        )
    )
    ctx.tun_observer = lambda: next(observations)

    report = lifecycle.install(ctx, quiet=True)  # includes the start
    assert report.status == "OK", report.messages

    harness = LaunchdHarness.attached(ctx.executor)
    assert harness.running is True
    assert harness.disabled is True, "the core must not come back at the next boot"
    assert ctx.facts.commands["mihomo"] == ctx.layout.binary
    assert ctx.executor.find(ctx.layout.binary, "-t") is not None
    assert ctx.executor.find(str(source), "-t") is None
    _assert_plists_use_the_installed_binary(ctx, source)


@pytest.mark.skipif(os.name == "nt", reason="shutil.which applies PATHEXT on Windows")
def test_reinstall_keeps_the_installed_binary_when_the_source_disappears(tmp_path):
    ctx, _source = _path_only_context(tmp_path)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"

    ctx.environ["PATH"] = ""
    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert "mihomo binary installed" not in report.messages
    assert ctx.facts.commands["mihomo"] == ctx.layout.binary


def test_install_source_override_wins_over_path_during_install(tmp_path):
    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    _write_executable(path_dir / "mihomo").write_bytes(b"path-payload")
    _add_geodata(path_dir)
    override = _write_executable(tmp_path / "override-mihomo")
    override.write_bytes(b"override-payload")
    _add_geodata(override.parent)
    ctx = make_context(
        tmp_path,
        environ={"PATH": str(path_dir), context.BINARY_ENV: str(override)},
        facts=launchd_facts(),
        validator=None,
    )
    ctx.converter = write_provider
    ctx.controller_factory = lambda _secret: FakeController()

    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"
    assert Path(ctx.layout.binary).read_bytes() == b"override-payload"
    assert ctx.facts.commands["mihomo"] == ctx.layout.binary


def test_install_without_any_source_fails_closed_before_writing(tmp_path):
    ctx = make_context(tmp_path, facts=launchd_facts(), validator=None)
    ctx.environ["PATH"] = ""

    with pytest.raises(FailClosed) as excinfo:
        lifecycle.install(ctx, quiet=True, skip_start=True)

    assert "MPM_MIHOMO_BINARY" in str(excinfo.value)
    assert not os.path.exists(ctx.layout.binary)
    assert not os.path.exists(ctx.layout.secret_file)
    assert not os.path.exists(ctx.layout.state_dir)
    assert not os.path.exists(ctx.layout.plist)


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bits are not emulated on Windows")
def test_unusable_override_fails_closed_even_with_a_path_candidate(tmp_path):
    ctx, _source = _path_only_context(tmp_path)
    bad = tmp_path / "bad-mihomo"
    bad.write_bytes(b"#!/bin/sh\nexit 0\n")
    bad.chmod(0o644)
    ctx.environ[context.BINARY_ENV] = str(bad)

    with pytest.raises(FailClosed):
        lifecycle.install(ctx, quiet=True, skip_start=True)

    assert not os.path.exists(ctx.layout.binary)
    assert not os.path.exists(ctx.layout.state_dir)


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bits are not emulated on Windows")
def test_non_executable_path_entry_is_not_an_install_source(tmp_path):
    ctx, source = _path_only_context(tmp_path)
    source.chmod(0o644)

    with pytest.raises(FailClosed):
        lifecycle.install(ctx, quiet=True, skip_start=True)

    assert not os.path.exists(ctx.layout.binary)
    assert not os.path.exists(ctx.layout.state_dir)


def test_install_still_rejects_a_host_missing_required_commands(tmp_path):
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)
    commands = {
        name: path
        for name, path in ctx.facts.commands.items()
        if name != "networksetup"
    }
    ctx.facts = darwin_facts(commands=commands)

    with pytest.raises(Unsupported):
        lifecycle.install(ctx, quiet=True, skip_start=True)

    assert not os.path.exists(ctx.layout.binary)
    assert not os.path.exists(ctx.layout.state_dir)


# ---- broken install: the override must not stand in for the runtime binary --
#
# Codex runtime-fork regression: layout.binary (B) missing or non-executable,
# MPM_MIHOMO_BINARY pointing at a valid different file (A), core plist already
# installed with --binary=B.  preflight/start/configure must fail closed: they
# may neither execute nor validate A, and start may not bootstrap the core.


def _broken_install(tmp_path, broken):
    ctx = make_context(tmp_path, validator=None)
    prepare_install(ctx)
    _add_geodata(Path(ctx.environ["MPM_MIHOMO_BINARY"]).parent)
    assert lifecycle.install(ctx, quiet=True, skip_start=True).status == "OK"
    assert os.path.isfile(ctx.layout.plist)

    other = _write_executable(tmp_path / "other-mihomo")
    ctx.environ[context.BINARY_ENV] = str(other)
    binary = Path(ctx.layout.binary)
    if broken == "missing":
        binary.unlink()
    else:
        binary.chmod(0o644)
    # facts as a fresh CLI/switch/boot process would build them
    ctx.facts = context.register_binary(launchd_facts(), ctx.layout)
    ctx.executor.calls.clear()
    return ctx, other


_BROKEN_STATES = [
    "missing",
    pytest.param(
        "non-executable",
        marks=pytest.mark.skipif(
            os.name == "nt", reason="POSIX execute bits are not emulated on Windows"
        ),
    ),
]


@pytest.mark.parametrize("broken", _BROKEN_STATES)
def test_preflight_fails_closed_when_the_installed_binary_is_unusable(tmp_path, broken):
    ctx, _other = _broken_install(tmp_path, broken)

    report = lifecycle.run_preflight(ctx)

    assert report.status == "FAILED"
    assert report.exit_code == int(ExitCode.UNSUPPORTED)
    assert preflight.MIHOMO_MISSING_REASON in report.warnings
    assert "mihomo" not in ctx.facts.commands
    assert ctx.executor.calls == [], "preflight must not execute anything"


@pytest.mark.parametrize("broken", _BROKEN_STATES)
def test_start_fails_closed_and_never_bootstraps_when_the_binary_is_unusable(tmp_path, broken):
    ctx, other = _broken_install(tmp_path, broken)

    with pytest.raises(Unsupported):
        lifecycle.start(ctx, quiet=True)

    assert ctx.executor.find(str(other), "-t") is None
    assert ctx.executor.find(ctx.layout.binary, "-t") is None
    assert ctx.executor.find("launchctl", "bootstrap") is None
    assert ctx.executor.find("launchctl", "kickstart") is None
    harness = LaunchdHarness.attached(ctx.executor)
    assert harness.loaded is False
    assert harness.running is False


@pytest.mark.parametrize("broken", _BROKEN_STATES)
def test_start_refuses_even_when_stale_facts_still_register_the_override(tmp_path, broken):
    ctx, other = _broken_install(tmp_path, broken)
    # the exact pre-fix bug: facts carry the override as the mihomo capability
    ctx.facts = darwin_facts(commands={**ctx.facts.commands, "mihomo": str(other)})

    with pytest.raises(FailClosed):
        lifecycle.start(ctx, quiet=True)

    assert ctx.executor.find(str(other), "-t") is None
    assert ctx.executor.find("launchctl", "bootstrap") is None
    assert ctx.executor.find("launchctl", "kickstart") is None


@pytest.mark.parametrize("broken", _BROKEN_STATES)
def test_configure_fails_closed_without_validating_the_override(tmp_path, broken):
    ctx, other = _broken_install(tmp_path, broken)
    config_before = Path(ctx.layout.config).read_bytes()

    with pytest.raises(FailClosed):
        lifecycle.configure(ctx, quiet=True)

    assert ctx.executor.find(str(other), "-t") is None
    assert ctx.executor.find(ctx.layout.binary, "-t") is None
    assert Path(ctx.layout.config).read_bytes() == config_before


def test_install_repairs_a_missing_binary_from_the_override(tmp_path):
    ctx, other = _broken_install(tmp_path, "missing")
    other.write_bytes(b"replacement-payload")

    report = lifecycle.install(ctx, quiet=True, skip_start=True)

    assert report.status == "OK", report.messages
    assert Path(ctx.layout.binary).read_bytes() == b"replacement-payload"
    # re-registered as the installed path, verified against it, never the source
    assert ctx.facts.commands["mihomo"] == ctx.layout.binary
    assert ctx.executor.find(ctx.layout.binary, "-t") is not None
    assert ctx.executor.find(str(other), "-t") is None