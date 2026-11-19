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
from mpm_macos.conflict import ConflictReport
from mpm_macos.lifecycle import TunObservation
from mpm_macos.locking import LOCK_UN
from mpm_macos.paths import Layout

ROOT = Path(__file__).resolve().parents[2]
CONFIG_TEMPLATE = ROOT / "config" / "config.yaml.template"
PLIST_TEMPLATE = ROOT / "macos" / "templates" / "com.mihomo-proxy-management.core.plist.template"
BOOT_PLIST_TEMPLATE = (
    ROOT / "macos" / "templates" / "com.mihomo-proxy-management.boot-switch.plist.template"
)

ALL_COMMANDS = (
    "launchctl",
    "networksetup",
    "mihomo",
    "caffeinate",
    "ifconfig",
    "route",
    "scutil",
    "lsof",
    "osascript",
    "curl",
)


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


#: The PATH a LaunchDaemon runs with: no /usr/local/bin, no Homebrew, and
#: certainly not the installed mihomo under libexec.
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def launchd_facts(**overrides) -> PlatformFacts:
    """Darwin facts as a LaunchDaemon sees them: mihomo is *not* on the PATH.

    The runtime capability must come from the shared context builder
    registering the installed absolute path, never from a PATH lookup.
    """
    commands = {
        name: f"/usr/bin/{name}"
        for name in (
            "launchctl",
            "networksetup",
            "caffeinate",
            "ifconfig",
            "route",
            "scutil",
            "lsof",
            "osascript",
        )
    }
    values = {"commands": commands}
    values.update(overrides)
    return darwin_facts(**values)


class FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances time."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeController:
    """Minimal stand-in for :class:`ControllerClient`."""

    def __init__(
        self,
        *,
        version: str = "1.19.0",
        node_count: int = 3,
        alive: int = 3,
        reachable: bool = True,
        group_alive: dict | None = None,
    ) -> None:
        self._version = version
        self._node_count = node_count
        self._alive = alive
        self._reachable = reachable
        self.group_alive = (
            dict(group_alive)
            if group_alive is not None
            else {"AUTO-FOREIGN": True, "GPT": True}
        )
        self.healthchecks: list[str] = []
        self.healthcheck_timeouts: list[float | None] = []

    def version(self, *, timeout: float | None = None) -> dict:
        if not self._reachable:
            raise ControllerError("controller unreachable")
        return {"version": self._version}

    def healthcheck(self, name: str, *, timeout: float | None = None) -> None:
        self.healthchecks.append(name)
        self.healthcheck_timeouts.append(timeout)
        if not self._reachable:
            raise ControllerError("healthcheck failed")

    def providers(self, *, timeout: float | None = None) -> dict:
        if not self._reachable:
            raise ControllerError("controller unreachable")
        nodes = [
            {"name": f"node-{index}", "alive": index < self._alive}
            for index in range(self._node_count)
        ]
        return {"providers": {"subscription": {"all": nodes}}}

    def proxies(self, *, timeout: float | None = None) -> dict:
        proxies = {
            f"node-{index}": {"alive": index < self._alive}
            for index in range(self._node_count)
        }
        for name, alive in self.group_alive.items():
            proxies[name] = {"alive": alive, "type": "Fallback"}
        return {"proxies": proxies}


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


CORE_LABEL = "com.mihomo-proxy-management.core"
BOOT_LABEL = "com.mihomo-proxy-management.boot-switch"
FEISHU_LABEL = "com.mihomo-proxy-management.feishu-sync"
NODE_LABEL = "com.mihomo-proxy-management.node-pipeline"


def _target_label(argv) -> str:
    """Label addressed by ``system/<label>`` (``bootstrap`` passes a plist path)."""
    target = str(argv[-1]) if argv else ""
    if target.startswith("system/"):
        return target.split("/", 1)[1]
    if BOOT_LABEL in target:
        return BOOT_LABEL
    if FEISHU_LABEL in target:
        return FEISHU_LABEL
    if NODE_LABEL in target:
        return NODE_LABEL
    return CORE_LABEL


