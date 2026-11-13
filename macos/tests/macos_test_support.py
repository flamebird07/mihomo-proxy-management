"""Shared fakes for the host-free macOS tests.

Nothing here touches the host: commands go through ``FakeExecutor``, the
controller is a plain object, and the lock is a one-slot fake.

The module name is prefixed so it cannot collide with ``linux/tests/support.py``
when the whole repository is collected by one ``pytest`` run (prepend import
mode keys modules by basename).
"""

from __future__ import annotations

import errno
import io
import os
import secrets
import stat
from pathlib import Path

import pytest
import yaml

from mpm_common.controller_health import ControllerError
from mpm_common.executor import FakeExecutor
from mpm_common.platforms import PlatformFacts
from mpm_macos import lifecycle
from mpm_macos.lifecycle import TunObservation
from mpm_macos.locking import LOCK_UN
from mpm_macos.paths import Layout

ROOT = Path(__file__).resolve().parents[2]
CONFIG_TEMPLATE = ROOT / "config" / "config.yaml.template"
PLIST_TEMPLATE = ROOT / "macos" / "templates" / "com.mihomo-proxy-management.core.plist.template"

ALL_COMMANDS = ("launchctl", "networksetup", "mihomo", "caffeinate", "ifconfig", "route")


def assert_posix_mode(path, expected: int) -> None:
    """Check an exact file mode; Windows does not emulate POSIX mode bits."""
    if os.name == "nt":
        pytest.skip("POSIX file modes are not emulated on Windows")
    assert stat.S_IMODE(os.stat(path).st_mode) == expected


def darwin_facts(**overrides) -> PlatformFacts:
    values = {
        "system": "darwin",
        "machine": "arm64",
        "raw_machine": "arm64",
        "is_root": True,
        "commands": {name: f"/usr/bin/{name}" for name in ALL_COMMANDS},
        "release": "14.5",
    }
    values.update(overrides)
    return PlatformFacts(**values)


class FakeController:
    """Minimal stand-in for :class:`ControllerClient`."""

    def __init__(
        self,
        *,
        version: str = "1.19.0",
        node_count: int = 3,
        alive: int = 3,
        reachable: bool = True,
    ) -> None:
        self._version = version
        self._node_count = node_count
        self._alive = alive
        self._reachable = reachable
        self.healthchecks: list[str] = []

    def version(self) -> dict:
        if not self._reachable:
            raise ControllerError("controller unreachable")
        return {"version": self._version}

    def healthcheck(self, name: str) -> None:
        self.healthchecks.append(name)
        if not self._reachable:
            raise ControllerError("healthcheck failed")

    def providers(self) -> dict:
        if not self._reachable:
            raise ControllerError("controller unreachable")
        nodes = [
            {"name": f"node-{index}", "alive": index < self._alive}
            for index in range(self._node_count)
        ]
        return {"providers": {"subscription": {"all": nodes}}}

    def proxies(self) -> dict:
        return {
            "proxies": {
                f"node-{index}": {"alive": index < self._alive}
                for index in range(self._node_count)
            }
        }


class FakePopen:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


class FakeFlock:
    """One-slot lock: the second acquire fails until ``LOCK_UN`` is issued."""

    def __init__(self) -> None:
        self.held = False
        self.calls: list[int] = []

    def __call__(self, fd: int, flags: int) -> None:
        self.calls.append(flags)
        if flags & LOCK_UN:
            self.held = False
            return
        if self.held:
            raise OSError(errno.EWOULDBLOCK, "lock is held")
        self.held = True


class LaunchdHarness:
    """Stateful ``launchctl`` responses wired into a FakeExecutor."""

    def __init__(self, executor: FakeExecutor) -> None:
        self.executor = executor
        self.loaded = False
        self.running = False
        self.pid: int | None = None
        executor.set("launchctl print", self._print)
        executor.set("launchctl bootstrap", self._bootstrap)
        executor.set("launchctl bootout", self._bootout)
        executor.set("launchctl kickstart", self._kickstart)

    def _print(self, argv):
        if not self.loaded:
            return (113, "", 'Could not find service "com.mihomo-proxy-management.core"')
        if self.running:
            return (0, f"state = running\n\tpid = {self.pid or 4242}\n", "")
        return (0, "state = not running\n\tlast exit code = 0\n", "")

    def _bootstrap(self, argv):
        self.loaded = True
        self.running = True
        self.pid = 4242
        return (0, "", "")

    def _bootout(self, argv):
        self.loaded = False
        self.running = False
        self.pid = None
        return (0, "", "")

    def _kickstart(self, argv):
        self.running = True
        self.pid = 4243
        return (0, "", "")


