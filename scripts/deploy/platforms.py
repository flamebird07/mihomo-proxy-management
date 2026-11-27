"""The two concrete platforms the bootstrap flow can target.

A platform contributes three paths and one call: where the Feishu pool
provider lives, where the machine's secret file is, where verified binaries
are staged, and how the platform's own installer is invoked.  Everything
after that call - config rendering, services, scheduled tasks - belongs to
the installers (``mpm_macos.cli install`` on macOS, ``scripts/deploy.ps1``
on Windows).
"""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys

from . import bootstrap

MACOS = "macos"
WINDOWS = "windows"
MACOS_DEFAULT_PYTHON = "/usr/local/libexec/mpm-python-venv/bin/python3"
MACOS_STAGING = "/usr/local/libexec/mihomo-proxy-management/supply"
#: The Feishu pool provider, relative to mihomo's home / the install dir.
POOL_RELATIVE = ("providers", "feishu-pool.yaml")
WINDOWS_DEFAULT_INSTALL_DIR = r"C:\mihomo"
POWERSHELL = "powershell.exe"


def find_repo_root() -> str:
    """The checkout root, derived from this file: deploy/ -> scripts/ -> repo."""
    here = os.path.dirname(os.path.abspath(__file__))          # .../scripts/deploy
    return os.path.dirname(os.path.dirname(here))              # .../ -> the repo


def _macos_layout(repo_root: str, root: str):
    """Import ``mpm_macos.paths.Layout`` from the checkout and root it.

    The two source trees are prepended to ``sys.path`` exactly once at
    construction time, so the macOS paths always come from the real module
    instead of a copied constant.
    """
    for entry in (os.path.join(repo_root, "common", "python"),
                  os.path.join(repo_root, "macos", "python")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from mpm_macos.paths import Layout  # noqa: E402 - path set up just above
    return Layout(root=root)


class MacosPlatform(bootstrap.Platform):
    """Paths and the installer call for a macOS (arm64) host."""

    def __init__(self, repo_root: str, environ=None, root: str = "/"):
        self.repo_root = os.path.abspath(repo_root)
        self.environ = os.environ if environ is None else environ
        layout = _macos_layout(self.repo_root, root)
        self.name = MACOS
        self.supply_os = MACOS
        self.arch = "arm64"
        self.pool_path = os.path.join(layout.support, *POOL_RELATIVE)
        self.secret_path = layout.secret_file
        prefix = root.rstrip("/") or "/"
        self.staging_dir = os.path.join(prefix, MACOS_STAGING.lstrip("/"))
        chosen = str(self.environ.get("MPM_PYTHON") or "").strip()
        self.python = chosen or MACOS_DEFAULT_PYTHON

    def install(self, request: bootstrap.InstallRequest) -> int:
        env = dict(os.environ)
        env["MPM_MIHOMO_BINARY"] = os.path.join(request.staging_dir, "mihomo")
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (os.path.join(self.repo_root, "common", "python"),
                        os.path.join(self.repo_root, "macos", "python"),
                        env.get("PYTHONPATH")) if p)
        argv = [self.python, "-m", "mpm_macos.cli", "install"]
        if not request.start:
            argv.append("--no-start")
        return subprocess.call(argv, env=env, cwd=self.repo_root)


class WindowsPlatform(bootstrap.Platform):
    """Paths and the installer call for a Windows (amd64) host.

    ``scripts/deploy.ps1`` - provided by the later Windows stage - takes the
    staged binaries from ``-StagingDir``, installs them into the install dir,
    renders the config in Feishu-pool mode and registers the scheduled tasks.
    Paths are joined with ``ntpath`` so the backslash form is stable even on
    a POSIX test host.
    """

    def __init__(self, repo_root: str, environ=None, install_dir: str = ""):
        self.repo_root = os.path.abspath(repo_root)
        self.environ = os.environ if environ is None else environ
        chosen = str(install_dir or "").strip()
        if not chosen:
            chosen = str(self.environ.get("MIHOMO_DIR") or "").strip()
        if not chosen:
            chosen = WINDOWS_DEFAULT_INSTALL_DIR
        self.install_dir = chosen
        self.name = WINDOWS
        self.supply_os = WINDOWS
        self.arch = "amd64"
        self.pool_path = ntpath.join(chosen, *POOL_RELATIVE)
        self.secret_path = ntpath.join(chosen, "subscription.env")
        self.staging_dir = ntpath.join(chosen, "staging", "supply")

    def install(self, request: bootstrap.InstallRequest) -> int:
        script = ntpath.join(self.repo_root, "scripts", "deploy.ps1")
        argv = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-File", script, "-StagingDir", request.staging_dir]
        if not request.start:
            argv.append("-NoStart")
        return subprocess.call(argv, cwd=self.repo_root)


def get_platform(name: str, repo_root: str, environ=None) -> bootstrap.Platform:
    """Build the platform called ``name``; anything else is a ``ValueError``."""
    root = os.path.abspath(repo_root)
    if name == MACOS:
        return MacosPlatform(root, environ)
    if name == WINDOWS:
        return WindowsPlatform(root, environ)
    raise ValueError("unknown platform")
