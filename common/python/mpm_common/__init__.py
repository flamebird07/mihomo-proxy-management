"""Platform-neutral building blocks shared by the Linux and macOS adapters.

Nothing in this package may import a platform-specific module at import time
(``fcntl``, ``winreg``, ``systemd``...); those are imported lazily inside the
function that needs them, so the package can be imported and unit-tested on
Windows, Linux and macOS alike.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
