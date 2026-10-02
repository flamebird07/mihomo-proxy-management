"""systemd unit rendering (section 7) - system profile, root, conservative.

Deliberate choices:

* ``WorkingDirectory`` and ``-d`` point at the *same* state directory so
  relative provider paths cannot drift.
* ``ExecStart`` contains only the binary and ``-d``; no URL, no secret, no
  environment values (the secret lives in the 0600 config file instead).
* ``ExecStartPre`` runs our own ``configure --check-only`` directly through the
  installed CLI wrapper, so config validation happens before the process starts
  and writes nothing.
* No ``ConditionPathExists=!config.yaml`` (that negative form from the design
  draft would block the very first render).  A *positive* condition on the
  rendered config is only added once the file exists, which ``install``
  guarantees.
* No ``PrivateDevices=`` (would hide ``/dev/net/tun``) and no
  ``PrivateUsers=`` (would break root-owned state mapping).
* No ``CAP_SYS_ADMIN`` granted by default.  TUN needs it, so it is added only
  for the TUN profile; degraded mode runs without any capability.
* Restart/StartLimit prevent crash loops.
"""

from __future__ import annotations

import re

from .errors import FailClosed

UNIT_FILENAME = "mihomo-proxy-management.service"

# ExecStartPre/ExecStart must never carry secret-bearing text.
_FORBIDDEN_UNIT_TOKENS = ("MPM_SUBSCRIPTION_URL", "MPM_CN_SUBSCRIPTION_URL", "Environment=")

# An interpreter must never *run the shell wrapper*: it would parse a /bin/sh
# script as Python (or vice versa) and the unit could never start.
_INTERPRETER_PREFIX = re.compile(
    r"(?:^|/)(?:python3?(?:\.\d+)*|pypy3?|sh|bash|dash|perl|ruby)$"
)


