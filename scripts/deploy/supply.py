"""Digest-locked supply of the binaries a fresh machine needs (stdlib only).

Nothing here downloads by itself: fetching is injected, so verification and
extraction stay pure and testable.  The lock pins exact asset URLs with a
digest and a byte size; any mismatch aborts before a file is staged.  Geo data
is published under a rolling tag, so its lock digest is only a fallback - the
GitHub API's own asset digest is preferred when the API answers.
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from typing import Callable, List, Optional

#: The lock shipped next to this module; ``load_lock`` reads it by default.
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supply.lock.json")
#: Uncompressed cap for one archive member: a decompression bomb must not fill the disk.
MAX_MEMBER_BYTES = 400 * 1024 * 1024
FORMATS = ("raw", "gzip", "zip")
OS_NAMES = ("macos", "windows", "linux", "all")
ARCH_NAMES = ("arm64", "amd64", "all")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED = ("key", "os", "arch", "url", "digest", "size", "fmt", "install_as")
_ZIP_READ_ERRORS = (zipfile.BadZipFile, RuntimeError, NotImplementedError,
                    OSError, EOFError, zlib.error)


class SupplyError(Exception):
    """Raised instead of leaking download contents: a ``kind`` plus a bare detail."""

    def __init__(self, kind: str, detail: str = "") -> None:
        message = f"supply {kind}" + (f" ({detail})" if detail else "")
        super().__init__(message)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Asset:
    """One pinned download: what to fetch, how to check it, how to stage it."""

    key: str
    os: str            # "macos" | "windows" | "linux" | "all"
    arch: str          # "arm64" | "amd64" | "all"
    url: str
    digest: str        # 64 lowercase hex digits
    size: int
    fmt: str           # "raw" | "gzip" | "zip"
    install_as: str
    member: str = ""   # zip: exact member path, or a glob picking one file
    mode: str = "0644"
    rolling: bool = False
    api: str = ""      # release API URL listing the asset digest (rolling: required)
    authenticode_signer: str = ""   # checked by the platform layer; only carried

    @property
    def name(self) -> str:
        """The URL's last path segment (query/fragment stripped) - the name the
        GitHub API's asset list matches against."""
        path = self.url.split("#", 1)[0].split("?", 1)[0]
        return path.rstrip("/").rsplit("/", 1)[-1]


