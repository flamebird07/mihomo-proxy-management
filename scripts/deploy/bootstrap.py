"""Orchestrate a Feishu-only bootstrap: credentials -> access -> supply -> pool -> install.

Every step is a function over injected dependencies, so the whole flow runs
offline with fakes.  Nothing here knows a platform path or how a platform's
installer is invoked: a :class:`Platform` supplies both.  The flow never
prints a credential: the caller's ``log`` receives lines that were produced
with values already masked, and the result object carries counts and key
names only.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from dataclasses import dataclass, field, fields
from typing import Mapping, Optional

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../scripts
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import feishu_pool  # noqa: E402
from node_provider import writer  # noqa: E402
from node_provider.feishu import FeishuClient, FeishuError  # noqa: E402

from . import feishu_url, registry, secrets_file, supply, verify  # noqa: E402
from . import fetch as _fetch  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_ACCESS, EXIT_SUPPLY, EXIT_POOL, EXIT_INSTALL = 0, 2, 3, 4, 5, 6
#: ``MPM_SUBSCRIPTION_URL`` / ``MPM_SUBSCRIPTION_URL_2..4`` -> the secret file's own keys.
SUBSCRIPTION_KEYS = {1: "SUBSCRIPTION_URL", 2: "SUBSCRIPTION_URL_2",
                     3: "SUBSCRIPTION_URL_3", 4: "SUBSCRIPTION_URL_4"}
_ENV_KEYS = tuple("MPM_SUBSCRIPTION_URL" + suffix
                  for suffix in ("", "_2", "_3", "_4"))


@dataclass(frozen=True)
class InstallRequest:
    staging_dir: str      # verified binaries (mihomo, geo data, ...)
    pool_path: str        # the Feishu pool provider written above
    start: bool           # False = install without starting the service


class Platform:
    """What the flow needs to know about one operating system."""

    name: str = ""            # "macos" | "windows"
    supply_os: str = ""       # key for supply.select_assets
    arch: str = ""            # "arm64" | "amd64"
    pool_path: str = ""
    secret_path: str = ""
    staging_dir: str = ""

    def install(self, request: InstallRequest) -> int:
        raise NotImplementedError


@dataclass
class BootstrapResult:
    exit_code: int = EXIT_OK
    problems: tuple = ()                 # "step:kind" strings, in failure order
    access: Optional[verify.AccessReport] = None
    pool_required: bool = False
    pool_nodes: int = 0
    pool_written: bool = False
    staged: dict = field(default_factory=dict)    # asset key -> staged path
    secret_keys: tuple = ()             # key names only
    secret_written: bool = False
    install_exit: Optional[int] = None
    dry_run: bool = False

    def ok(self) -> bool:
        return self.exit_code == EXIT_OK

    def summary(self) -> dict:
        """Every field in a JSON-friendly shape (the token never appears:
        ``asdict`` would carry it, so the access report is masked by hand)."""
        result = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "access":
                result[item.name] = _access_summary(value)
            else:
                result[item.name] = value
        return result


def _access_summary(access: Optional[verify.AccessReport]) -> Optional[dict]:
    if access is None:
        return None
    data = {item.name: getattr(access, item.name) for item in fields(access)}
    data.pop("app_token")  # the resolved real token never leaves this module
    return data


def subscription_urls_from_env(environ) -> dict:
    """``MPM_SUBSCRIPTION_URL`` / ``_2.._4`` from ``environ`` as the secret
    file's own keys, in 1..4 order.  The returned dict holds credentials:
    never log it."""
    source = environ if environ is not None else {}
    urls = {}
    for index, key in enumerate(_ENV_KEYS, start=1):
        value = str(source.get(key) or "").strip()
        if value:
            urls[SUBSCRIPTION_KEYS[index]] = value
    return urls


def secret_updates(app_token: str, credentials, subscription_urls: Mapping[str, str]) -> dict:
    """The KEY=VALUE payload for the secret file, in write order: the
    subscription URLs first, then the Feishu triple (with the resolved token,
    so the daemon needs no wiki resolution)."""
    updates = dict(subscription_urls)
    updates["FEISHU_APP_ID"] = credentials.app_id
    updates["FEISHU_APP_SECRET"] = credentials.app_secret
    updates["FEISHU_BITABLE_APP_TOKEN"] = app_token
    return updates


def _say(log, text: str) -> None:
    if log is not None:
        log(text)


def _fail(result: BootstrapResult, step: str, kind: str, exit_code: int) -> BootstrapResult:
    problem = f"{step}:{kind}"
    result.problems = result.problems + (problem,)
    result.exit_code = exit_code
    return result


def _kind_from(exc: BaseException) -> str:
    if isinstance(exc, (FeishuError, supply.SupplyError, secrets_file.SecretFileError)):
        return exc.kind
    return type(exc).__name__


def run_bootstrap(credentials, platform, *, lock=None, environ=None,
                  client_factory=FeishuClient, hostname=None,
                  project_version: str = "", fetch=None, fetch_release=None,
                  start: bool = True, dry_run: bool = False,
                  skip_supply: bool = False, skip_install: bool = False,
                  min_nodes: int = 1, now_ms: int = None,
                  log=None) -> BootstrapResult:
    """Run every step in order and stop at the first failure.  Failures come
    back as ``problems`` plus an exit code; nothing here raises to the caller."""
    result = BootstrapResult(dry_run=dry_run)

    def say(text: str) -> None:
        _say(log, text)

    # Step 1 - address.
    try:
        parsed = feishu_url.parse_bitable_url(credentials.feishu_url)
    except Exception as exc:  # noqa: BLE001
        return _fail(result, "url", getattr(exc, "kind", type(exc).__name__), EXIT_USAGE)

    # Step 2 - access.
    record = registry.DeploymentRecord(
        hostname or socket.gethostname(), platform.name, project_version,
        deployed_at_ms=now_ms if now_ms is not None else int(time.time() * 1000))
    try:
        access = verify.verify_access(client_factory, credentials, parsed, record,
                                      log=log, write_probe=not dry_run)
    except Exception as exc:  # noqa: BLE001 - verify_access is not supposed to raise
        return _fail(result, "access", _kind_from(exc), EXIT_ACCESS)
    result.access = access
    for hint in access.hints():
        say(f"bootstrap hint: {hint}")
    first_problem = access.problems[0] if access.problems else "failed"
    if not access.read_ok or (not dry_run and not access.write_ok):
        return _fail(result, "access", first_problem, EXIT_ACCESS)

    # Step 3 - subscriptions.
    urls = subscription_urls_from_env(environ)
    result.pool_required = not urls

    # Step 4 - supply.
    staged = {}
    if not skip_supply:
        if lock is None:
            lock = supply.load_lock()
        assets = supply.select_assets(lock, os_name=platform.supply_os,
                                      arch=platform.arch)
        if dry_run:
            say(f"bootstrap dry-run supply assets={len(assets)} "
                f"keys={','.join(asset.key for asset in assets)}")
        else:
            try:
                os.makedirs(platform.staging_dir, exist_ok=True)
                fetch = fetch or _fetch.fetch_to_file
                fetch_release = fetch_release or _fetch.fetch_json
                staged = supply.stage_assets(assets, platform.staging_dir,
                                             fetch=fetch, fetch_release=fetch_release,
                                             log=log)
            except Exception as exc:  # noqa: BLE001
                return _fail(result, "supply", _kind_from(exc), EXIT_SUPPLY)
    result.staged = staged
    if not skip_supply and not dry_run:
        say(f"bootstrap staged assets={len(staged)}")

    # Step 5 - node pool.
    pool_nodes = 0
    pool_written = False
    if not result.pool_required:
        say("subscription configured: the pool is not needed")
    else:
        try:
            if not access.can_source_pool:
                raise FeishuError("no_nodes_in_table")
            client = client_factory(credentials.app_id, credentials.app_secret,
                                    access.app_token)
            tables = client.list_tables()
            table_id = feishu_pool.find_nodes_table(tables)
            if not table_id:
                raise FeishuError("table_missing")
            rows = client.list_records(table_id)
            built = feishu_pool.build_pool(rows, now_ms=now_ms)
            pool_nodes = built.selected
            if pool_nodes < min_nodes:
                raise FeishuError("below_min_nodes")
            if dry_run:
                say(f"bootstrap dry-run pool nodes={pool_nodes}")
            else:
                os.makedirs(os.path.dirname(os.path.abspath(platform.pool_path)),
                            exist_ok=True)
                feishu_pool.write_pool(platform.pool_path,
                                       writer.render_provider(built.proxies))
                pool_written = True
                say(f"bootstrap pool nodes={pool_nodes}")
        except Exception as exc:  # noqa: BLE001
            return _fail(result, "pool", _kind_from(exc), EXIT_POOL)
    result.pool_nodes = pool_nodes
    result.pool_written = pool_written

    # Step 6 - secret file.
    updates = secret_updates(access.app_token, credentials, urls)
    result.secret_keys = tuple(updates)
    if not dry_run:
        try:
            try:
                with open(platform.secret_path, "r", encoding="utf-8") as stream:
                    existing = stream.read()
            except OSError:
                existing = ""
            os.makedirs(os.path.dirname(os.path.abspath(platform.secret_path)),
                        exist_ok=True)
            text = secrets_file.merge_env_text(existing, updates)
            os_name = "nt" if platform.name == "windows" else "posix"
            secrets_file.write_secret_file(platform.secret_path, text, os_name=os_name)
            result.secret_written = True
        except Exception as exc:  # noqa: BLE001
            return _fail(result, "secrets", _kind_from(exc), EXIT_INSTALL)
        say(f"bootstrap secrets written keys={len(result.secret_keys)}")

    # Step 7 - install.
    if not skip_install and not dry_run:
        request = InstallRequest(platform.staging_dir, platform.pool_path, start)
        try:
            code = platform.install(request)
        except Exception as exc:  # noqa: BLE001
            return _fail(result, "install", type(exc).__name__, EXIT_INSTALL)
        result.install_exit = code
        say(f"bootstrap install exit={code}")
        if code != 0:
            return _fail(result, "install", f"exit_{code}", EXIT_INSTALL)

    result.exit_code = EXIT_OK
    return result
