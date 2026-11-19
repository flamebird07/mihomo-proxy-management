"""Local single-instance lock file (run infrastructure, not node data)."""

from __future__ import annotations

import os
import time
from pathlib import Path


class LockBusy(Exception):
    pass


class FileLock:
    STALE_SECONDS = 24 * 3600

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_stale()
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockBusy("another node_ops instance is running") from None
        os.write(self._fd, f"pid={os.getpid()} time={int(time.time())}\n".encode("ascii"))

    def _remove_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age > self.STALE_SECONDS:
            try:
                self.path.unlink()
            except OSError:
                pass

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
