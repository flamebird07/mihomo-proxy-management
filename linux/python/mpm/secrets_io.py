"""Secret input resolution (D7=A) - the only place secrets are read.

Two sources are allowed, and no others:

1. a local secret file with strict permissions (default
   ``/etc/mihomo-proxy-management/subscription.env``, overridable by *path*
   only - the CLI never accepts a URL or secret on the command line)
2. the environment of the ``install`` / ``configure`` process itself

If the same key is provided by both sources with different values we fail
closed rather than guess.  Environment values are never persisted anywhere
except the rendered ``config.yaml`` (0600), which mihomo must be able to read.

Recognised keys::

    MPM_SUBSCRIPTION_URL        foreign subscription   (required)
    MPM_CN_SUBSCRIPTION_URL     CN subscription        (optional)
    MPM_CONTROLLER_SECRET       controller secret      (optional, generated)
    MPM_CN_HEALTHCHECK_URL      CN health-check URL    (required with CN)
    MPM_SUBSCRIPTION_INTERVAL   refresh interval, min  (optional, default 60)
"""

from __future__ import annotations

import os
import re
import secrets as _secrets
from dataclasses import dataclass, field

from .atomicio import check_secret_input, read_text_strict, write_atomic
from .errors import FailClosed
from .sanitize import HOST, describe_url, fingerprint

ENV_FOREIGN = "MPM_SUBSCRIPTION_URL"
ENV_CN = "MPM_CN_SUBSCRIPTION_URL"
ENV_SECRET = "MPM_CONTROLLER_SECRET"
ENV_CN_HEALTH = "MPM_CN_HEALTHCHECK_URL"
ENV_INTERVAL = "MPM_SUBSCRIPTION_INTERVAL"

ENV_KEYS = (ENV_FOREIGN, ENV_CN, ENV_SECRET, ENV_CN_HEALTH, ENV_INTERVAL)

DEFAULT_INTERVAL = "60"
_INTERVAL_RE = re.compile(r"^\d{1,6}$")

# keys that must never be empty after resolution
_REQUIRED = (ENV_FOREIGN,)


@dataclass
class SecretInputs:
    """Resolved inputs plus the *origin* of each value (never the value)."""

    foreign_url: str = ""
    cn_url: str = ""
    controller_secret: str = ""
    cn_healthcheck_url: str = ""
    interval: str = DEFAULT_INTERVAL
    origins: dict[str, str] = field(default_factory=dict)
    generated_secret: bool = False

    def as_safe_dict(self) -> dict[str, object]:
        """JSON-safe view: descriptions only, no secret material."""
        view: dict[str, object] = {
            "foreign": describe_url(self.foreign_url) if self.foreign_url else "absent",
            "foreign_sha256_12": fingerprint(self.foreign_url) if self.foreign_url else "",
            "cn": describe_url(self.cn_url) if self.cn_url else "absent",
            "cn_sha256_12": fingerprint(self.cn_url) if self.cn_url else "",
            "controller_secret": "present" if self.controller_secret else "absent",
            "controller_secret_source": "generated" if self.generated_secret else "provided",
            "cn_healthcheck": (
                describe_url(self.cn_healthcheck_url) if self.cn_healthcheck_url else "absent"
            ),
            "interval_minutes": self.interval,
            "origins": dict(sorted(self.origins.items())),
        }
        return view


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ignores blanks/comments and surrounding quotes."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _validate_url(key: str, value: str) -> None:
    if not value:
        return
    lowered = value.lower()
    if not lowered.startswith(("http://", "https://")):
        raise FailClosed(f"{key} must be an http(s) URL (got scheme {scheme_of(lowered)})")
    if "\n" in value or "\r" in value:
        raise FailClosed(f"{key} contains a newline")


def scheme_of(value: str) -> str:
    """Scheme prefix only - safe to print, never the rest of the URL."""
    idx = value.find("://")
    return value[:idx] if idx > 0 else "none"


def _merge(key: str, file_value: str | None, env_value: str | None, origins: dict[str, str]) -> str:
    """Combine two sources, failing closed on ambiguity."""
    if file_value is not None and env_value is not None:
        if file_value != env_value:
            raise FailClosed(
                f"{key} is set in both the secret file and the process environment "
                f"with different values (file fp={fingerprint(file_value)}, "
                f"env fp={fingerprint(env_value)}); remove one source"
            )
        origins[key] = "file+env"
        return file_value
    if env_value is not None:
        origins[key] = "env"
        return env_value
    if file_value is not None:
        origins[key] = "file"
        return file_value
    origins[key] = "absent"
    return ""