class LaunchdHarness:
    """Stateful ``launchctl`` responses wired into a FakeExecutor.

    Three labels exist, so the persistent-disable state is kept per label:
    ``disabled`` is the core job (what the safety rules are about) and
    ``boot_disabled`` the boot entry.  Load/run state is tracked separately for
    each one as well - the boot entry runs once and is normally not loaded,
    and the Feishu sync job is loaded by install but never running (no
    ``RunAtLoad``; its first tick fires one ``StartInterval`` later).
    """

    def __init__(self, executor: FakeExecutor) -> None:
        self.executor = executor
        executor.launchd_harness = self
        self.loaded = False
        self.running = False
        self.pid: int | None = None
        self.boot_loaded = False
        self.boot_running = False
        self.feishu_loaded = False
        self.feishu_running = False
        self.node_loaded = False
        self.node_running = False
        #: per-label ``launchctl disable`` override; the core starts disabled
        #: (install disables it) and the boot entry starts enabled.
        self.disabled_labels: dict[str, bool] = {
            CORE_LABEL: True,
            BOOT_LABEL: False,
            FEISHU_LABEL: False,
            NODE_LABEL: False,
        }
        #: bootstrap/kickstart calls that load the job but leave it stopped,
        #: so ``wait_running`` times out (a start failure without a crash).
        self.start_failures = 0
        #: The context's real ``Layout``, needed by the simulated RunAtLoad
        #: boot-entry process to check the hold stamp and write the ack file.
        #: Tests that build a harness by hand and then run ``install`` must
        #: set it (``harness.layout = ctx.layout``).
        self.layout: Layout | None = None
        #: How many more ``launchctl print`` observations the simulated boot
        #: entry stays "up but not yet at the stamp check" - the async race
        #: Codex review boot3 flagged (loaded, process not done yet).
        self.boot_entry_delay = 0
        #: Whether the simulated entry writes the ack when it reads the stamp.
        self.boot_entry_acks = True
        #: How many times the simulated entry completed a run.
        self.boot_entry_runs = 0
        executor.set("launchctl print", self._print)
        executor.set("launchctl print-disabled", self._print_disabled)
        executor.set("launchctl bootstrap", self._bootstrap)
        executor.set("launchctl bootout", self._bootout)
        executor.set("launchctl kickstart", self._kickstart)
        executor.set("launchctl enable", self._enable)
        executor.set("launchctl disable", self._disable)

    @classmethod
    def attached(cls, executor: FakeExecutor) -> "LaunchdHarness":
        """Return the harness already wired to ``executor``, attaching one if absent.

        ``make_context`` attaches a harness automatically; ``attached`` lets a
        helper or test reuse that instance instead of resetting its state.
        """
        existing = getattr(executor, "launchd_harness", None)
        if existing is not None:
            return existing
        return cls(executor)

    @property
    def disabled(self) -> bool:
        """The core job's persistent disable override."""
        return self.disabled_labels[CORE_LABEL]

    @disabled.setter
    def disabled(self, value: bool) -> None:
        self.disabled_labels[CORE_LABEL] = value

    @property
    def boot_disabled(self) -> bool:
        return self.disabled_labels[BOOT_LABEL]

    @boot_disabled.setter
    def boot_disabled(self, value: bool) -> None:
        self.disabled_labels[BOOT_LABEL] = value

    @property
    def feishu_disabled(self) -> bool:
        return self.disabled_labels[FEISHU_LABEL]

    @feishu_disabled.setter
    def feishu_disabled(self, value: bool) -> None:
        self.disabled_labels[FEISHU_LABEL] = value

    @property
    def node_disabled(self) -> bool:
        return self.disabled_labels[NODE_LABEL]

    @node_disabled.setter
    def node_disabled(self, value: bool) -> None:
        self.disabled_labels[NODE_LABEL] = value

    def _print(self, argv):
        target = _target_label(argv)
        if target == BOOT_LABEL:
            if not self.boot_loaded:
                return (113, "", f'Could not find service "{BOOT_LABEL}"')
            if self.boot_running:
                # time passes between two launchctl observations: advance the
                # simulated RunAtLoad process before reporting its state
                self._run_boot_entry()
            state = "running" if self.boot_running else "not running"
            return (0, f"state = {state}\n\tlast exit code = 0\n", "")
        if target == FEISHU_LABEL:
            if not self.feishu_loaded:
                return (113, "", f'Could not find service "{FEISHU_LABEL}"')
            state = "running" if self.feishu_running else "not running"
            return (0, f"state = {state}\n\tlast exit code = 0\n", "")
        if target == NODE_LABEL:
            if not self.node_loaded:
                return (113, "", f'Could not find service "{NODE_LABEL}"')
            state = "running" if self.node_running else "not running"
            return (0, f"state = {state}\n\tlast exit code = 0\n", "")
        if not self.loaded:
            return (113, "", 'Could not find service "com.mihomo-proxy-management.core"')
        if self.running:
            return (0, f"state = running\n\tpid = {self.pid or 4242}\n", "")
        return (0, "state = not running\n\tlast exit code = 0\n", "")

    def _run_boot_entry(self) -> None:
        """Advance the simulated RunAtLoad boot-entry process one observation.

        While ``boot_entry_delay`` is positive the process is up but has not
        reached the hold-stamp check yet (nothing is acked).  Once it gets
        there, it reads the *real* stamp file, writes the *real* 0600 ack
        file (if ``boot_entry_acks``) and exits - exactly what
        ``mpm_macos.boot`` does on the hold exit.
        """
        if self.boot_entry_delay > 0:
            self.boot_entry_delay -= 1
            return
        self.boot_entry_runs += 1
        if (
            self.boot_entry_acks
            and self.layout is not None
            and os.path.isfile(self.layout.boot_hold)
        ):
            try:
                descriptor = os.open(
                    self.layout.boot_hold_ack,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                os.close(descriptor)
            except FileExistsError:
                pass
        # the entry has no KeepAlive: it exits after this single run
        self.boot_running = False

    def _print_disabled(self, argv):
        entries = "".join(
            f'\t"{label}" => disabled\n'
            for label, value in self.disabled_labels.items()
            if value
        )
        return (0, "disabled services = {\n" + entries + "}\n", "")

    def _enable(self, argv):
        self.disabled_labels[_target_label(argv)] = False
        return (0, "", "")

    def _disable(self, argv):
        self.disabled_labels[_target_label(argv)] = True
        return (0, "", "")

    def _start(self) -> None:
        self.loaded = True
        if self.start_failures > 0:
            self.start_failures -= 1
            self.running = False
            self.pid = None
            return
        self.running = True
        self.pid = 4242

    def _start_boot(self) -> None:
        self.boot_loaded = True
        self.boot_running = True

    def _start_feishu(self) -> None:
        # no RunAtLoad: bootstrap loads the job, the first tick fires later
        self.feishu_loaded = True
        self.feishu_running = False

    def _bootstrap(self, argv):
        target = _target_label(argv)
        if target == BOOT_LABEL:
            self._start_boot()
            return (0, "", "")
        if target == FEISHU_LABEL:
            self._start_feishu()
            return (0, "", "")
        if target == NODE_LABEL:
            self.node_loaded = True
            self.node_running = False
            return (0, "", "")
        self._start()
        return (0, "", "")

    def _bootout(self, argv):
        target = _target_label(argv)
        if target == BOOT_LABEL:
            self.boot_loaded = False
            self.boot_running = False
            return (0, "", "")
        if target == FEISHU_LABEL:
            self.feishu_loaded = False
            self.feishu_running = False
            return (0, "", "")
        if target == NODE_LABEL:
            self.node_loaded = False
            self.node_running = False
            return (0, "", "")
        self.loaded = False
        self.running = False
        self.pid = None
        return (0, "", "")

    def _kickstart(self, argv):
        target = _target_label(argv)
        if target == BOOT_LABEL:
            self._start_boot()
            return (0, "", "")
        if target == FEISHU_LABEL:
            self._start_feishu()
            return (0, "", "")
        self._start()
        self.pid = 4243 if self.running else None
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
    executor = overrides.pop("executor", None) or FakeExecutor()
    # A fake clock paired with the sleeper: deadline loops (stable disconnect
    # window, health gate, probe) advance instantly and never really sleep.
    clock = overrides.pop("clock", None) or FakeClock()
    # install talks to launchd (it disables the core, then bootstraps the
    # boot-switch entry and waits for it to acknowledge the hold stamp); a
    # bare FakeExecutor would answer every print with "not found" and every
    # install test would fail closed.  The harness is stateful per executor
    # and ``attached`` reuses an existing one, so helpers that wire their own
    # harness still read the state install actually produced.  The layout
    # lets the simulated boot entry read the real stamp and write the real
    # ack file.
    layout = Layout(str(root))
    LaunchdHarness.attached(executor).layout = layout
    values = {
        "layout": layout,
        "facts": darwin_facts(),
        "executor": executor,
        "out": io.StringIO(),
        "err": io.StringIO(),
        "environ": env,
        "system_profile": False,
        "python": "/usr/bin/python3",
        "sleeper": clock.sleep,
        "clock": clock,
        "validator": lambda _text: None,
        "tun_observer": lambda: lifecycle.TunObservation(("en0",), "en0"),
        "conflict_detector": lambda: ConflictReport(ok=True),
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
