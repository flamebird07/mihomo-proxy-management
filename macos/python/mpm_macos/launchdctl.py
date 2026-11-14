"""Thin, strictly scoped ``launchctl`` adapter.

Only the fixed label is ever addressed; every command is built as an argv list
(never a shell string).  Parsing is pure so it can be tested with captured
``launchctl print`` output on any OS.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from mpm_common.errors import FailClosed
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST

from .paths import LABEL

DOMAIN = "system"

_STATE = re.compile(r"^\s*state\s*=\s*(?P<value>\S+)", re.MULTILINE)
_PID = re.compile(r"^\s*pid\s*=\s*(?P<value>\d+)", re.MULTILINE)
_LAST_EXIT = re.compile(r"^\s*last exit code\s*=\s*(?P<value>-?\d+)", re.MULTILINE)
_PROGRAM = re.compile(r"^\s*program\s*=\s*(?P<value>.+)$", re.MULTILINE)
_NOT_FOUND = re.compile(r"could not find service|no such process", re.IGNORECASE)
_DISABLED_ENTRY = re.compile(
    r'"(?P<label>[^"]+)"\s*=>\s*(?P<value>true|false|disabled|enabled)', re.IGNORECASE
)


def validate_label(label: str) -> str:
    """Only our own LaunchDaemon may be operated on."""
    if label != LABEL:
        raise FailClosed(f"refusing to operate on a foreign launchd label: {label}")
    return label


@dataclass
class LaunchdState:
    label: str
    loaded: bool = False
    running: bool = False
    pid: int | None = None
    last_exit: int | None = None
    state_text: str = ""
    program: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "loaded": self.loaded,
            "running": self.running,
            "pid": self.pid,
            "last_exit": self.last_exit,
            "state": self.state_text,
        }


def parse_print(text: str, *, label: str = LABEL, returncode: int = 0) -> LaunchdState:
    """Parse ``launchctl print system/<label>`` output.

    A non-zero return code or the "Could not find service" message means the
    job is not loaded; it is never confused with "loaded but stopped".
    """
    payload = text or ""
    if returncode != 0 or _NOT_FOUND.search(payload):
        return LaunchdState(label=label, loaded=False, running=False)
    state_match = _STATE.search(payload)
    pid_match = _PID.search(payload)
    exit_match = _LAST_EXIT.search(payload)
    program_match = _PROGRAM.search(payload)
    state_text = state_match.group("value") if state_match else ""
    loaded = bool(state_match or "path = " in payload)
    return LaunchdState(
        label=label,
        loaded=loaded,
        running=state_text == "running",
        pid=int(pid_match.group("value")) if pid_match else None,
        last_exit=int(exit_match.group("value")) if exit_match else None,
        state_text=state_text,
        program=(program_match.group("value").strip() if program_match else ""),
    )


def parse_disabled(text: str, *, label: str = LABEL) -> bool:
    """Parse ``launchctl print-disabled system`` for one label.

    ``launchctl print-disabled system`` prints entries such as::

        disabled services = {
        \t"com.example.job" => true
        \t"com.example.other" => disabled
        }

    A label that is absent is enabled (the launchd default).
    """
    for match in _DISABLED_ENTRY.finditer(text or ""):
        if match.group("label") == label:
            return match.group("value").lower() in ("true", "disabled")
    return False


class Launchd:
    """``launchctl`` operations for the fixed label only."""

    def __init__(
        self,
        executor: Executor,
        *,
        plist: str,
        label: str = LABEL,
        domain: str = DOMAIN,
        sleeper: Callable[[float], None] = time.sleep,
        attempts: int = 40,
        interval: float = 0.5,
    ) -> None:
        self.executor = executor
        self.plist = plist
        self.label = validate_label(label)
        self.domain = domain
        self.sleeper = sleeper
        self.attempts = attempts
        self.interval = interval

    # ---- command construction -----------------------------------------
    @property
    def service_target(self) -> str:
        return f"{self.domain}/{self.label}"

    def bootstrap_argv(self) -> list[str]:
        return ["launchctl", "bootstrap", self.domain, self.plist]

    def bootout_argv(self) -> list[str]:
        return ["launchctl", "bootout", self.service_target]

    def kickstart_argv(self) -> list[str]:
        return ["launchctl", "kickstart", "-k", self.service_target]

    def print_argv(self) -> list[str]:
        return ["launchctl", "print", self.service_target]

    def enable_argv(self) -> list[str]:
        return ["launchctl", "enable", self.service_target]

    def disable_argv(self) -> list[str]:
        return ["launchctl", "disable", self.service_target]

    def print_disabled_argv(self) -> list[str]:
        return ["launchctl", "print-disabled", self.domain]

    # ---- state ---------------------------------------------------------
    def state(self) -> LaunchdState:
        result = self.executor.run(self.print_argv())
        return parse_print(
            (result.stdout or "") + (result.stderr or ""),
            label=self.label,
            returncode=result.returncode,
        )

    def loaded(self) -> bool:
        return self.state().loaded

    def running(self) -> bool:
        return self.state().running

    def is_disabled(self) -> bool:
        """Whether the persistent launchd override disables this label."""
        result = self.executor.run(self.print_disabled_argv())
        if not result.ok:
            raise FailClosed(
                "launchctl print-disabled failed: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:200]
            )
        return parse_disabled((result.stdout or "") + (result.stderr or ""), label=self.label)

    # ---- operations ----------------------------------------------------
    def bootstrap(self) -> None:
        if self.state().loaded:
            return
        result = self.executor.run(self.bootstrap_argv())
        if not result.ok and not self.state().loaded:
            raise FailClosed(
                "launchctl bootstrap failed: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:200]
            )

    def bootout(self) -> None:
        if not self.state().loaded:
            return
        result = self.executor.run(self.bootout_argv())
        if not result.ok:
            # a job that disappeared between print and bootout is success
            if _NOT_FOUND.search((result.stderr or "") + (result.stdout or "")):
                return
            raise FailClosed(
                "launchctl bootout failed: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:200]
            )

    def kickstart(self) -> None:
        result = self.executor.run(self.kickstart_argv())
        if not result.ok:
            raise FailClosed(
                "launchctl kickstart failed: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:200]
            )

    def enable(self) -> None:
        """Remove the persistent disable override before an explicit start."""
        result = self.executor.run(self.enable_argv())
        if not result.ok:
            raise FailClosed(
                "launchctl enable failed: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:200]
            )

    def disable(self) -> None:
        """Persistently disable the label: it will not load at the next boot.

        An already-loaded job keeps running (``disable`` only affects future
        loads), so a successful start can disable immediately and still be
        restarted by ``KeepAlive`` if it crashes in this boot session.
        """
        result = self.executor.run(self.disable_argv())
        if not result.ok:
            raise FailClosed(
                "launchctl disable failed: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:200]
            )

    def wait_running(self, *, attempts: int | None = None) -> bool:
        for _ in range(attempts or self.attempts):
            if self.state().running:
                return True
            self.sleeper(self.interval)
        return False

    def wait_stopped(self, *, attempts: int | None = None) -> bool:
        for _ in range(attempts or self.attempts):
            if not self.state().running:
                return True
            self.sleeper(self.interval)
        return False


__all__ = [
    "DOMAIN",
    "Launchd",
    "LaunchdState",
    "parse_disabled",
    "parse_print",
    "validate_label",
]
