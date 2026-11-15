"""Shared install-context construction for the CLI, ``mpm-switch`` and boot.

One place decides which absolute mihomo path the runtime facts register, so
``mihomo-proxy-management preflight`` and the ``boot-switch`` LaunchDaemon can
never disagree about the binary:

* the **installed** binary (``Layout.binary``) is the *only* runtime path: it
  must exist as a regular, executable file, because it is what the core runner
  executes and what ``mihomo -t`` validates against.  When it is unusable the
  runtime resolves to nothing and preflight/configure/start fail closed - an
  environment override must never let verification run one file while the
  runner starts another;
* ``MPM_MIHOMO_BINARY`` is an **install-source** override only: a fresh
  ``install`` copies it under ``libexec`` and re-registers the installed path
  afterwards.  It is never registered as a runtime capability;
* the ``PATH`` is **never** consulted at runtime.  A LaunchDaemon runs with
  ``/usr/bin:/bin:/usr/sbin:/sbin`` and the installed binary is not there - the
  2026-11-15 ``launchctl kickstart`` run failed preflight exactly because boot
  built its facts from a bare ``PATH`` lookup while the CLI corrected them with
  ``Layout.binary``.  ``PATH`` stays an install-source candidate only, resolved
  separately by :func:`resolve_install_source` (override first, then the
  context's ``PATH``) and used by ``lifecycle._install_binary``.

This module imports only ``preflight``/``paths`` at module level; ``lifecycle``
is imported inside :func:`build_context`.  ``lifecycle`` in turn resolves the
``mihomo -t`` binary through :func:`resolve_runtime_binary`, and both plists
inject the same absolute installed path as :data:`BINARY_ENV` - so preflight,
config verification and the runner all agree on one file, and none of the
entries (``cli``/``switch``/``boot``) has to import another one.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from mpm_common.executor import Executor

from . import preflight as preflight_mod
from .paths import Layout

if TYPE_CHECKING:
    from .lifecycle import Context

#: Environment variable carrying the absolute mihomo path: the install source
#: override, and the same absolute installed path injected into both
#: LaunchDaemon plists (no secret, no subscription URL - just a path).
BINARY_ENV = "MPM_MIHOMO_BINARY"
ROOT_ENV = "MPM_ROOT"
PYTHON_ENV = "MPM_PYTHON"


def is_usable_binary(path: str) -> bool:
    """A regular executable file (host filesystem semantics)."""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_runtime_binary(layout: Layout) -> str:
    """The single absolute mihomo path every runtime check must agree on.

    Only ``layout.binary`` qualifies: it must exist as a regular, executable
    file, because the core plist runner executes exactly that path.  Anything
    else (missing, a directory, not executable) resolves to ``""``; the caller
    then registers no mihomo capability and preflight/configure/start fail
    closed.  ``MPM_MIHOMO_BINARY`` is deliberately *not* consulted here - it is
    an install source (:func:`resolve_install_source`), and honoring it at
    runtime would validate one file while the runner starts another.
    """
    return layout.binary if is_usable_binary(layout.binary) else ""


def resolve_install_source(environ: dict[str, str] | None = None) -> str:
    """The absolute mihomo file a fresh ``install`` copies from.

    Separate from the runtime resolution: ``MPM_MIHOMO_BINARY`` wins,
    otherwise ``mihomo`` is looked up on the *context's* ``PATH`` (never the
    process environment's).  The candidate must be a regular executable file;
    anything else resolves to ``""`` and the installer fails closed.  The
    result is an install source only - neither the override nor a PATH find is
    ever registered as a runtime capability (:func:`register_binary` accepts
    the installed file alone).
    """
    env = os.environ if environ is None else environ
    override = (env.get(BINARY_ENV) or "").strip()
    if override:
        candidate = os.path.abspath(override)
    else:
        found = shutil.which("mihomo", path=env.get("PATH", ""))
        if not found:
            return ""
        candidate = os.path.abspath(found)
    return candidate if is_usable_binary(candidate) else ""


def register_binary(
    facts: "preflight_mod.PlatformFacts",
    layout: Layout,
) -> "preflight_mod.PlatformFacts":
    """Replace the facts' mihomo capability with the installed absolute path.

    Any other mihomo entry (a PATH find, an install-source override, a stale
    one) is dropped when the installed binary is unusable: runtime verification
    and start must use the same absolute path the runner executes, never
    whatever happens to be on ``PATH`` or in the environment.
    """
    commands = dict(facts.commands)
    binary = resolve_runtime_binary(layout)
    if binary:
        commands["mihomo"] = binary
    else:
        commands.pop("mihomo", None)
    return replace(facts, commands=commands)


def build_context(
    root: str | None = None,
    *,
    environ: dict[str, str] | None = None,
    python: str | None = None,
    collect: Callable[[], "preflight_mod.PlatformFacts"] | None = None,
) -> Context:
    """The one context constructor every entry point shares.

    ``collect`` injects host facts in tests (production uses
    :func:`mpm_macos.preflight.collect_facts`); everything else is identical
    for ``mihomo-proxy-management <command>``, ``mpm-switch`` and the boot
    entry.
    """
    from .lifecycle import Context

    env = dict(os.environ) if environ is None else dict(environ)
    layout = Layout(root or env.get(ROOT_ENV, "/"))
    reader = collect or preflight_mod.collect_facts
    return Context(
        layout=layout,
        facts=register_binary(reader(), layout),
        executor=Executor(),
        environ=env,
        system_profile=True,
        python=python or env.get(PYTHON_ENV) or sys.executable,
    )


__all__ = [
    "BINARY_ENV",
    "PYTHON_ENV",
    "ROOT_ENV",
    "build_context",
    "is_usable_binary",
    "register_binary",
    "resolve_install_source",
    "resolve_runtime_binary",
]
