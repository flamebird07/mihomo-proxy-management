"""Output sanitisation - the single gate through which all text leaves mpm.

Nothing printed by this project may contain a subscription URL, a controller
secret, a node endpoint or a credential in a query string.  Three mechanisms
are combined:

1. **known-secret replacement**: any value registered with
   :meth:`Sanitizer.register` is swapped for ``[REDACTED]`` wherever it
   appears, byte for byte.
2. **pattern scrubbing**: URLs keep only ``scheme://host`` plus an irreversible
   short fingerprint; ``key=value`` pairs whose key looks secret are masked;
   non-loopback ``host:port`` endpoints are masked.
3. **digest preservation**: ``sha256:<64 hex>`` is non-secret verification
   material and is deliberately kept, so supply-chain failures stay debuggable.

The public entry points are :func:`sanitize` (process-wide registry) and
:class:`Sanitizer` for scoped use in tests.
"""

from __future__ import annotations

import hashlib
import re
import threading
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"
FINGERPRINT_LEN = 12

_SCHEME_URL = re.compile(r"\b(?:[A-Za-z][A-Za-z0-9+.-]{1,15}://)\S+")
_USERINFO = re.compile(r"//[^/\s]*@")
_SECRET_ASSIGN = re.compile(
    r"(?P<key>[\"']?(?:password|passwd|secret|token|api[-_]?key|apikey|auth|"
    r"authorization|bearer|subscription[-_]?url|cn[-_]?url|uuid|pbk|psk|sid)"
    r"[\"']?\s*[:=]\s*)(?P<val>[^\s,;\"']+)",
    re.IGNORECASE,
)
_IPV4_ENDPOINT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")
_HOSTNAME_ENDPOINT = re.compile(
    r"\b[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?(?:\.[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?)+:\d{1,5}\b",
    re.IGNORECASE,
)
# long opaque blobs look like key material and are never echoed.  ``/`` and
# ``.`` are deliberately excluded so filesystem paths (``etc/systemd/system/
# mihomo-proxy-management.service``) are not mistaken for base64 key material;
# real secrets are still caught by the registry and by the rules above.
_OPAQUE_BLOB = re.compile(r"(?<![A-Za-z\d+/])[A-Za-z\d+]{24,}={0,2}(?![A-Za-z\d/])")
# explicit non-secret: checksums used by the supply-chain verifier
_DIGEST = re.compile(r"(?:sha(?:256|512)|md5):[0-9a-fA-F]{8,}")

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]", "any"}


def fingerprint(value: str) -> str:
    """Irreversible short fingerprint used in place of any secret text."""
    return hashlib.sha256(("mpm-fp:" + value).encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]


def describe_url(url: str) -> str:
    """``scheme://host#fp<12>`` - the only allowed rendering of a URL."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    scheme = (parts.scheme or "").lower()
    if not scheme or not host:
        return f"invalid-url#fp{fingerprint(url)}"
    return f"{scheme}://{host}#fp{fingerprint(url)}"


class Sanitizer:
    """Registry of secret values plus scrubbing of unknown secrets."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()

    def register(self, *values: str | None) -> None:
        with self._lock:
            for value in values:
                if value and len(value) >= 3:
                    self._secrets.add(value)

    def forget_all(self) -> None:
        with self._lock:
            self._secrets = set()

    @property
    def registered_count(self) -> int:
        with self._lock:
            return len(self._secrets)

    def _mask_known(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def text(self, value: str) -> str:
        """Scrub ``value``, preserving ``sha256:<hex>`` verification material.

        Digests are split out and re-joined rather than guarded by an inline
        sentinel: a sentinel would itself look like key material to the blob
        rule and be destroyed.
        """
        if not value:
            return value
        # known secrets are masked across the whole value first, so a secret
        # that happens to contain a digest-looking run cannot survive the split
        value = self._mask_known(value)
        pieces: list[str] = []
        cursor = 0
        for match in _DIGEST.finditer(value):
            pieces.append(self._scrub(value[cursor:match.start()]))
            pieces.append(match.group(0))
            cursor = match.end()
        pieces.append(self._scrub(value[cursor:]))
        return "".join(pieces)

    def _scrub(self, value: str) -> str:
        if not value:
            return value
        value = _USERINFO.sub("//" + REDACTED + "@", value)
        value = _SCHEME_URL.sub(lambda m: describe_url(m.group(0)), value)
        value = _SECRET_ASSIGN.sub(lambda m: m.group("key") + REDACTED, value)
        value = self._mask_endpoints(value)
        value = _OPAQUE_BLOB.sub(REDACTED, value)
        return value

    def _mask_endpoints(self, value: str) -> str:
        def repl(match: re.Match[str]) -> str:
            whole = match.group(0)
            host = whole.rsplit(":", 1)[0]
            if host.lower() in _LOOPBACK:
                return whole
            return REDACTED

        value = _IPV4_ENDPOINT.sub(repl, value)
        value = _HOSTNAME_ENDPOINT.sub(repl, value)
        return value

    def lines(self, value: str) -> list[str]:
        return [self.text(line) for line in (value or "").splitlines()]

    def exception(self, exc: BaseException) -> str:
        """Exception text is never trusted; scrub type + message."""
        return self.text(f"{type(exc).__name__}: {exc}")


# Process-wide registry used by every lifecycle module.
HOST = Sanitizer()

# Scoped instance kept for tests that need isolation from HOST.
DEFAULT = Sanitizer()


def register(*values: str | None) -> None:
    HOST.register(*values)
    DEFAULT.register(*values)


def sanitize(value: str) -> str:
    """Scrub ``value`` against the process-wide registry."""
    return HOST.text(value)


__all__ = [
    "DEFAULT",
    "HOST",
    "REDACTED",
    "Sanitizer",
    "describe_url",
    "fingerprint",
    "register",
    "sanitize",
]