def _discard(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def load_lock(path: str = LOCK_PATH) -> dict:
    """Read and validate the lock file.  Unknown asset fields are ignored
    (forward compatibility); anything else that does not fit raises
    ``SupplyError("lock_invalid", <field name or key>)``.  The returned dict
    keeps the lock's other top-level keys as they are."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            lock = json.load(stream)
    except OSError:
        raise SupplyError("lock_missing") from None
    except ValueError:
        raise SupplyError("lock_invalid") from None
    if not isinstance(lock, dict):
        raise SupplyError("lock_invalid")
    version = lock.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise SupplyError("lock_invalid", "schema_version")
    entries = lock.get("assets")
    if not isinstance(entries, list) or not entries:
        raise SupplyError("lock_invalid", "assets")
    assets: List[Asset] = []
    seen = set()
    for index, entry in enumerate(entries):
        asset = _asset_from(entry, index)
        if asset.key in seen:
            raise SupplyError("lock_invalid", asset.key)
        seen.add(asset.key)
        assets.append(asset)
    result = dict(lock)
    result["schema_version"] = 1
    result["assets"] = assets
    return result


def _asset_from(entry, index: int) -> Asset:
    """Validate one lock entry and build the ``Asset``.  ``bool`` never counts
    as an ``int`` here - ``True`` is not a byte size."""
    if not isinstance(entry, dict):
        raise SupplyError("lock_invalid", f"assets[{index}]")

    def bad(field: str) -> SupplyError:
        return SupplyError("lock_invalid", field)

    for field in _REQUIRED:
        if field not in entry:
            raise bad(field)
    key = entry["key"]
    if not isinstance(key, str) or not key:
        raise bad("key")
    if not isinstance(entry["url"], str) or not entry["url"]:
        raise bad("url")
    if not isinstance(entry["install_as"], str) or not entry["install_as"]:
        raise bad("install_as")
    digest = entry["digest"]
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        raise bad("digest")
    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise bad("size")
    fmt = entry["fmt"]
    if not isinstance(fmt, str) or fmt not in FORMATS:
        raise bad("fmt")
    os_name = entry["os"]
    if not isinstance(os_name, str) or os_name not in OS_NAMES:
        raise bad("os")
    arch = entry["arch"]
    if not isinstance(arch, str) or arch not in ARCH_NAMES:
        raise bad("arch")
    member = entry.get("member", "")
    if not isinstance(member, str):
        raise bad("member")
    if fmt == "zip" and not member:
        raise bad("member")
    if fmt != "zip" and member:
        raise bad("member")
    mode = entry.get("mode", "0644")
    if not isinstance(mode, str):
        raise bad("mode")
    try:
        int(mode, 8)
    except ValueError:
        raise bad("mode") from None
    rolling = entry.get("rolling", False)
    if not isinstance(rolling, bool):
        raise bad("rolling")
    api = entry.get("api", "")
    if not isinstance(api, str):
        raise bad("api")
    if rolling and not api:
        raise bad("api")
    signer = entry.get("authenticode_signer", "")
    if not isinstance(signer, str):
        raise bad("authenticode_signer")
    return Asset(key=key, os=os_name, arch=arch, url=entry["url"], digest=digest,
                 size=size, fmt=fmt, install_as=entry["install_as"], member=member,
                 mode=mode, rolling=rolling, api=api, authenticode_signer=signer)


def select_assets(lock: dict, *, os_name: str, arch: str) -> list:
    """The assets that serve this platform, in lock-file order.  ``"all"``
    matches either axis."""
    return [asset for asset in lock["assets"]
            if asset.os in (os_name, "all") and asset.arch in (arch, "all")]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def asset_digest_from_release(release, asset: Asset) -> str:
    """Pull the ``sha256:`` digest the GitHub API lists for ``asset``.  Empty
    means "not available" - callers fall back to the lock's digest."""
    if not isinstance(release, dict):
        return ""
    entries = release.get("assets")
    if not isinstance(entries, list):
        return ""
    prefix = "sha256:"
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == asset.name:
            digest = entry.get("digest")
            if not isinstance(digest, str) or not digest.lower().startswith(prefix):
                return ""
            value = digest[len(prefix):].lower()
            return value if _HEX64.fullmatch(value) else ""
    return ""


def verify_payload(data: bytes, asset: Asset, api_digest: str = "") -> None:
    """Byte size first, then digest.  A rolling asset trusts a well-formed API
    digest over the lock's (stale by design) fallback digest; a non-rolling
    asset only ever accepts its own pinned digest."""
    if len(data) != asset.size:
        raise SupplyError("size_mismatch", asset.key)
    expected = asset.digest
    if asset.rolling and isinstance(api_digest, str) and _HEX64.fullmatch(api_digest):
        expected = api_digest
    if sha256_hex(data) != expected:
        raise SupplyError("digest_mismatch", asset.key)


def member_url(url: str, mirror: str = "") -> str:
    """Prefix GitHub URLs with the mirror; anything else is returned as-is.
    A mirror only changes where bytes come from - never what gets verified."""
    prefix = "https://github.com/"
    if mirror and url.startswith(prefix):
        return mirror.rstrip("/") + "/" + url[len(prefix):]
    return url


def extract_member(data: bytes, asset: Asset) -> bytes:
    """Return the payload bytes, in memory only.  Archive members are never
    written to an archive-derived path - extraction reads bytes out of the
    archive, so an entry's name cannot point outside the staging directory
    (no zip-slip by construction)."""
    if asset.fmt == "raw":
        return data
    if asset.fmt == "gzip":
        try:
            member = gzip.decompress(data)
        except (OSError, EOFError, zlib.error):
            raise SupplyError("archive_invalid", asset.key) from None
    else:
        member = _zip_member(data, asset)
    if len(member) > MAX_MEMBER_BYTES:
        raise SupplyError("archive_too_large", asset.key)
    return member


def _zip_member(data: bytes, asset: Asset) -> bytes:
    """The one member ``asset.member`` names - exactly, or by glob when the
    glob matches exactly one non-directory entry."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            candidates = [name for name in archive.namelist()
                          if not name.endswith("/")
                          and (name == asset.member
                               or fnmatch.fnmatchcase(name, asset.member))]
            if not candidates:
                raise SupplyError("member_missing", asset.key)
            if len(candidates) > 1:
                raise SupplyError("member_ambiguous", asset.key)
            try:
                return archive.read(candidates[0])
            except _ZIP_READ_ERRORS:
                raise SupplyError("archive_invalid", asset.key) from None
    except zipfile.BadZipFile:
        raise SupplyError("archive_invalid", asset.key) from None


def stage_asset(asset: Asset, data: bytes, staging_dir: str, *,
                api_digest: str = "") -> str:
    """Verify, extract, and atomically place the member at
    ``staging_dir/install_as``.  The staging directory must already exist; a
    failed or mismatched asset leaves nothing behind - not even a temp file."""
    verify_payload(data, asset, api_digest)
    member = extract_member(data, asset)
    target = os.path.join(staging_dir, asset.install_as)
    temporary = ""
    try:
        handle, temporary = tempfile.mkstemp(dir=staging_dir, prefix=".tmp-")
        with os.fdopen(handle, "wb") as stream:
            stream.write(member)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, int(asset.mode, 8))
        os.replace(temporary, target)
    except OSError as exc:
        if temporary:
            _discard(temporary)
        raise SupplyError("stage_failed", type(exc).__name__) from None
    return target


def stage_assets(assets, staging_dir: str, *, fetch,
                 fetch_release: Optional[Callable[[str], dict]] = None,
                 log: Optional[Callable[[str], None]] = None) -> dict:
    """Fetch, verify and stage every asset, or leave nothing behind.

    The mirror prefix is read from ``MPM_GITHUB_MIRROR`` here - the only
    environment variable this module touches.  Any failure deletes the files
    staged so far and re-raises the original ``SupplyError``: a caller never
    inherits half a set of binaries.
    """
    say = log or (lambda _message: None)
    mirror = os.environ.get("MPM_GITHUB_MIRROR", "")
    staged = {}
    try:
        for asset in assets:
            staged[asset.key] = _stage_one(asset, staging_dir, mirror, fetch=fetch,
                                           fetch_release=fetch_release, say=say)
    except BaseException:
        for path in staged.values():
            _discard(path)
        raise
    return staged


def _stage_one(asset: Asset, staging_dir: str, mirror: str, *, fetch,
               fetch_release, say) -> str:
    """Download, read back, discard the raw download, then verify and stage."""
    destination = os.path.join(staging_dir, asset.key + ".download")
    try:
        fetch(member_url(asset.url, mirror), destination)
    except Exception as exc:  # noqa: BLE001 - the fetcher's text never leaks
        raise SupplyError("download_failed", type(exc).__name__) from None
    try:
        with open(destination, "rb") as stream:
            data = stream.read()
    except OSError as exc:
        raise SupplyError("download_failed", type(exc).__name__) from None
    _discard(destination)  # the raw download never outlives this call
    api_digest = ""
    if asset.rolling and fetch_release is not None:
        try:
            release = fetch_release(asset.api)
        except Exception:  # noqa: BLE001 - an unreachable API falls back to the lock
            release = None
        if release is None:
            say(f"supply api digest unavailable key={asset.key} fallback=lock")
        else:
            api_digest = asset_digest_from_release(release, asset)
            if api_digest and api_digest != asset.digest:
                say(f"supply lock digest stale key={asset.key} verified=api")
    target = stage_asset(asset, data, staging_dir, api_digest=api_digest)
    say(f"supply staged key={asset.key} bytes={len(extract_member(data, asset))}")
    return target
