"""POSIX ``flock`` locks: one lifecycle lock, one core lock.

Two independent locks, acquired in a fixed order and never mixed:

* **lifecycle lock** - serialises install/configure/start/stop/restart;
* **core lock** - held by the runner for the whole mihomo lifetime, so a second
  core can never start.

The lock is owned by the open file descriptor; the kernel releases it when the
process dies.  There is deliberately no PID-file deletion or stale-file
takeover logic (that is the race this design avoids).  The lock file only
records a PID, a timestamp and the lock's mode - never a secret.

``fcntl`` is imported lazily inside :func:`_default_flock`, so this module can
be imported (and its logic tested with an injected fake) on Windows.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from mpm_common.errors import FailClosed

# Values are identical on Linux and macOS (fcntl.LOCK_EX / LOCK_NB / LOCK_UN).
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

Flock = Callable[[int, int], None]


class LockBusy(FailClosed):
    """Another process holds the lock; nothing was changed."""


def _default_flock(fd: int, flags: int) -> None:
    import fcntl  # local import: keeps the module importable on Windows

    fcntl.flock(fd, flags)


@dataclass
class FileLock:
    """Non-blocking exclusive lock held by an open file descriptor."""

    path: str
    label: str = "lock"
    flock: Flock | None = None
    clock: Callable[[], float] = time.time
    _fd: int | None = field(default=None, init=False, repr=False)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "FileLock":
        if self._fd is not None:
            return self
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            (self.flock or _default_flock)(fd, LOCK_EX | LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LockBusy(
                f"{self.label} lock is held by another process: {self.path}"
            ) from exc
        self._fd = fd
        self._write_metadata()
        return self

    def _write_metadata(self) -> None:
        """PID/time/mode only - the lock file must never carry a secret."""
        if self._fd is None:  # pragma: no cover - guarded by acquire
            return
        payload = json.dumps(
            {"pid": os.getpid(), "time": round(self.clock(), 3), "mode": self.label},
            sort_keys=True,
        )
        data = (payload + "\n").encode("utf-8")
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data)
        os.fsync(self._fd)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        # closing the descriptor releases the flock; the file is left in place
        # on purpose - deleting it would reintroduce the stale-file race.
        try:
            (self.flock or _default_flock)(fd, LOCK_UN)
        except OSError:  # pragma: no cover - descriptor already invalid
            pass
        os.close(fd)

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def is_held(path: str, *, flock: Flock | None = None) -> bool:
    """True when some process currently holds the lock at ``path``."""
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        (flock or _default_flock)(fd, LOCK_EX | LOCK_NB)
    except ImportError:  # pragma: no cover - Windows has no fcntl
        return False
    except OSError:
        return True
    else:
        try:
            (flock or _default_flock)(fd, LOCK_UN)
        except OSError:  # pragma: no cover
            pass
        return False
    finally:
        os.close(fd)


def read_metadata(path: str) -> dict[str, object]:
    """Best-effort read of the non-secret lock metadata."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "FileLock",
    "Flock",
    "LOCK_EX",
    "LOCK_NB",
    "LOCK_UN",
    "LockBusy",
    "is_held",
    "read_metadata",
]
