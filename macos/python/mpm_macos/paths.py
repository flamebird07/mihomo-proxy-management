"""Darwin filesystem layout, rooted at a single prefix.

On a real host ``root`` is ``/``; every test passes a throwaway directory, so
no lifecycle code ever touches the host.  Paths follow the plan's system-level
layout:

    /Library/LaunchDaemons/com.mihomo-proxy-management.core.plist
    /Library/LaunchDaemons/com.mihomo-proxy-management.boot-switch.plist
    /usr/local/bin/mihomo-proxy-management
    /usr/local/libexec/mihomo-proxy-management/current/{mihomo,runner}
    /usr/local/share/mihomo-proxy-management/{python,templates,scripts}
    /Library/Application Support/mihomo-proxy-management/{config.yaml,providers,state,logs}
    /var/run/mihomo-proxy-management/{lifecycle.lock,core.lock}

``support`` is mihomo's home (``-d``): the provider file, the geo databases and
the cache mihomo writes all live there.  ``state`` only holds this project's own
state files (secret, deployment record, proxy snapshot, staging).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PROJECT = "mihomo-proxy-management"
LABEL = f"com.{PROJECT}.core"
#: Second system job: runs once at boot and hands the machine to mihomo through
#: the same safe flow as ``mpm-switch mihomo``.  It is *not* the core job and
#: must never be disabled at boot - but it must never restart either (no
#: ``KeepAlive``), or a failed switch would flap the network.
BOOT_LABEL = f"com.{PROJECT}.boot-switch"
#: Third system job: fires the Feishu speedtest sync (``--speedtest-write``,
#: 表2/表3) every ``StartInterval`` seconds.  Like the boot entry it must never
#: restart itself (no ``KeepAlive``): a failing tick is simply retried on the
#: next interval, and a restart policy would only pile up overlapping runs.
FEISHU_SYNC_LABEL = f"com.{PROJECT}.feishu-sync"
XIGUA_IPPOOL_LABEL = f"com.{PROJECT}.xigua-ippool"
BINARY_NAME = "mihomo"
RUNNER_NAME = "runner"
PLIST_NAME = f"{LABEL}.plist"
BOOT_PLIST_NAME = f"{BOOT_LABEL}.plist"
FEISHU_SYNC_PLIST_NAME = f"{FEISHU_SYNC_LABEL}.plist"

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

    @property
    def boot_plist(self) -> str:
        """The boot-switch job: the only LaunchDaemon that runs at boot."""
        return os.path.join(self.plist_dir, BOOT_PLIST_NAME)

    @property
    def feishu_sync_plist(self) -> str:
        """The Feishu speedtest sync job (StartInterval-driven, no boot run)."""
        return os.path.join(self.plist_dir, FEISHU_SYNC_PLIST_NAME)

    @property
    def xigua_ippool_plist(self) -> str:
        return os.path.join(self.plist_dir, f"{XIGUA_IPPOOL_LABEL}.plist")

    # ---- executables / read-only data ---------------------------------
    @property
    def usr_local_bin(self) -> str:
        return os.path.join(self.root, "usr", "local", "bin")

    @property
    def cli_entry(self) -> str:
        return os.path.join(self.usr_local_bin, PROJECT)

    @property
    def switch_entry(self) -> str:
        """``mpm-switch`` - the user-facing Nextin/mihomo switch."""
        return os.path.join(self.usr_local_bin, "mpm-switch")

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
    def boot_plist_template(self) -> str:
        return os.path.join(self.template_dir, BOOT_PLIST_NAME + ".template")

    @property
    def feishu_sync_plist_template(self) -> str:
        return os.path.join(self.template_dir, FEISHU_SYNC_PLIST_NAME + ".template")

    @property
    def config_template(self) -> str:
        return os.path.join(self.template_dir, "config.yaml.template")

    @property
    def scripts_dir(self) -> str:
        return os.path.join(self.libexec, "scripts")

    @property
    def convert_sub(self) -> str:
        return os.path.join(self.scripts_dir, "convert_sub.py")

    @property
    def sync_script(self) -> str:
        """Installed copy of ``scripts/sync-feishu-bitable.py``.

        The Feishu sync daemon imports it by path; it must sit next to the
        installed ``convert_sub.py`` because the script imports its sibling
        through ``sys.path``.
        """
        return os.path.join(self.scripts_dir, "sync-feishu-bitable.py")

    # ---- support (mihomo home + project state) ------------------------
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
    def mihomo_home(self) -> str:
        """mihomo's ``-d`` directory (also the LaunchDaemon WorkingDirectory).

        mihomo resolves a relative provider ``path`` against this directory and
        rejects any absolute provider path that is not inside it (or in
        ``SAFE_PATHS``).  ``providers/`` therefore must stay below ``support``:
        the rendered provider path is ``support/providers/subscription.yaml``.
        """
        return self.support

    @property
    def providers(self) -> str:
        return os.path.join(self.support, "providers")

    @property
    def provider_file(self) -> str:
        return os.path.join(self.providers, "subscription.yaml")

    @property
    def state_dir(self) -> str:
        """Project state only (secret, deployment record, snapshots); not ``-d``."""
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
    def boot_hold(self) -> str:
        """0600 hold stamp: a boot entry started while it exists exits at once.

        ``install`` creates it (``O_EXCL``) *before* ``launchctl bootstrap`` -
        ``RunAtLoad`` starts the boot entry right after the bootstrap, and
        without the stamp that run would perform the whole network switch
        while the operator watches.  Install removes the stamp only after the
        entry acknowledged it (see :attr:`boot_hold_ack`), so the next entry
        run (a real boot or a manual kickstart) performs the switch again.
        """
        return os.path.join(self.state_dir, "boot-switch.hold")

    @property
    def boot_hold_ack(self) -> str:
        """0600 ack: the boot entry saw the hold stamp and exited harmlessly.

        The entry writes it right before the zero-side-effect hold exit;
        ``install`` waits for it (together with "job loaded and no longer
        running") after the bootstrap and deletes it with the stamp.  Without
        the ack, "loaded" alone cannot tell whether the ``RunAtLoad`` process
        already ran, so deleting the stamp could race a later entry run into
        a network switch.
        """
        return os.path.join(self.state_dir, "boot-switch.hold-ack")

    @property
    def staging_dir(self) -> str:
        return os.path.join(self.state_dir, "staging")

    @property
    def feishu_state_dir(self) -> str:
        """Feishu sync state (tables map, run mirror, GPT history, lock, caches)."""
        return os.path.join(self.state_dir, "feishu")

    @property
    def logs(self) -> str:
        return os.path.join(self.support, "logs")

    @property
    def log_out(self) -> str:
        return os.path.join(self.logs, "core.out.log")

    @property
    def log_err(self) -> str:
        return os.path.join(self.logs, "core.err.log")

    @property
    def log_boot_out(self) -> str:
        return os.path.join(self.logs, "boot-switch.out.log")

    @property
    def log_boot_err(self) -> str:
        return os.path.join(self.logs, "boot-switch.err.log")

    @property
    def log_feishu_out(self) -> str:
        return os.path.join(self.logs, "feishu-sync.out.log")

    @property
    def log_feishu_err(self) -> str:
        return os.path.join(self.logs, "feishu-sync.err.log")

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
            self.switch_entry,
            self.libexec,
            self.share,
            self.support,
            self.run_dir,
            self.plist,
            self.boot_plist,
            self.feishu_sync_plist,
            self.xigua_ippool_plist,
        ]

    def to_target_path(self, path: str) -> str:
        """Render a host path as the macOS path recorded in a plist.

        A plist always describes the *target* host, never the machine that
        rendered it.  On the real host the root is ``/`` and this is the
        identity; the host-free tests run with a throwaway sandbox root (a
        Windows drive path included), so the sandbox prefix is replaced with
        ``/`` and separators become POSIX ones.
        """
        text = str(path)
        if os.sep == "/":
            return text
        root = os.path.normpath(os.path.abspath(self.root))
        candidate = os.path.normpath(os.path.abspath(text))
        prefix = root.rstrip(os.sep) + os.sep
        if candidate == root:
            return "/"
        if candidate.startswith(prefix):
            relative = candidate[len(prefix):]
            return "/" + relative.replace(os.sep, "/")
        return text.replace(os.sep, "/")

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
        (layout.feishu_state_dir, 0o700),
        (layout.logs, 0o750),
        (layout.run_dir, 0o700),
    ]


__all__ = [
    "BINARY_NAME",
    "BOOT_LABEL",
    "BOOT_PLIST_NAME",
    "FEISHU_SYNC_LABEL",
    "FEISHU_SYNC_PLIST_NAME",
    "LABEL",
    "PLIST_NAME",
    "PROJECT",
    "RUNNER_NAME",
    "Layout",
    "managed_dirs",
]
