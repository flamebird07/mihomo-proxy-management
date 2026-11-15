"""``networksetup`` proxy snapshot / apply / restore transaction.

Fallback to mixed-port mode means pointing every *enabled* network service at
``127.0.0.1:7890``.  That is a system-wide change, so it is handled as a
transaction:

1. enumerate services and snapshot Web / Secure Web / SOCKS settings plus the
   bypass domains, per service;
2. write the snapshot atomically at mode ``0600`` **before** any change;
3. apply, read back and verify;
4. restore item by item, read back and verify, and only then delete the
   snapshot.

An existing snapshot is never overwritten: it represents an unfinished
transaction that must be restored (or rolled forward) first.

An authenticated proxy cannot be restored through ``networksetup`` (it does
not expose the password), so the transaction fails closed before touching
anything rather than claiming a full restore it cannot deliver.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from mpm_common.atomicio import read_text_strict, write_atomic
from mpm_common.errors import FailClosed
from mpm_common.executor import Executor
from mpm_common.sanitize import HOST

SNAPSHOT_SCHEMA = 1
DEFAULT_SERVER = "127.0.0.1"
DEFAULT_PORT = 7890

STAGE_PREPARED = "prepared"
STAGE_APPLIED = "applied"


class NetworkProxyError(FailClosed):
    """The proxy transaction was refused or could not be completed."""


@dataclass
class ProxySetting:
    enabled: bool = False
    server: str = ""
    port: int = 0
    authenticated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "server": self.server,
            "port": self.port,
            "authenticated": self.authenticated,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ProxySetting":
        if not isinstance(payload, dict):
            return cls()
        try:
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        return cls(
            enabled=bool(payload.get("enabled")),
            server=str(payload.get("server") or ""),
            port=port,
            authenticated=bool(payload.get("authenticated")),
        )


@dataclass
class ServiceEntry:
    name: str
    disabled: bool = False


@dataclass
class ServiceSnapshot:
    name: str
    disabled: bool = False
    web: ProxySetting = field(default_factory=ProxySetting)
    secure: ProxySetting = field(default_factory=ProxySetting)
    socks: ProxySetting = field(default_factory=ProxySetting)
    bypass: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "disabled": self.disabled,
            "web": self.web.to_dict(),
            "secure": self.secure.to_dict(),
            "socks": self.socks.to_dict(),
            "bypass": list(self.bypass),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ServiceSnapshot":
        if not isinstance(payload, dict):
            raise NetworkProxyError("network proxy snapshot entry is not an object")
        bypass = payload.get("bypass") or []
        return cls(
            name=str(payload.get("name") or ""),
            disabled=bool(payload.get("disabled")),
            web=ProxySetting.from_dict(payload.get("web")),
            secure=ProxySetting.from_dict(payload.get("secure")),
            socks=ProxySetting.from_dict(payload.get("socks")),
            bypass=[str(item) for item in bypass] if isinstance(bypass, list) else [],
        )

    def authenticated(self) -> list[str]:
        return [
            label
            for label, setting in (("web", self.web), ("secure", self.secure), ("socks", self.socks))
            if setting.authenticated
        ]


@dataclass
class Snapshot:
    services: list[ServiceSnapshot] = field(default_factory=list)
    stage: str = STAGE_PREPARED
    created_at: str = ""
    schema: int = SNAPSHOT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "stage": self.stage,
            "created_at": self.created_at,
            "services": [service.to_dict() for service in self.services],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Snapshot":
        if not isinstance(payload, dict):
            raise NetworkProxyError("network proxy snapshot is not an object")
        services = payload.get("services") or []
        if not isinstance(services, list):
            raise NetworkProxyError("network proxy snapshot has no service list")
        return cls(
            services=[ServiceSnapshot.from_dict(item) for item in services],
            stage=str(payload.get("stage") or STAGE_PREPARED),
            created_at=str(payload.get("created_at") or ""),
            schema=int(payload.get("schema") or SNAPSHOT_SCHEMA),
        )

    def enabled_services(self) -> list[ServiceSnapshot]:
        return [service for service in self.services if not service.disabled]


# ---- pure parsers ---------------------------------------------------------


def _field(text: str, key: str) -> str:
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip().lower() == key:
            return value.strip()
    return ""


def parse_proxy(text: str) -> ProxySetting:
    """Parse ``networksetup -get{web,secureweb,socksfirewall}proxy`` output."""
    enabled = _field(text, "enabled").lower() in ("yes", "1", "true", "on")
    try:
        port = int(_field(text, "port") or 0)
    except ValueError:
        port = 0
    authenticated = _field(text, "authenticated proxy enabled").lower() in (
        "1",
        "yes",
        "true",
        "on",
    )
    return ProxySetting(
        enabled=enabled,
        server=_field(text, "server"),
        port=port,
        authenticated=authenticated,
    )


def parse_bypass(text: str) -> list[str]:
    """Parse ``networksetup -getproxybypassdomains`` output."""
    payload = (text or "").strip()
    if not payload:
        return []
    lowered = payload.lower()
    if "aren't any bypass domains" in lowered or "no bypass domains" in lowered:
        return []
    return [line.strip() for line in payload.splitlines() if line.strip()]


def parse_services(text: str) -> list[ServiceEntry]:
    """Parse ``networksetup -listallnetworkservices`` output.

    The first line is an explanatory header; disabled services are prefixed
    with ``*``.  Disabled services are recorded but never modified.
    """
    entries: list[ServiceEntry] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("an asterisk"):
            continue
        disabled = stripped.startswith("*")
        name = stripped[1:].strip() if disabled else stripped
        if name:
            entries.append(ServiceEntry(name=name, disabled=disabled))
    return entries


# ---- snapshot persistence -------------------------------------------------


def save_snapshot(path: str, snapshot: Snapshot, *, overwrite: bool = False) -> None:
    """Atomic ``0600`` write; refuses to clobber an unfinished transaction."""
    if os.path.exists(path) and not overwrite:
        raise NetworkProxyError(
            "a network proxy snapshot already exists; refusing to overwrite an "
            "unfinished transaction (restore it first)"
        )
    payload = json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) + "\n"
    write_atomic(path, payload, mode=0o600)


def load_snapshot(path: str) -> Snapshot:
    try:
        payload = json.loads(read_text_strict(path))
    except ValueError as exc:
        raise NetworkProxyError(f"network proxy snapshot is not valid JSON: {path}") from exc
    return Snapshot.from_dict(payload)


def snapshot_exists(path: str) -> bool:
    return os.path.isfile(path)


# ---- the adapter ----------------------------------------------------------


class NetworkProxy:
    """``networksetup`` adapter; commands are argv lists, never shell strings."""

    def __init__(
        self,
        executor: Executor,
        *,
        server: str = DEFAULT_SERVER,
        port: int = DEFAULT_PORT,
        command: str = "networksetup",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.executor = executor
        self.server = server
        self.port = port
        self.command = command
        self.clock = clock

    # ---- reads ---------------------------------------------------------
    def list_services(self) -> list[ServiceEntry]:
        result = self.executor.run([self.command, "-listallnetworkservices"])
        if not result.ok:
            raise NetworkProxyError(
                "cannot enumerate network services: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:160]
            )
        return parse_services(result.stdout)

    def _get(self, service: str, flag: str) -> str:
        result = self.executor.run([self.command, flag, service])
        if not result.ok:
            raise NetworkProxyError(
                f"networksetup {flag} failed: "
                + HOST.text((result.stderr or result.stdout or "").strip())[:160]
            )
        return result.stdout

    def read_service(self, entry: ServiceEntry) -> ServiceSnapshot:
        return ServiceSnapshot(
            name=entry.name,
            disabled=entry.disabled,
            web=parse_proxy(self._get(entry.name, "-getwebproxy")),
            secure=parse_proxy(self._get(entry.name, "-getsecurewebproxy")),
            socks=parse_proxy(self._get(entry.name, "-getsocksfirewallproxy")),
            bypass=parse_bypass(self._get(entry.name, "-getproxybypassdomains")),
        )

    def snapshot(self) -> Snapshot:
        """Snapshot every service before any change is made."""
        services = [self.read_service(entry) for entry in self.list_services()]
        return Snapshot(
            services=services,
            stage=STAGE_PREPARED,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
        )

    # ---- guards --------------------------------------------------------
    @staticmethod
    def _assert_restorable(snapshot: Snapshot) -> None:
        blocked = [
            f"{service.name}:{','.join(service.authenticated())}"
            for service in snapshot.enabled_services()
            if service.authenticated()
        ]
        if blocked:
            raise NetworkProxyError(
                "an existing proxy uses authentication; networksetup cannot read or "
                "restore its credentials, so automatic override/restore is refused "
                "for: " + ", ".join(blocked)
            )

    # ---- writes --------------------------------------------------------
    def _set(self, flag: str, service: str, *values: str) -> None:
        argv = [self.command, flag, service, *values]
        result = self.executor.run(argv)
        if not result.ok:
            raise NetworkProxyError(
                f"networksetup {flag} failed for '{service}': "
                + HOST.text((result.stderr or result.stdout or "").strip())[:160]
            )

    def _set_state(self, flag: str, service: str, enabled: bool) -> None:
        self._set(flag, service, "on" if enabled else "off")

    def apply(self, snapshot: Snapshot) -> list[str]:
        """Point every enabled service at this proxy, then verify by read-back."""
        self._assert_restorable(snapshot)
        modified: list[str] = []
        try:
            for service in snapshot.enabled_services():
                self._set("-setwebproxy", service.name, self.server, str(self.port))
                self._set("-setsecurewebproxy", service.name, self.server, str(self.port))
                self._set("-setsocksfirewallproxy", service.name, self.server, str(self.port))
                modified.append(service.name)
            self._verify_applied(snapshot)
        except NetworkProxyError:
            # never leave a half-applied state behind; a rollback failure must
            # not hide the original error (the caller keeps the snapshot)
            try:
                self.restore(snapshot)
            except NetworkProxyError:
                pass
            raise
        return modified

    def _verify_applied(self, snapshot: Snapshot) -> None:
        for service in snapshot.enabled_services():
            current = self.read_service(ServiceEntry(name=service.name))
            for label, setting in (
                ("web", current.web),
                ("secure", current.secure),
                ("socks", current.socks),
            ):
                if not setting.enabled or setting.server != self.server or setting.port != self.port:
                    raise NetworkProxyError(
                        f"read-back mismatch for {service.name} {label}: "
                        f"enabled={setting.enabled} server={setting.server or '-'} port={setting.port}"
                    )

    def restore(self, snapshot: Snapshot) -> list[str]:
        """Restore every item, read back, and report what was restored."""
        self._assert_restorable(snapshot)
        restored: list[str] = []
        for service in snapshot.enabled_services():
            for setting, set_flag, state_flag in (
                (service.web, "-setwebproxy", "-setwebproxystate"),
                (service.secure, "-setsecurewebproxy", "-setsecurewebproxystate"),
                (service.socks, "-setsocksfirewallproxy", "-setsocksfirewallproxystate"),
            ):
                if setting.enabled and setting.server:
                    self._set(set_flag, service.name, setting.server, str(setting.port))
                    self._set_state(state_flag, service.name, True)
                else:
                    self._set_state(state_flag, service.name, False)
            self._restore_bypass(service)
            restored.append(service.name)
        self._verify_restored(snapshot)
        return restored

    def _restore_bypass(self, service: ServiceSnapshot) -> None:
        if service.bypass:
            self._set("-setproxybypassdomains", service.name, *service.bypass)
        else:
            self._set("-setproxybypassdomains", service.name, "Empty")

    def _verify_restored(self, snapshot: Snapshot) -> None:
        for service in snapshot.enabled_services():
            current = self.read_service(ServiceEntry(name=service.name))
            for label, expected, actual in (
                ("web", service.web, current.web),
                ("secure", service.secure, current.secure),
                ("socks", service.socks, current.socks),
            ):
                if expected.enabled != actual.enabled:
                    raise NetworkProxyError(
                        f"restore verification failed for {service.name} {label}: "
                        f"enabled {expected.enabled} != {actual.enabled}"
                    )
                if expected.enabled and (
                    expected.server.lower() != actual.server.lower()
                    or expected.port != actual.port
                ):
                    raise NetworkProxyError(
                        f"restore verification failed for {service.name} {label}: "
                        f"server/port did not return to the saved values"
                    )
            if [item.lower() for item in service.bypass] != [
                item.lower() for item in current.bypass
            ]:
                raise NetworkProxyError(
                    f"restore verification failed for {service.name}: bypass domains differ"
                )


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_SERVER",
    "NetworkProxy",
    "NetworkProxyError",
    "ProxySetting",
    "STAGE_APPLIED",
    "STAGE_PREPARED",
    "ServiceEntry",
    "ServiceSnapshot",
    "Snapshot",
    "load_snapshot",
    "parse_bypass",
    "parse_proxy",
    "parse_services",
    "save_snapshot",
    "snapshot_exists",
]
