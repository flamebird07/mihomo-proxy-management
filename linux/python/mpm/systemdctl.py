"""systemctl wrapper - scope-limited to *our* unit (section 7).

Hard rules implemented here:

* only ``systemctl`` subcommands are ever used; no ``pkill``/``killall``/
  ``pgrep -f``/process-name scanning anywhere in the project
* ``stop`` and ``restart`` address the unit name, so only our own unit/cgroup
  is ever signalled
* ``start`` never touches ``enable`` state (that is an explicit flag)
* ``enable_state`` and ``active_state`` are reported separately
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .errors import FailClosed
from .executor import Executor

SYSTEMCTL = "systemctl"
VALID_UNIT = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")

ACTIVE = "active"
INACTIVE = "inactive"
FAILED = "failed"
UNKNOWN = "unknown"


@dataclass
class UnitState:
    unit: str
    active: str
    sub: str
    enabled: str
    main_pid: str
    detail: str = ""

    @property
    def is_active(self) -> bool:
        return self.active == ACTIVE

    @property
    def is_enabled(self) -> bool:
        return self.enabled.startswith("enabled")

    def to_dict(self) -> dict[str, object]:
        return {
            "unit": self.unit,
            "active_state": self.active,
            "sub_state": self.sub,
            "enabled_state": self.enabled,
            "main_pid_present": bool(self.main_pid and self.main_pid != "0"),
        }


def _check_unit(unit: str) -> None:
    if not VALID_UNIT.match(unit):
        raise FailClosed(f"refusing to operate on a non-unit name: {unit!r}")


class Systemd:
    def __init__(
        self,
        executor: Executor,
        *,
        unit: str,
        root: str = "/",
        sleeper=None,
    ) -> None:
        _check_unit(unit)
        self.executor = executor
        self.unit = unit
        self.root = root
        # injectable so tests never sleep; production uses time.sleep
        self.sleeper = sleeper if sleeper is not None else _default_sleep

    # ---- read-only -----------------------------------------------------
    def show(self, *properties: str) -> dict[str, str]:
        args = [SYSTEMCTL, "show", self.unit]
        for prop in properties:
            args += ["-p", prop]
        args += ["--value"]
        result = self.executor.run(args)
        values = result.stdout.strip().splitlines()
        if len(values) != len(properties):
            # systemd prints one line per property; pad with blanks
            values += [""] * (len(properties) - len(values))
        return dict(zip(properties, (value.strip() for value in values)))

    def state(self) -> UnitState:
        props = self.show("ActiveState", "SubState", "UnitFileState", "MainPID")
        return UnitState(
            unit=self.unit,
            active=props.get("ActiveState") or UNKNOWN,
            sub=props.get("SubState") or UNKNOWN,
            enabled=props.get("UnitFileState") or "static",
            main_pid=props.get("MainPID") or "0",
        )

    def is_active(self) -> bool:
        return self.state().is_active

    def unit_installed(self) -> bool:
        result = self.executor.run([SYSTEMCTL, "list-unit-files", self.unit, "--no-legend"])
        return self.unit in (result.stdout or "")

    # ---- state changes -------------------------------------------------
    def daemon_reload(self) -> None:
        self.executor.run_ok([SYSTEMCTL, "daemon-reload"])

    def start(self) -> None:
        # start does not enable; enable is an explicit, separate decision
        self.executor.run_ok([SYSTEMCTL, "start", self.unit])

    def stop(self) -> None:
        # only systemctl stop, targeting our unit - never a process scan
        self.executor.run_ok([SYSTEMCTL, "stop", self.unit])

    def enable(self) -> None:
        self.executor.run_ok([SYSTEMCTL, "enable", self.unit])

    def disable(self) -> None:
        self.executor.run_ok([SYSTEMCTL, "disable", self.unit])

    def disable_now(self) -> None:
        self.executor.run_ok([SYSTEMCTL, "disable", "--now", self.unit])

    def reset_failed(self) -> None:
        self.executor.run_ok([SYSTEMCTL, "reset-failed", self.unit])

    def verify(self) -> tuple[bool, str]:
        """``systemd-analyze verify`` when available; caller reports NOT_RUN."""
        if not self.executor.has("systemd-analyze"):
            return False, "systemd-analyze not available"
        result = self.executor.run(["systemd-analyze", "verify", self.unit_path])
        return result.ok, (result.stdout + result.stderr).strip()

    @property
    def unit_path(self) -> str:
        from .paths import Layout

        return Layout(self.root).unit

    def wait_stopped(self, *, attempts: int = 20) -> bool:
        """Poll until the unit is inactive.  Only ever polls our own unit."""
        for _ in range(attempts):
            if not self.state().is_active:
                return True
            self.sleeper(0.25)
        return not self.state().is_active

    def wait_active(self, *, attempts: int = 40) -> bool:
        for _ in range(attempts):
            if self.state().is_active:
                return True
            self.sleeper(0.25)
        return self.state().is_active


def _default_sleep(seconds: float) -> None:  # pragma: no cover - host path only
    time.sleep(seconds)


__all__ = ["ACTIVE", "FAILED", "INACTIVE", "Systemd", "UnitState", "UNKNOWN"]
