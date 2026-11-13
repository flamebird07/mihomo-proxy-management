"""LaunchDaemon plist rendering and static audit.

Rendering goes through :mod:`plistlib` (the template is only a readable
skeleton), so the emitted document is always a valid property list.  The audit
is the security boundary: it refuses a plist that could leak a secret, run a
shell, add ``caffeinate`` or lose the crash-restart semantics.

KeepAlive semantics (launchd.plist(5)):

* ``KeepAlive = {SuccessfulExit: false}`` restarts the job when it exits with a
  non-zero status or is killed by a signal, and leaves it stopped after a clean
  ``exit 0``;
* ``launchctl bootout`` removes the job entirely, so an explicit ``stop`` is
  never raced by the restart policy.
"""

from __future__ import annotations

import plistlib
from typing import Any
from xml.sax.saxutils import escape

from mpm_common.errors import FailClosed

from .paths import LABEL

PLACEHOLDERS = (
    "__LABEL__",
    "__PYTHON__",
    "__PACKAGE_DIR__",
    "__STATE_DIR__",
    "__CONFIG__",
    "__BINARY__",
    "__RUN_DIR__",
    "__LOG_OUT__",
    "__LOG_ERR__",
)

FORBIDDEN_TOKENS = ("caffeinate", "://", "secret", "token", "password")

SHELL_NAMES = {"sh", "bash", "zsh", "dash", "/bin/sh", "/bin/bash", "/bin/zsh", "-c"}


def _values(
    *,
    label: str,
    python: str,
    package_dir: str,
    state_dir: str,
    config: str,
    binary: str,
    run_dir: str,
    log_out: str,
    log_err: str,
) -> dict[str, str]:
    return {
        "__LABEL__": label,
        "__PYTHON__": python,
        "__PACKAGE_DIR__": package_dir,
        "__STATE_DIR__": state_dir,
        "__CONFIG__": config,
        "__BINARY__": binary,
        "__RUN_DIR__": run_dir,
        "__LOG_OUT__": log_out,
        "__LOG_ERR__": log_err,
    }


def render(
    template_text: str,
    *,
    label: str = LABEL,
    python: str,
    package_dir: str,
    state_dir: str,
    config: str,
    binary: str,
    run_dir: str,
    log_out: str,
    log_err: str,
) -> str:
    """Fill the template, validate it with plistlib and re-serialise it."""
    if label != LABEL:
        raise FailClosed(f"refusing to render a foreign launchd label: {label}")
    values = _values(
        label=label,
        python=python,
        package_dir=package_dir,
        state_dir=state_dir,
        config=config,
        binary=binary,
        run_dir=run_dir,
        log_out=log_out,
        log_err=log_err,
    )
    text = template_text
    for name, value in values.items():
        text = text.replace(name, escape(value))
    leftover = [name for name in PLACEHOLDERS if name in text]
    if leftover:
        raise FailClosed("unresolved plist placeholders: " + ", ".join(leftover))
    try:
        document = plistlib.loads(text.encode("utf-8"))
    except Exception as exc:
        raise FailClosed(f"rendered plist is not valid: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise FailClosed("rendered plist root is not a dictionary")
    audit(document, label=label, state_dir=state_dir)
    _scan_values(document)
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def render_from_file(template_path: str, **kwargs: Any) -> str:
    with open(template_path, "r", encoding="utf-8") as handle:
        return render(handle.read(), **kwargs)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FailClosed(f"plist audit failed: {message}")


def audit(document: Any, *, label: str = LABEL, state_dir: str | None = None) -> dict[str, Any]:
    """Static audit of a parsed LaunchDaemon document."""
    _require(isinstance(document, dict), "root is not a dictionary")
    _require(document.get("Label") == label, "Label must be the project label")
    _require(document.get("RunAtLoad") is True, "RunAtLoad must be true")
    _require(
        document.get("KeepAlive") == {"SuccessfulExit": False},
        "KeepAlive must be {SuccessfulExit: false} (crash-only restart)",
    )
    _require(document.get("ThrottleInterval") == 5, "ThrottleInterval must be 5")
    _require(
        document.get("ProcessType") == "Interactive",
        "ProcessType must be Interactive",
    )

    arguments = document.get("ProgramArguments")
    _require(isinstance(arguments, list) and bool(arguments), "ProgramArguments missing")
    _require(
        all(isinstance(item, str) and item for item in arguments),
        "ProgramArguments must be a list of non-empty strings",
    )
    joined = " ".join(arguments)
    for token in FORBIDDEN_TOKENS:
        _require(token not in joined.lower(), f"ProgramArguments must not contain '{token}'")
    _require(
        not any(item in SHELL_NAMES for item in arguments),
        "ProgramArguments must not invoke a shell",
    )
    _require(
        any(item.endswith(("python3", "python3.11", "python3.12", "python3.13")) for item in arguments[:1]),
        "ProgramArguments[0] must be a Python interpreter",
    )
    _require("-m" in arguments, "ProgramArguments must start the package with -m")
    _require("mpm_macos.runner" in arguments, "ProgramArguments must point at mpm_macos.runner")

    working = document.get("WorkingDirectory")
    _require(isinstance(working, str) and working.startswith("/"), "WorkingDirectory must be absolute")
    if state_dir is not None:
        _require(working == state_dir, "WorkingDirectory must be the state directory")

    for key in ("StandardOutPath", "StandardErrorPath"):
        value = document.get(key)
        _require(isinstance(value, str) and value.startswith("/"), f"{key} must be an absolute path")

    environment = document.get("EnvironmentVariables", {})
    _require(isinstance(environment, dict), "EnvironmentVariables must be a dictionary")
    _require(
        not any("secret" in str(key).lower() for key in environment),
        "EnvironmentVariables must not carry a secret",
    )

    return {
        "label": document["Label"],
        "keep_alive": dict(document["KeepAlive"]),
        "throttle_interval": document["ThrottleInterval"],
        "program_arguments": list(arguments),
        "working_directory": working,
    }


def _scan_values(value: Any, *, path: str = "") -> None:
    """Every string in the document is checked; the XML DTD is not."""
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_values(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_values(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower()
        for token in FORBIDDEN_TOKENS:
            _require(token not in lowered, f"{path or 'value'} must not contain '{token}'")


def audit_text(text: str, *, label: str = LABEL, state_dir: str | None = None) -> dict[str, Any]:
    """Audit the serialised plist (values are scanned, the XML DTD is not)."""
    try:
        document = plistlib.loads(text.encode("utf-8"))
    except Exception as exc:
        raise FailClosed(f"plist is not valid: {type(exc).__name__}") from exc
    result = audit(document, label=label, state_dir=state_dir)
    _scan_values(document)
    return result


__all__ = [
    "FORBIDDEN_TOKENS",
    "PLACEHOLDERS",
    "audit",
    "audit_text",
    "render",
    "render_from_file",
]