def load_secret_file(path: str, *, system: bool = True) -> dict[str, str]:
    """Read and validate a secret input file (never follows symlinks)."""
    check_secret_input(path, require_root_owned=system)
    return parse_env_file(read_text_strict(path))


def generate_controller_secret(length: int = 40) -> str:
    return _secrets.token_urlsafe(length)[:length]


def resolve(
    layout,
    *,
    env_file: str | None = None,
    environ: dict[str, str] | None = None,
    system: bool = True,
) -> SecretInputs:
    """Resolve inputs from file + environment, then validate and persist.

    ``system=False`` relaxes the *ownership* requirement only (still rejects
    symlinks, non-regular files and any group/other permission bit).  That is
    what lets the test-suite exercise the same code unprivileged.  Production
    always uses ``system=True``.

    Never raises with any secret material in the message text.
    """
    env = dict(os.environ if environ is None else environ)
    path = env_file or layout.subscription_env
    file_map: dict[str, str] = {}
    if os.path.exists(path):
        file_map = load_secret_file(path, system=system)

    origins: dict[str, str] = {}
    inputs = SecretInputs(origins=origins)

    inputs.foreign_url = _merge(ENV_FOREIGN, file_map.get(ENV_FOREIGN), env.get(ENV_FOREIGN), origins)
    inputs.cn_url = _merge(ENV_CN, file_map.get(ENV_CN), env.get(ENV_CN), origins)
    inputs.controller_secret = _merge(
        ENV_SECRET, file_map.get(ENV_SECRET), env.get(ENV_SECRET), origins
    )
    inputs.cn_healthcheck_url = _merge(
        ENV_CN_HEALTH, file_map.get(ENV_CN_HEALTH), env.get(ENV_CN_HEALTH), origins
    )
    inputs.interval = _merge(ENV_INTERVAL, file_map.get(ENV_INTERVAL), env.get(ENV_INTERVAL), origins) or DEFAULT_INTERVAL

    if not inputs.foreign_url:
        raise FailClosed(
            f"{ENV_FOREIGN} is required: put it in the 0600 secret file "
            f"{layout.subscription_env} or export it in this process only"
        )
    if not _INTERVAL_RE.match(inputs.interval):
        raise FailClosed(f"{ENV_INTERVAL} must be digits only (minutes)")

    _validate_url(ENV_FOREIGN, inputs.foreign_url)
    _validate_url(ENV_CN, inputs.cn_url)
    _validate_url(ENV_CN_HEALTH, inputs.cn_healthcheck_url)

    if inputs.cn_url and not inputs.cn_healthcheck_url:
        raise FailClosed(
            f"{ENV_CN_HEALTH} is required whenever a CN subscription is configured "
            f"(D8b=2: CN health must be explicit, never implied)"
        )

    existing = _read_existing_secret(layout.controller_secret_file, system=system)
    if not inputs.controller_secret:
        if existing:
            inputs.controller_secret = existing
            origins[ENV_SECRET] = "stored"
        else:
            inputs.controller_secret = generate_controller_secret()
            inputs.generated_secret = True
            origins[ENV_SECRET] = "generated"
    # The CLI must be able to authenticate later (status/test/update), so the
    # resolved secret is always mirrored into its own 0600 file.  mihomo itself
    # reads the secret from the rendered 0600 config.
    if inputs.controller_secret != existing:
        _store_controller_secret(layout, inputs.controller_secret)

    # register everything with the sanitizer before any other code can print
    HOST.register(
        inputs.foreign_url,
        inputs.cn_url,
        inputs.controller_secret,
        inputs.cn_healthcheck_url,
    )
    return inputs


def _read_existing_secret(path: str, *, system: bool = True) -> str:
    if not os.path.isfile(path) or os.path.islink(path):
        return ""
    check_secret_input(path, require_root_owned=system)
    return read_text_strict(path).strip()


def _store_controller_secret(layout, value: str) -> None:
    write_atomic(layout.controller_secret_file, value, mode=0o600)


__all__ = [
    "ENV_CN",
    "ENV_CN_HEALTH",
    "ENV_FOREIGN",
    "ENV_INTERVAL",
    "ENV_KEYS",
    "ENV_SECRET",
    "SecretInputs",
    "describe_url",
    "generate_controller_secret",
    "load_secret_file",
    "parse_env_file",
    "resolve",
]
