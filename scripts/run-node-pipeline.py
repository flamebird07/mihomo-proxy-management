"""Sequential node pipeline for the scheduled task: collect -> pool -> ops -> provide.

**Windows entry.**  The macOS counterpart is
``macos/python/mpm_macos/node_pipeline.py`` (launchd job, adds a stable-GPT
switch step and a presence-flip config re-render).  Keep the step list in
sync across both files when adding/changing steps - see
``docs/CROSS-PLATFORM.md`` section 3/6.

The ``pool`` step only runs on a Feishu-pool host (``proxy-providers.
subscription`` is a ``type: file`` provider pointing at the managed pool):
it rebuilds ``providers/feishu-pool.yaml`` from the 「节点测速总表」 and then
hot-reloads the live provider when its content changed.  Subscription hosts
keep the original three steps and their original argv.

Each step runs even if a previous one failed: a failed collect still lets ops
refresh measurements on the existing table, and a failed ops round still lets
provide republish the last valid set.  Exit code is 1 if any step failed.

On a Feishu-pool host every step receives the *same* credentials env file, the
one installed by the bootstrap (``MIHOMO_DIR\\subscription.env`` on Windows,
resolved at run time by :func:`portable_paths.default_env_file`).  A fresh
Feishu-only host does not have the repo's ``config/subscription.env``, so the
old unconditional ``--sources-env <repo>/config/subscription.env`` made every
tick exit 2.  In pool mode the collect step is supplemental and only runs when
there is something to collect: a configured ``SUBSCRIPTION_URL_n`` (in the
install env file or an existing valid repo source file) or an available
``source4`` app input.  Otherwise it is skipped with a log line and
pool/ops/provide run normally.

Whatever collect *does* return is authoritative: any non-zero exit is a real
failure.  ``node_provider`` uses 3 both for "no nodes collected" and for
write-permission/API faults, so it must never be treated as an empty tick.
"""

import hashlib
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import portable_paths as portable  # noqa: E402 - after the sys.path fix

LIVE_CONTROLLER = "http://127.0.0.1:9090"
POOL_PROVIDER_PATH = "./providers/feishu-pool.yaml"

#: Subscription slots the collect step may read (SUBSCRIPTION_URL_1..4).
_SUBSCRIPTION_KEYS = tuple(f"SUBSCRIPTION_URL_{index}" for index in range(1, 5))


def _install_path(*parts: str) -> str:
    """A path under the install directory (``MIHOMO_DIR`` or ``C:\\mihomo``).

    On Windows this is exactly the install directory; on a POSIX host the
    drive-letter default maps under ``$MPM_HOME`` (see ``portable_paths``).
    The explicit ``MIHOMO_DIR`` names the directory on every host, so the
    pipeline never falls back to a hardcoded administrator path.
    """
    return portable.portable_path(
        os.path.join(portable.windows_install_dir(), *parts)
    )


PRODUCTION_CONFIG = _install_path("config.yaml")
PRODUCTION_PROVIDER = _install_path("providers", "node-valid.yaml")
POOL_FILE = _install_path("providers", "feishu-pool.yaml")
POOL_SCRIPT = str(SCRIPTS / "feishu_pool.py")
POOL_MARKER = _install_path("logs", "pool.sha256")
LOG = _install_path("logs", "node-pipeline.log")


def _read_env_values(path: str):
    """Parsed env file, or ``None`` when it is missing/malformed (never raises)."""
    try:
        from node_provider.envfile import EnvFileError, read_env_file
    except ImportError:  # pragma: no cover - direct execution path guard
        sys.path.insert(0, str(SCRIPTS))
        from node_provider.envfile import EnvFileError, read_env_file
    try:
        return read_env_file(path)
    except EnvFileError:
        return None