def render(
    *,
    binary: str,
    state_dir: str,
    run_dir: str,
    cli_entry: str,
    unit_name: str = UNIT_FILENAME,
    tun: bool,
) -> str:
    """Render the unit text.  ``tun`` decides the capability set only."""
    if not binary or not state_dir:
        raise FailClosed("unit rendering needs binary and state_dir")
    if not cli_entry:
        raise FailClosed("unit rendering needs the installed CLI entry path")
    capabilities = (
        "AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW\nCapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW\n"
        if tun
        else "CapabilityBoundingSet=\n"
    )
    lines = [
        "[Unit]",
        f"Description=mihomo-proxy-management (mihomo {('TUN' if tun else 'mixed-port/degraded')} profile)",
        "Documentation=https://github.com/flamebird07/mihomo-proxy-management",
        "After=network-online.target",
        "Wants=network-online.target",
        # StartLimit* are [Unit] keys since systemd 229; in [Service] they are
        # silently ignored (and flagged by systemd-analyze verify).
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        # config validation happens here, before ExecStart.  The installed CLI
        # wrapper is itself an executable (#!/bin/sh, mode 0755) and selects its
        # own interpreter - prefixing an interpreter here would make systemd
        # parse a shell script as Python and the service could never start.
        f"ExecStartPre={cli_entry} configure --check-only --quiet",
        f"WorkingDirectory={state_dir}",
        # only the binary and -d; the 0600 config.yaml inside holds the URLs
        f"ExecStart={binary} -d {state_dir}",
        "",
        "Restart=on-failure",
        "RestartSec=5s",
        "KillMode=control-group",
        "TimeoutStopSec=30s",
        "",
        "UMask=0077",
        "NoNewPrivileges=true",
        "ProtectSystem=full",
        "ProtectHome=true",
        "PrivateTmp=true",
        # PrivateDevices/PrivateUsers deliberately NOT enabled: they would hide
        # /dev/net/tun and remap the root-owned state directory.
        capabilities,
        "RuntimeDirectory=" + run_dir.rsplit("/", 1)[-1],
        "RuntimeDirectoryMode=0700",
        "SyslogLevel=info",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    text = "\n".join(lines)
    audit(text, tun=tun)
    return text


def audit(text: str, *, tun: bool) -> None:
    """Static checks on the unit we are about to install."""

    def _need(condition: bool, message: str) -> None:
        if not condition:
            raise FailClosed(f"unit audit failed: {message}")

    _need("[Unit]" in text and "[Service]" in text and "[Install]" in text, "missing sections")
    _need(re.search(r"^Type=simple$", text, re.M) is not None, "Type=simple required")
    _need(re.search(r"^Restart=on-failure$", text, re.M) is not None, "Restart=on-failure required")
    _need(re.search(r"^RestartSec=\d+s?$", text, re.M) is not None, "RestartSec required")
    _need(re.search(r"^KillMode=control-group$", text, re.M) is not None, "KillMode required")
    # StartLimit* only have an effect in [Unit]; if they land in [Service],
    # systemd ignores them and a crash loop is no longer bounded.
    unit_section = text.split("[Service]")[0]
    _need(
        re.search(r"^StartLimitIntervalSec=\d+", unit_section, re.M) is not None
        and re.search(r"^StartLimitBurst=\d+", unit_section, re.M) is not None,
        "StartLimit pair must live in [Unit] (crash-loop protection)",
    )
    _need(re.search(r"^UMask=0077$", text, re.M) is not None, "UMask=0077 required")
    _need(re.search(r"^NoNewPrivileges=true$", text, re.M) is not None, "NoNewPrivileges required")
    _need(re.search(r"^ProtectSystem=full$", text, re.M) is not None, "ProtectSystem=full required")
    _need(re.search(r"^ProtectHome=true$", text, re.M) is not None, "ProtectHome required")
    _need(re.search(r"^PrivateTmp=true$", text, re.M) is not None, "PrivateTmp required")
    _need(
        re.search(r"^PrivateDevices=true$", text, re.M) is None,
        "PrivateDevices must not be enabled (hides /dev/net/tun)",
    )
    _need(
        re.search(r"^PrivateUsers=true$", text, re.M) is None,
        "PrivateUsers must not be enabled (breaks root-owned state dir)",
    )
    _need(
        re.search(r"^ExecStartPre=.*configure --check-only", text, re.M) is not None,
        "ExecStartPre must run the project's configure --check-only",
    )
    # ExecStartPre must *exec* the installed wrapper.  Handing the shell wrapper
    # to an interpreter (python3 /usr/local/bin/...) makes systemd parse a shell
    # script as Python and the unit can never start.
    exec_pre = re.search(r"^ExecStartPre=(?P<v>.*)$", text, re.M)
    _need(exec_pre is not None, "ExecStartPre missing")
    pre_program = exec_pre.group("v").split()[0]
    _need(
        pre_program.startswith("/") and _INTERPRETER_PREFIX.search(pre_program) is None,
        "ExecStartPre must invoke the installed CLI wrapper as an absolute "
        "executable, never through an interpreter",
    )

    exec_start = re.search(r"^ExecStart=(?P<v>.*)$", text, re.M)
    _need(exec_start is not None, "ExecStart missing")
    value = exec_start.group("v")
    _need(
        re.fullmatch(r"/[^\s]+ -d /[^\s]+", value) is not None,
        "ExecStart must be exactly '<binary> -d <state-dir>'",
    )
    working = re.search(r"^WorkingDirectory=(?P<v>.*)$", text, re.M)
    _need(working is not None, "WorkingDirectory missing")
    state_from_exec = value.split(" -d ", 1)[1].strip()
    _need(
        working.group("v").rstrip("/") == state_from_exec.rstrip("/"),
        "WorkingDirectory must equal the -d state directory",
    )
    for token in _FORBIDDEN_UNIT_TOKENS:
        _need(token not in text, f"unit must not contain {token}")
    _need("pkill" not in text and "killall" not in text and "pgrep" not in text, "no process scanning")
    _need(
        re.search(r"^ConditionPathExists=!/", text, re.M) is None,
        "negative ConditionPathExists is forbidden (blocks first render)",
    )
    if not tun:
        _need(
            re.search(r"^AmbientCapabilities=.*CAP_SYS_ADMIN", text, re.M) is None,
            "degraded profile must not grant CAP_SYS_ADMIN",
        )
    _need(
        re.search(r"^CapabilityBoundingSet=.*CAP_SYS_ADMIN", text, re.M) is None,
        "CAP_SYS_ADMIN must never be granted by default",
    )


__all__ = ["UNIT_FILENAME", "audit", "render"]
