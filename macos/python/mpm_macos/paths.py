"""Darwin filesystem layout, rooted at a single prefix.

On a real host ``root`` is ``/``; every test passes a throwaway directory, so
no lifecycle code ever touches the host.  Paths follow the plan's system-level
layout:

    /Library/LaunchDaemons/com.mihomo-proxy-management.core.plist
    /usr/local/bin/mihomo-proxy-management
    /usr/local/libexec/mihomo-proxy-management/current/{mihomo,runner}
    /usr/local/share/mihomo-proxy-management/{python,templates,scripts}
    /Library/Application Support/mihomo-proxy-management/{config.yaml,providers,state,logs}
    /var/run/mihomo-proxy-management/{lifecycle.lock,core.lock}
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PROJECT = "mihomo-proxy-management"
LABEL = f"com.{PROJECT}.core"
BINARY_NAME = "mihomo"
RUNNER_NAME = "runner"
PLIST_NAME = f"{LABEL}.plist"

SUPPORT_DIR_NAME = PROJECT
RUN_DIR_NAME = PROJECT


@dataclass(frozen=True)
class Layout:
    """All fixed system locations, rooted at ``root``."""

    root: str = "/"

    # ---- launchd ------------------------------------------------------
    @property
    def plist_dir(self) -> str:
        return os.path.join(self.root, "Library", "LaunchDaemons")

    @property
    def plist(self) -> str:
        return os.path.join(self.plist_dir, PLIST_NAME)

    # ---- executables / read-only data ---------------------------------
    @property
    def usr_local_bin(self) -> str:
        return os.path.join(self.root, "usr", "local", "bin")

    @property
    def cli_entry(self) -> str:
        return os.path.join(self.usr_local_bin, PROJECT)

    @property
    def libexec(self) -> str:
        return os.path.join(self.root, "usr", "local", "libexec", PROJECT)

    @property
    def libexec_current(self) -> str:
        return os.path.join(self.libexec, "current")

    @property
    def binary(self) -> str:
        return os.path.join(self.libexec_current, BINARY_NAME)

    @property
    def runner(self) -> str:
        return os.path.join(self.libexec_current, RUNNER_NAME)

    @property
    def share(self) -> str:
        return os.path.join(self.root, "usr", "local", "share", PROJECT)

    @property
    def share_python(self) -> str:
        """Installed copy of ``mpm_common`` + ``mpm_macos``."""
        return os.path.join(self.share, "python")

    @property
    def template_dir(self) -> str:
        return os.path.join(self.share, "templates")

    @property
    def plist_template(self) -> str:
        return os.path.join(self.template_dir, PLIST_NAME + ".template")

    @property
    def config_template(self) -> str:
        return os.path.join(self.template_dir, "config.yaml.template")

    @property
    def scripts_dir(self) -> str:
        return os.path.join(self.libexec, "scripts")

    @property
    def convert_sub(self) -> str:
        return os.path.join(self.scripts_dir, "convert_sub.py")

    # ---- support (configuration + state) ------------------------------
    @property
    def support(self) -> str:
        return os.path.join(self.root, "Library", "Application Support", SUPPORT_DIR_NAME)

    @property
    def config(self) -> str:
        return os.path.join(self.support, "config.yaml")

    @property
    def config_tun_backup(self) -> str:
        """Last known-good TUN config, kept for recovery from mixed mode."""
        return os.path.join(self.state_dir, "config.tun.yaml")

    @property
    def providers(self) -> str:
        return os.path.join(self.support, "providers")

    @property
    def provider_file(self) -> str:
        return os.path.join(self.providers, "subscription.yaml")

    @property
    def state_dir(self) -> str:
        return os.path.join(self.support, "state")

    @property
    def secret_file(self) -> str:
        return os.path.join(self.state_dir, "subscription.env")

    @property
    def network_backup(self) -> str:
        return os.path.join(self.state_dir, "network-proxy-backup.json")

    @property
    def deployment_state(self) -> str:
        return os.path.join(self.state_dir, "deployment.json")

    @property
    def staging_dir(self) -> str:
        return os.path.join(self.state_dir, "staging")

    @property
    def logs(self) -> str:
        return os.path.join(self.support, "logs")

    @property
    def log_out(self) -> str:
        return os.path.join(self.logs, "core.out.log")

    @property
    def log_err(self) -> str:
        return os.path.join(self.logs, "core.err.log")

    # ---- runtime ------------------------------------------------------
    @property
    def run_dir(self) -> str:
        return os.path.join(self.root, "var", "run", RUN_DIR_NAME)

    @property
    def lifecycle_lock(self) -> str:
        return os.path.join(self.run_dir, "lifecycle.lock")

    @property
    def core_lock(self) -> str:
        return os.path.join(self.run_dir, "core.lock")

    # ---- helpers ------------------------------------------------------
    def deletable_prefixes(self) -> list[str]:
        return [
            self.cli_entry,
            self.libexec,
            self.share,
            self.support,
            self.run_dir,
            self.plist,
        ]

    def is_project_path(self, path: str) -> bool:
        abspath = os.path.normpath(os.path.abspath(path))
        for prefix in self.deletable_prefixes():
            real = os.path.normpath(os.path.abspath(prefix))
            if abspath == real or abspath.startswith(real + os.sep):
                return True
        return False


def managed_dirs(layout: Layout) -> list[tuple[str, int]]:
    """Directories a system install needs, with their exact modes."""
    return [
        (layout.plist_dir, 0o755),
        (layout.usr_local_bin, 0o755),
        (layout.libexec, 0o755),
        (layout.libexec_current, 0o755),
        (layout.share, 0o755),
        (layout.share_python, 0o755),
        (layout.template_dir, 0o755),
        (layout.scripts_dir, 0o755),
        (layout.support, 0o755),
        (layout.providers, 0o700),
        (layout.state_dir, 0o700),
        (layout.logs, 0o750),
        (layout.run_dir, 0o700),
    ]


__all__ = [
    "BINARY_NAME",
    "LABEL",
    "PLIST_NAME",
    "PROJECT",
    "RUNNER_NAME",
    "Layout",
    "managed_dirs",
]