def _is_managed_pool_path(path) -> bool:
    """True only for the managed pool path, never a user's custom file provider."""
    if not isinstance(path, str) or not path.strip():
        return False
    normalized = path.replace("\\", "/")
    if normalized in (POOL_PROVIDER_PATH, POOL_PROVIDER_PATH[2:]):
        return True
    try:
        return os.path.normpath(path) == os.path.normpath(POOL_FILE)
    except (OSError, ValueError):
        return False


def pool_mode(config_path: str) -> bool:
    """Whether ``config_path`` is a Feishu-pool config.

    True only when ``proxy-providers.subscription`` is a ``type: file``
    provider at the managed pool path.  A missing/undecodable/unparsable file
    or a structurally wrong document is just ``False`` - never an exception.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return False
    try:
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            document = yaml.safe_load(handle)
    except Exception:  # noqa: BLE001 - any read/parse failure means "not pool mode"
        return False
    if not isinstance(document, dict):
        return False
    providers = document.get("proxy-providers")
    if not isinstance(providers, dict):
        return False
    provider = providers.get("subscription")
    if not isinstance(provider, dict):
        return False
    if provider.get("type") != "file":
        return False
    return _is_managed_pool_path(provider.get("path"))


def _configured_sources_env(env_file: str) -> Optional[str]:
    """The env file that actually carries a ``SUBSCRIPTION_URL_n``, else ``None``.

    An existing valid repo ``config/subscription.env`` may be kept (optional
    compatibility); otherwise the installed ``env_file`` is used.  A missing or
    malformed candidate is never returned.
    """
    repo_sources = os.path.join(str(ROOT), "config", "subscription.env")
    for candidate in (repo_sources, env_file):
        values = _read_env_values(candidate)
        if values and any((values.get(key) or "").strip()
                          for key in _SUBSCRIPTION_KEYS):
            return candidate
    return None


def _source4_input_available() -> bool:
    """Whether a ``source4`` app profile is present, without reading any data.

    Reuses the app's own profile discovery only (no extraction, no decryption,
    no real user data); any failure just means "not available".
    """
    try:
        from node_provider.app_extract import AppExtractError, discover_shuttle_profile
    except Exception:  # noqa: BLE001 - a missing optional dep means "not available"
        return False
    try:
        discover_shuttle_profile()
    except AppExtractError:
        return False
    except Exception:  # noqa: BLE001 - discovery must never fail the tick
        return False
    return True


def _pool_collect_step(env_file: str):
    """The supplemental collect step, or ``None`` when there is nothing to collect.

    Runs only with readable credentials plus a configured subscription slot or
    an available ``source4`` profile.  The credentials ``env_file`` is always
    passed to collect; the sources file is passed only when it has slots.
    """
    if _read_env_values(env_file) is None:
        return None  # no readable credentials: collect would exit 2 anyway
    sources_env = _configured_sources_env(env_file)
    has_app = _source4_input_available()
    if sources_env is None and not has_app:
        return None
    argv = [
        sys.executable, "-m", "node_provider", "--collect",
        "--env-file", env_file,
    ]
    if sources_env is not None:
        argv.extend(["--sources-env", sources_env])
    if has_app:
        argv.extend(["--from-app", "source4"])
    return ("collect", argv)


def build_steps(*, config_path: Optional[str] = None,
                env_file: Optional[str] = None):
    """``[(label, argv), ...]``; the pool step exists only in pool mode.

    Subscription hosts keep the original three argv.  Pool hosts pass the same
    ``env_file`` to every step they run, use install-dir paths for the probe
    core and the provider output, and omit collect when there is nothing to
    collect.
    """
    if config_path is None:
        config_path = PRODUCTION_CONFIG
    if env_file is None:
        env_file = portable.default_env_file()

    if not pool_mode(config_path):
        return [
            ("collect", [
                sys.executable, "-m", "node_provider", "--collect",
                "--sources-env", str(ROOT / "config" / "subscription.env"),
                "--from-app", "source4",
            ]),
            ("ops", [sys.executable, "-m", "node_ops"]),
            ("provide", [
                sys.executable, "-m", "node_provider",
                "--output", PRODUCTION_PROVIDER,
            ]),
        ]

    steps = []
    collect = _pool_collect_step(env_file)
    if collect is not None:
        steps.append(collect)
    steps.extend([
        ("pool", [
            sys.executable, POOL_SCRIPT,
            "--env-file", env_file,
            "--output", POOL_FILE,
        ]),
        ("ops", [
            sys.executable, "-m", "node_ops",
            "--env-file", env_file,
            "--core", _install_path("mihomo.exe"),
        ]),
        ("provide", [
            sys.executable, "-m", "node_provider",
            "--output", PRODUCTION_PROVIDER,
            "--env-file", env_file,
        ]),
    ])
    return steps


def reload_pool_provider(*, env_file: str, urlopen=None) -> bool:
    """PUT the live controller's ``subscription`` provider so it re-reads the pool."""
    values = _read_env_values(env_file)
    if not values:
        return False
    secret = (values.get("SECRET") or "").strip()
    if not secret:
        return False

    request = urllib.request.Request(
        f"{LIVE_CONTROLLER}/providers/proxies/subscription",
        data=b"",
        headers={"Authorization": f"Bearer {secret}"},
        method="PUT",
    )
    opener = urlopen or urllib.request.urlopen
    try:
        response = opener(request, timeout=10)
        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
    except (OSError, urllib.error.URLError, ValueError, AttributeError):
        return False
    except Exception:  # noqa: BLE001 - any injected opener failure is "unavailable"
        return False
    return status in (200, 204)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refresh_pool(*, marker: str = POOL_MARKER, env_file: Optional[str] = None,
                 urlopen=None, log=print) -> bool:
    """Hot-reload the pool provider once its content changed; persist on success.

    The marker is only written after a successful reload, so a failed reload
    is retried on the next tick.
    """
    if not os.path.isfile(POOL_FILE):
        return False
    try:
        current = _sha256_file(POOL_FILE)
    except OSError:
        return False
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            previous = handle.read().strip()
    except OSError:
        previous = None
    if current == previous:
        return False

    if env_file is None:
        env_file = portable.default_env_file()
    if not reload_pool_provider(env_file=env_file, urlopen=urlopen):
        log("pool reload unavailable; marker not updated (retried next tick)")
        return False

    try:
        parent = os.path.dirname(os.path.abspath(marker))
        os.makedirs(parent, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(current + "\n")
    except OSError as exc:
        log(f"pool reloaded but marker write failed class={type(exc).__name__}")
        return True
    log("pool provider reloaded (refreshed without dropping connections)")
    return True


def _say(log, message: str) -> None:
    log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    log.flush()


def main() -> int:
    os.makedirs(os.path.dirname(os.path.abspath(LOG)), exist_ok=True)
    failed = []
    env_file = portable.default_env_file()
    with open(LOG, "a", encoding="utf-8") as log:
        steps = build_steps(env_file=env_file)
        if pool_mode(PRODUCTION_CONFIG) and not any(
                label == "collect" for label, _cmd in steps):
            _say(log, "SKIP collect: no subscription sources and no source4 "
                      "input on a Feishu-pool host")
        for label, cmd in steps:
            _say(log, f"START {label}")
            proc = subprocess.run(cmd, cwd=SCRIPTS, stdout=log,
                                  stderr=subprocess.STDOUT)
            _say(log, f"EXIT {proc.returncode} {label}")
            if proc.returncode != 0:
                # Every non-zero collect exit is real (3 covers write/API
                # faults too), so it is never masked as an empty tick.
                failed.append(label)
            elif label == "pool":
                try:
                    refresh_pool(env_file=env_file,
                                 log=lambda message: _say(log, message))
                except Exception as exc:  # noqa: BLE001 - a reload issue is not a step failure
                    _say(log, f"WARN pool refresh error class={type(exc).__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
