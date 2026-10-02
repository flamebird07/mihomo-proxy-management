"""Typed failures with stable exit codes and pre-sanitised messages.

A ``FailClosed`` error always means "refuse to change anything".  The message
attached to it must already be safe to print; ``cli`` re-sanitises it as a
second line of defence.
"""

from __future__ import annotations

EXIT_FAIL_CLOSED = 2
EXIT_NOT_READY = 3
EXIT_UNSUPPORTED = 4


class ExitCode:
    """Stable exit codes; ``main`` maps failures onto these."""

    OK = 0
    FAILURE = 1
    FAIL_CLOSED = EXIT_FAIL_CLOSED
    NOT_READY = EXIT_NOT_READY
    UNSUPPORTED = EXIT_UNSUPPORTED


class MpmError(Exception):
    """Base error; carries an exit code."""

    exit_code = 1

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if exit_code is not None:
            self.exit_code = exit_code


class FailClosed(MpmError):
    """A security or validation boundary was hit.  Nothing was modified."""

    exit_code = EXIT_FAIL_CLOSED


class Unsupported(MpmError):
    """The host is outside the documented support matrix."""

    exit_code = EXIT_UNSUPPORTED


class NotReady(MpmError):
    """The service could not be reached after being asked to start."""

    exit_code = EXIT_NOT_READY
