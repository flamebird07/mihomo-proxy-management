"""Linux lifecycle tests are POSIX-only.

On Windows every case fails at the first Linux-host assumption (``os.geteuid``,
systemd unit paths, ``/proc`` ...).  They are skipped as a group there instead
of reporting 100+ spurious failures; the Linux and macOS runs are unaffected.
"""

from __future__ import annotations

import os
import sys

import pytest

LINUX_ONLY_REASON = (
    "Linux lifecycle tests are Linux/POSIX-only; on main they errored at collection"
)

HERE = os.path.dirname(os.path.abspath(__file__))


def _item_directory(item) -> str:
    path = getattr(item, "path", None)
    if path is None:  # pragma: no cover - pytest < 7 exposes only fspath
        path = item.fspath
    return os.path.dirname(os.path.abspath(str(path)))


def pytest_collection_modifyitems(config: pytest.Config, items) -> None:
    """Skip only this directory's tests; other suites are untouched."""
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(reason=LINUX_ONLY_REASON)
    for item in items:
        if _item_directory(item) == HERE:
            item.add_marker(skip)
