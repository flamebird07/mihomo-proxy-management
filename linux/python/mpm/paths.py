"""Filesystem layout for the Linux *system* profile.

Every path is derived from a single ``root`` prefix.  On a real host ``root``
is ``/``; the test-suite passes a throwaway directory instead so that no
lifecycle code ever touches the host.  The product only ever installs the
system profile (D2=A) - there is deliberately no user-profile layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

PROJECT = "mihomo-proxy-management"
UNIT_NAME = f"{PROJECT}.service"


@dataclass(frozen=True)
class Layout:
    """All fixed system locations, rooted at ``root``."""

    root: str = "/"

    # ---- executables / read-only data ---------------------------------
    @property
    def usr_local_bin(self) -> str:
        return os.path.join(self.root, "usr", "local", "bin")

    @property
    def cli_entry(self) -> str:
        return os.path.join(self.usr_local_bin, PROJECT)

    @property
    def libexec(self) -> str:
        return os.path.join(self.root, "usr", "libexec", PROJECT)

    def libexec_version_dir(self, version: str) -> str:
        return os.path.join(self.libexec, version)

    def libexec_binary(self, version: str) -> str:
        return os.path.join(self.libexec_version_dir(version), PROJECT_BINARY)

    @property
    def libexec_current(self) -> str:
        """Symlink switched atomically to a pinned version directory."""
        return os.path.join(self.libexec, "current")

    @property
    def binary(self) -> str:
        """Stable path used by the unit's ``ExecStart``."""
        return os.path.join(self.libexec_current, PROJECT_BINARY)

    @property
    def share(self) -> str:
        return os.path.join(self.root, "usr", "share", PROJECT)

    @property
    def template_dir(self) -> str:
        return os.path.join(self.share, "templates")

    @property
    def share_python(self) -> str:
        """Installed copy of the mpm package (the CLI entry points here)."""
        return os.path.join(self.share, "python")

    @property
    def installed_lock(self) -> str:
        """Installed copy of the audited supply lock."""
        return os.path.join(self.share, "supply", "mihomo.lock.json")

    @property
    def unit_template(self) -> str:
        return os.path.join(self.template_dir, f"{UNIT_NAME}.template")

    @property
    def config_template(self) -> str:
        return os.path.join(self.template_dir, "config.yaml.template")

    # ---- configuration (inputs + overrides) --------------------------
    @property
    def etc(self) -> str:
        return os.path.join(self.root, "etc", PROJECT)

    @property
    def subscription_env(self) -> str:
        return os.path.join(self.etc, "subscription.env")

    @property
    def controller_secret_file(self) -> str:
        return os.path.join(self.etc, "controller.secret")

    @property
    def overrides_dir(self) -> str:
        """Non-secret user overrides; kept by plain uninstall (D14=2)."""
        return os.path.join(self.etc, "overrides.d")

    # ---- state (mihomo -d == WorkingDirectory) ------------------------
    @property
    def var_lib(self) -> str:
        return os.path.join(self.root, "var", "lib", PROJECT)

    @property
    def config(self) -> str:
        return os.path.join(self.var_lib, "config.yaml")

    @property
    def providers(self) -> str:
        return os.path.join(self.var_lib, "providers")

    @property
    def state_dir(self) -> str:
        """Our own non-secret state, kept inside the state directory."""
        return os.path.join(self.var_lib, "state")

    @property
    def degraded_file(self) -> str:
        return os.path.join(self.state_dir, "degraded.json")

    @property
    def plan_file(self) -> str:
        return os.path.join(self.state_dir, "plan.json")

    @property
    def backups_dir(self) -> str:
        return os.path.join(self.state_dir, "backups")

    @property
    def backup_index(self) -> str:
        return os.path.join(self.backups_dir, "index.json")

    @property
    def staging_dir(self) -> str:
        return os.path.join(self.state_dir, "staging")

    @property
    def geoip(self) -> str:
        return os.path.join(self.var_lib, "GeoIP.dat")

    @property
    def geosite(self) -> str:
        return os.path.join(self.var_lib, "GeoSite.dat")

    # ---- runtime ------------------------------------------------------
    @property
    def run(self) -> str:
        return os.path.join(self.root, "run", PROJECT)

    # ---- systemd ------------------------------------------------------
    @property
    def unit_dir(self) -> str:
        return os.path.join(self.root, "etc", "systemd", "system")

    @property
    def unit(self) -> str:
        return os.path.join(self.unit_dir, UNIT_NAME)

    @property
    def unit_dropin_dir(self) -> str:
        return f"{self.unit}.d"

    # ---- helpers ------------------------------------------------------
    def is_project_path(self, path: str) -> bool:
        """True when ``path`` is inside one of the fixed project prefixes.

        Used by purge: anything that fails this check is never deleted.
        """
        abspath = os.path.normpath(os.path.abspath(path))
        for prefix in self.deletable_prefixes():
            real = os.path.normpath(os.path.abspath(prefix))
            if abspath == real or abspath.startswith(real + os.sep):
                return True
        return False

    def deletable_prefixes(self) -> list[str]:
        return [
            self.cli_entry,
            self.libexec,
            self.share,
            self.etc,
            self.var_lib,
            self.run,
            self.unit,
            self.unit_dropin_dir,
        ]


PROJECT_BINARY = "mihomo"

# Directories that must exist for a system install, with their exact modes.
# (path, octal mode)
def managed_dirs(layout: Layout) -> list[tuple[str, int]]:
    return [
        (layout.usr_local_bin, 0o755),
        (layout.libexec, 0o755),
        (layout.share, 0o755),
        (layout.template_dir, 0o755),
        (layout.etc, 0o755),
        (layout.overrides_dir, 0o750),
        (layout.var_lib, 0o750),
        (layout.providers, 0o700),
        (layout.state_dir, 0o700),
        (layout.backups_dir, 0o700),
        (layout.run, 0o700),
        (layout.unit_dir, 0o755),
    ]


__all__ = ["Layout", "PROJECT", "UNIT_NAME", "PROJECT_BINARY", "managed_dirs"]
