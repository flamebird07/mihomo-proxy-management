"""Command execution seam.

Every external program is invoked through :class:`Executor`.  Tests inject
:class:`FakeExecutor` so that ``systemctl``, ``systemd-analyze`` and
``mihomo -t`` are recorded but never run, which is what keeps the whole test
suite free of real system state changes.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field

from .errors import FailClosed
from .sanitize import HOST


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class Call:
    """A recorded invocation, used by assertions."""

    argv: list[str]
    cwd: str | None
    env: dict[str, str] = field(default_factory=dict)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class Executor:
    """Real subprocess executor."""

    def __init__(self, *, interactive: bool = False) -> None:
        self.interactive = interactive
        self.calls: list[Call] = []

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def has(self, name: str) -> bool:
        return self.which(name) is not None

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
        timeout: float | None = 60.0,
    ) -> Result:
        if not argv:
            raise FailClosed("empty argv")
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=run_env,
                input="" if not self.interactive else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            result = Result(tuple(argv), 127, "", HOST.exception(exc))
            self.calls.append(Call(list(argv), cwd, env or {}, 127, "", HOST.exception(exc)))
            if check:
                raise FailClosed(f"command not found: {argv[0]}") from exc
            return result
        except subprocess.TimeoutExpired as exc:
            result = Result(tuple(argv), 124, "", "timeout")
            self.calls.append(Call(list(argv), cwd, env or {}, 124, "timeout", HOST.exception(exc)))
            if check:
                raise FailClosed(f"command timed out: {shlex.join([argv[0]])}") from exc
            return result

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        self.calls.append(Call(list(argv), cwd, env or {}, proc.returncode, stdout, stderr))
        result = Result(tuple(argv), proc.returncode, stdout, stderr)
        if check and not result.ok:
            raise FailClosed(
                "command failed rc=%d: %s" % (proc.returncode, shlex.join(argv))
            )
        return result

    def run_ok(self, argv: list[str], **kwargs) -> Result:
        return self.run(argv, check=True, **kwargs)

    def reset(self) -> None:
        self.calls = []


class FakeExecutor(Executor):
    """Executor that never spawns processes.

    ``responses`` maps an argv-prefix key (space separated) to either a
    :class:`Result`-like tuple ``(returncode, stdout, stderr)`` or a callable
    taking the argv list.  Unmatched commands return success with empty output
    unless ``default`` is set.
    """

    def __init__(self, *, default: tuple[int, str, str] = (0, "", "")) -> None:
        super().__init__()
        self.responses: dict[str, object] = {}
        self.default = default
        self.interactive = False

    def set(self, prefix: str, value: object) -> None:
        self.responses[prefix] = value

    def which(self, name: str) -> str | None:
        # pretend systemctl/systemd-analyze/curl exist so the code paths run
        return f"/usr/bin/{name}" if name else None

    def has(self, name: str) -> bool:
        return True

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
        timeout: float | None = 60.0,
    ) -> Result:
        if not argv:
            raise FailClosed("empty argv")
        joined = " ".join(argv)
        value: object = self.default
        for prefix, candidate in self.responses.items():
            if joined == prefix or joined.startswith(prefix + " "):
                value = candidate
                break
        if callable(value):
            outcome = value(argv)
        else:
            outcome = value
        if isinstance(outcome, Result):
            code, out, err = outcome.returncode, outcome.stdout, outcome.stderr
        else:
            code, out, err = outcome  # type: ignore[misc]
        self.calls.append(Call(list(argv), cwd, dict(env or {}), code, out, err))
        result = Result(tuple(argv), code, out, err)
        if check and not result.ok:
            raise FailClosed(f"command failed rc={code}: {shlex.join(argv)}")
        return result

    def find(self, *prefix_tokens: str) -> Call | None:
        prefix = " ".join(prefix_tokens)
        for call in self.calls:
            joined = " ".join(call.argv)
            if joined == prefix or joined.startswith(prefix + " "):
                return call
        return None

    def count(self, *prefix_tokens: str) -> int:
        prefix = " ".join(prefix_tokens)
        return sum(
            1
            for call in self.calls
            if " ".join(call.argv) == prefix or " ".join(call.argv).startswith(prefix + " ")
        )


__all__ = ["Executor", "FakeExecutor", "Result", "Call"]