class FakeNetworkSetup:
    """Stateful ``networksetup`` implementation (no shell, argv only)."""

    def __init__(self, services=None, *, disabled=(), authenticated=()) -> None:
        self.services = list(services or ["Wi-Fi", "USB 10/100/1000 LAN"])
        self.disabled = set(disabled)
        self.authenticated = set(authenticated)
        self.state = {
            name: {
                "web": {"enabled": False, "server": "", "port": 0},
                "secure": {"enabled": False, "server": "", "port": 0},
                "socks": {"enabled": False, "server": "", "port": 0},
                "bypass": [],
            }
            for name in self.services
        }
        self.fail_set_for: set[str] = set()

    # ---- helpers -------------------------------------------------------
    def seed(self, service: str, kind: str, *, enabled: bool, server: str, port: int) -> None:
        self.state[service][kind] = {"enabled": enabled, "server": server, "port": port}

    def seed_bypass(self, service: str, domains) -> None:
        self.state[service]["bypass"] = list(domains)

    def _text(self, service: str, kind: str) -> str:
        setting = self.state[service][kind]
        auth = 1 if (service, kind) in self.authenticated else 0
        return (
            f"Enabled: {'Yes' if setting['enabled'] else 'No'}\n"
            f"Server: {setting['server']}\n"
            f"Port: {setting['port']}\n"
            f"Authenticated Proxy Enabled: {auth}\n"
        )

    def _bypass_text(self, service: str) -> str:
        domains = self.state[service]["bypass"]
        if not domains:
            return f"There aren't any bypass domains set on {service}.\n"
        return "\n".join(domains) + "\n"

    # ---- handler -------------------------------------------------------
    def __call__(self, argv):
        command = argv[1]
        if command == "-listallnetworkservices":
            lines = ["An asterisk (*) denotes that a network service is disabled."]
            for name in self.services:
                lines.append(("*" if name in self.disabled else "") + name)
            return (0, "\n".join(lines) + "\n", "")

        service = argv[2]
        if service not in self.state:
            return (1, "", "unknown service")

        if command == "-getwebproxy":
            return (0, self._text(service, "web"), "")
        if command == "-getsecurewebproxy":
            return (0, self._text(service, "secure"), "")
        if command == "-getsocksfirewallproxy":
            return (0, self._text(service, "socks"), "")
        if command == "-getproxybypassdomains":
            return (0, self._bypass_text(service), "")

        kind_for_set = {
            "-setwebproxy": "web",
            "-setsecurewebproxy": "secure",
            "-setsocksfirewallproxy": "socks",
        }
        if command in kind_for_set:
            kind = kind_for_set[command]
            if (service, kind) in self.fail_set_for or service in self.fail_set_for:
                return (1, "", "refused")
            self.state[service][kind] = {
                "enabled": True,
                "server": argv[3],
                "port": int(argv[4]),
            }
            return (0, "", "")

        state_for = {
            "-setwebproxystate": "web",
            "-setsecurewebproxystate": "secure",
            "-setsocksfirewallproxystate": "socks",
        }
        if command in state_for:
            self.state[service][state_for[command]]["enabled"] = argv[3] == "on"
            return (0, "", "")

        if command == "-setproxybypassdomains":
            domains = list(argv[3:])
            if domains == ["Empty"]:
                domains = []
            self.state[service]["bypass"] = domains
            return (0, "", "")

        return (1, "", f"unsupported command {command}")


def write_provider(url: str, destination: str) -> list[str]:
    """Default fake converter: writes a provider and returns its node names."""
    names = ["美国-01", "日本-01", "香港-01"]
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(
        yaml.safe_dump(
            {"proxies": [{"name": name, "type": "ss"} for name in names]},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return names


def make_context(root, *, environ=None, **overrides) -> lifecycle.Context:
    env = {
        "MPM_SUBSCRIPTION_URL": "https://sub.example.invalid/subscription",
        # runtime canary: never a credential-shaped literal in the repository
        "MPM_SECRET": "canary-" + secrets.token_hex(8),
        "MPM_SUBSCRIPTION_INTERVAL": "60",
    }
    if environ:
        env.update(environ)
    values = {
        "layout": Layout(str(root)),
        "facts": darwin_facts(),
        "executor": FakeExecutor(),
        "out": io.StringIO(),
        "err": io.StringIO(),
        "environ": env,
        "system_profile": False,
        "python": "/usr/bin/python3",
        "sleeper": lambda _seconds: None,
        "validator": lambda _text: None,
        "tun_observer": lambda: lifecycle.TunObservation(("en0",), "en0"),
    }
    values.update(overrides)
    return lifecycle.Context(**values)


def prepare_install(ctx: lifecycle.Context, *, controller=None, converter=None) -> None:
    """Add the inputs ``install`` needs without touching the host."""
    source = Path(ctx.layout.root) / "source-mihomo"
    source.write_bytes(b"#!/bin/sh\nexit 0\n")
    source.chmod(0o755)
    ctx.environ["MPM_MIHOMO_BINARY"] = str(source)
    ctx.converter = converter or write_provider
    ctx.controller_factory = lambda secret: controller or FakeController()
