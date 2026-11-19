"""Minimal logger with defence-in-depth secret scrubbing.

No file is written: the provider's only local artifact is the output YAML.
Callers must never pass node configs or credentials to the logger; registered
secret values are additionally scrubbed from every emitted line.
"""

from __future__ import annotations

import sys
from typing import Callable, Iterable, List, Optional


class Logger:
    def __init__(self, sink: Optional[Callable[[str], None]] = None,
                 verbose: bool = False) -> None:
        self._sink = sink
        self.verbose = verbose
        self._secrets: List[str] = []
        self.lines: List[str] = []

    def add_secret(self, value: Optional[str]) -> None:
        if value and len(value) >= 3 and value not in self._secrets:
            self._secrets.append(value)

    def add_secrets(self, values: Iterable[Optional[str]]) -> None:
        for value in values:
            self.add_secret(value)

    def _scrub(self, message: str) -> str:
        text = str(message)
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def _emit(self, level: str, message: str) -> None:
        line = f"{level} {self._scrub(message)}"
        self.lines.append(line)
        if self._sink is not None:
            self._sink(line)

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)


def stderr_logger(verbose: bool = False) -> Logger:
    return Logger(sink=lambda line: print(line, file=sys.stderr, flush=True),
                  verbose=verbose)
