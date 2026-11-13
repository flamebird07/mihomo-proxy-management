"""Test bootstrap: make ``mpm_common`` / ``mpm_macos`` importable and register markers.

The tests are host-free by design: they run on Windows, Linux and macOS because
every external command, lock and controller is injected.

Module names must stay unique across the repository: pytest's default
(prepend) import mode keys modules by basename, so ``macos/tests/test_config.py``
and ``linux/tests/test_config.py`` cannot both be imported in one run.  Every
test module here therefore carries the ``test_macos_`` prefix and the shared
fakes live in ``macos_test_support``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "common" / "python", ROOT / "macos" / "python"):
    _entry = str(_path)
    if _entry not in sys.path:
        sys.path.insert(0, _entry)



def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "macos: host-free macOS adapter tests")
    config.addinivalue_line(
        "markers", "darwin_integration: tests that read real Darwin host state (skipped elsewhere)"
    )
