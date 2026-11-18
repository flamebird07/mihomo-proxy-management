"""Minimal local logger for run infrastructure.

The log file is run infrastructure (allowed locally).  Node data, history and
caches must NOT be written locally by this package.

Security contract: callers never pass subscription URLs, credentials, tokens or
node passwords to this logger.  As defence in depth, registered secret values
are scrubbed from every line before it is written.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Iterable


class Logger:
    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, path: str | None = None, verbose: bool = False) -> None:
        self.path = Path(path) if path else None
        self.verbose = verbose
        self._secrets: list[str] = []
        self._fallback = False

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= 3:
            self._secrets.append(value)

    def _scrub(self, message: str) -> str:
        text = str(message)
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def _write(self, level: str, message: str) -> None:
        line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {level} {self._scrub(message)}"
        if self.verbose or level in ("WARN", "ERROR"):
            print(line, file=sys.stderr, flush=True)
        if self.path is None or self._fallback:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                backup = self.path.with_name(self.path.name + ".1")
                if backup.exists():
                    backup.unlink()
                self.path.rename(backup)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            self._fallback = True
            print(f"{line} (log write failed: {type(exc).__name__})", file=sys.stderr, flush=True)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def add_secrets(self, values: Iterable[str | None]) -> None:
        for value in values:
            self.add_secret(value)
