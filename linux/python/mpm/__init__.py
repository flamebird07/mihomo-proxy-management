"""mihomo-proxy-management Linux system-profile implementation (mpm).

All product behaviour lives in this package.  The shell entry point in
``linux/bin/mihomo-proxy-management`` is a thin wrapper only.

Design contract (phase 3):

* one Python CLI = the only implementation of the lifecycle logic
* every path is derived from a :class:`mpm.paths.Layout` so that tests can run
  against a throwaway root prefix instead of the host
* every external command goes through :class:`mpm.executor.Executor` so tests
  can inject a fake systemctl / mihomo without touching the host
* no secret (subscription URL, controller secret, node endpoint) may ever reach
  an output stream; see :mod:`mpm.sanitize`
"""

__all__ = ["__version__"]
__version__ = "3.0.0"
