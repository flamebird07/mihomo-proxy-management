"""Supply-chain verification and installation (section 8, D9b=A).

Everything is digest-pinned in ``linux/supply/mihomo.lock.json``.  The order is
fixed and every step can abort the install:

1. resolve one asset by *exact* name (0 or >1 match -> fail)
2. download into a ``0700`` staging directory
3. require a SHA-256 digest and require it to match exactly
4. check the gzip magic
5. decompress to a single file, rejecting traversal / multi-member archives
6. check the ELF magic and ``e_machine`` against the target architecture
7. only then move into ``/usr/libexec/<version>/``
8. switch the ``current`` symlink atomically
9. any failure means nothing is installed and the service is not started
10. an unavailable metadata API never degrades into an unverified download

Tests inject ``fetch``/``probe`` so no system binary is ever downloaded or
executed.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import io
import json
import os
import tarfile
from dataclasses import dataclass
from typing import Any, Callable
import urllib.error
import urllib.request

from .atomicio import ensure_dir, make_symlink_atomic, secure_tempdir, write_atomic
from .errors import FailClosed
from .sanitize import HOST

ELF_MAGIC = b"\x7fELF"
GZIP_MAGIC = b"\x1f\x8b"
# ELF e_machine values
EM_X86_64 = 0x3E
EM_AARCH64 = 0xB7

# preflight.asset_suffix -> lock asset key
ASSET_KEY_BY_SUFFIX = {
    "amd64-v3": "linux-amd64-v3",
    "amd64-compatible": "linux-amd64-compatible",
    "arm64": "linux-arm64",
}
# lock asset key / suffix -> required ELF e_machine
ELM_BY_TARGET = {
    "amd64-v3": EM_X86_64,
    "amd64-compatible": EM_X86_64,
    "linux-amd64-v3": EM_X86_64,
    "linux-amd64-compatible": EM_X86_64,
    "arm64": EM_AARCH64,
    "linux-arm64": EM_AARCH64,
}

Fetcher = Callable[[str, str], None]  # (url, destination path)


@dataclass(frozen=True)
class Asset:
    key: str
    name: str
    url: str
    sha256: str
    size: int
    source: str
    kind: str  # "binary" | "geo"
    install_as: str = ""

    def expected_digest(self) -> str:
        return self.sha256.lower()


@dataclass
class Lock:
    tag: str
    geo_tag: str
    assets: dict[str, Asset]
    path: str

    def asset(self, key: str) -> Asset:
        if key not in self.assets:
            raise FailClosed(f"lock file has no asset entry for '{key}'")
        return self.assets[key]

    def asset_by_name(self, name: str) -> Asset:
        matches = [asset for asset in self.assets.values() if asset.name == name]
        if len(matches) != 1:
            raise FailClosed(f"lock file does not pin exactly one asset named '{name}'")
        return matches[0]

    def binary_for(self, asset_suffix: str) -> Asset:
        key = ASSET_KEY_BY_SUFFIX.get(asset_suffix)
        if key is None:
            raise FailClosed(f"no pinned asset for architecture suffix '{asset_suffix}'")
        return self.asset(key)

    def summary(self) -> dict[str, object]:
        return {
            "mihomo_tag": self.tag,
            "geo_tag": self.geo_tag,
            "assets": sorted(
                (
                    {
                        "key": asset.key,
                        "name": asset.name,
                        "sha256": asset.sha256,
                        "size": asset.size,
                        "kind": asset.kind,
                    }
                    for asset in self.assets.values()
                ),
                key=lambda item: item["key"],
            ),
        }


def load_lock(path: str) -> Lock:
    """Parse and structurally validate the audited lock file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.loads(handle.read())
    except FileNotFoundError as exc:
        raise FailClosed(f"lock file missing: {path}") from exc
    except ValueError as exc:
        raise FailClosed(f"lock file is not valid JSON: {path}") from exc
    tag = str(raw.get("mihomo_tag") or "")
    geo_tag = str(raw.get("geo_tag") or "")
    if not tag or not geo_tag:
        raise FailClosed("lock file must pin both mihomo_tag and geo_tag")
    entries = raw.get("assets")
    if not isinstance(entries, list) or not entries:
        raise FailClosed("lock file must list at least one asset")
    assets: dict[str, Asset] = {}
    for entry in entries:
        key = str(entry.get("key") or "")
        name = str(entry.get("name") or "")
        url = str(entry.get("url") or "")
        digest = str(entry.get("sha256") or "")
        source = str(entry.get("source") or "")
        kind = str(entry.get("kind") or "")
        install_as = str(entry.get("install_as") or "")
        size = entry.get("size")
        if not key or not name or not url:
            raise FailClosed(f"lock asset '{key or '?'}' is incomplete")
        if kind not in {"binary", "geo"}:
            raise FailClosed(f"lock asset '{key}' has unknown kind")
        if kind == "geo" and not re.fullmatch(r"[A-Za-z0-9_.-]+", install_as):
            raise FailClosed(f"lock geo asset '{key}' must pin a safe install_as filename")
        if not url.startswith("https://github.com/"):
            raise FailClosed(f"lock asset '{key}' is not sourced from the official GitHub host")
        if not _valid_digest(digest):
            raise FailClosed(f"lock asset '{key}' lacks a full sha256 digest")
        if not isinstance(size, int) or size <= 0:
            raise FailClosed(f"lock asset '{key}' lacks a positive pinned size")
        if key in assets:
            raise FailClosed(f"lock asset key '{key}' is duplicated (ambiguous)")
        assets[key] = Asset(key, name, url, digest.lower(), size, source, kind, install_as)
    for required in ("linux-amd64-v3", "linux-amd64-compatible", "linux-arm64", "geoip", "geosite"):
        if required not in assets:
            raise FailClosed(f"lock file is missing the required asset '{required}'")
    return Lock(tag=tag, geo_tag=geo_tag, assets=assets, path=path)


def _valid_digest(digest: str) -> bool:
    value = digest.removeprefix("sha256:")
    return len(value) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_digest(value: str) -> str:
    """Accept ``sha256:<hex>`` or bare hex; reject anything ambiguous."""
    text = (value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise FailClosed("asset digest is absent or not a full sha256 hex digest")
    return text


def select_asset_by_name(assets: list[dict[str, object]], name: str) -> dict[str, object]:
    """Exact-name selection from API metadata; 0 or >1 matches fail closed."""
    matches = [item for item in assets if str(item.get("name")) == name]
    if not matches:
        raise FailClosed(f"release metadata contains no asset named '{name}'")
    if len(matches) > 1:
        raise FailClosed(f"release metadata is ambiguous: {len(matches)} assets named '{name}'")
    return matches[0]


def digest_from_metadata(asset: dict[str, object]) -> str:
    digest = str(asset.get("digest") or "")
    if not digest:
        raise FailClosed("release metadata carries no digest; refusing an unverified download")
    return normalise_digest(digest)


# ---- verification -----------------------------------------------------------


def check_gzip_magic(path: str) -> None:
    with open(path, "rb") as handle:
        magic = handle.read(2)
    if magic != GZIP_MAGIC:
        raise FailClosed(f"not a gzip stream (magic {magic[:2]!r})")


def gunzip_single_file(path: str, dest: str) -> None:
    """Decompress to exactly one file, rejecting tar members / traversal."""
    check_gzip_magic(path)
    with gzip.open(path, "rb") as handle:
        payload = handle.read()
    if not payload:
        raise FailClosed("decompressed asset is empty")
    if payload[:264].find(b"ustar") != -1:
        _extract_single_tar_member(payload, dest)
        return
    write_atomic(dest, payload, mode=0o755, mkdir=True)


def _extract_single_tar_member(payload: bytes, dest: str) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        members = [m for m in archive.getmembers() if m.isfile()]
        if len(members) != 1:
            raise FailClosed(f"archive must contain exactly one file (got {len(members)} members)")
        member = members[0]
        name = member.name
        if name.startswith("/") or ".." in name.split("/") or os.sep in name.strip("/"):
            raise FailClosed("archive member name is not a safe single path element")
        handle = archive.extractfile(member)
        if handle is None:  # pragma: no cover - guarded by isfile()
            raise FailClosed("archive member is unreadable")
        write_atomic(dest, handle.read(), mode=0o755, mkdir=True)


def check_elf(path: str, *, expect: str) -> None:
    """Verify ELF magic and ``e_machine`` against the target architecture."""
    wanted = ELM_BY_TARGET.get(expect)
    if wanted is None:
        raise FailClosed(f"unknown architecture target '{expect}'")
    with open(path, "rb") as handle:
        data = handle.read(20)
    if len(data) < 20:
        raise FailClosed("ELF header truncated")
    if data[:4] != ELF_MAGIC:
        raise FailClosed("downloaded binary is not an ELF executable")
    little_endian = data[5] == 1
    machine = int.from_bytes(data[18:20], "little" if little_endian else "big")
    if machine != wanted:
        raise FailClosed(
            "ELF architecture mismatch: binary e_machine=0x%x, host expects 0x%x"
            % (machine, wanted)
        )


# ---- install -----------------------------------------------------------------


@dataclass
class InstallOutcome:
    version: str
    binary: str
    asset: str
    sha256: str
    geo: list[str]


def install_verified(
    *,
    layout,
    lock: Lock,
    asset_suffix: str,
    fetch: Fetcher,
    exec_ok: bool = True,
    staging_parent: str | None = None,
) -> InstallOutcome:
    """Download, verify and atomically switch to a pinned binary + geo data.

    ``exec_ok`` is a test seam for "we never execute the downloaded binary";
    production always passes True and the code path simply does not exec it.
    """
    if not exec_ok:
        raise FailClosed("install aborted before execution of downloaded material")
    binary_asset = lock.binary_for(asset_suffix)
    geo_assets = [lock.asset("geoip"), lock.asset("geosite")]
    staging_root = staging_parent or layout.staging_dir
    ensure_dir(staging_root, 0o700)
    version = lock.tag

    with secure_tempdir(staging_root, "dl") as staging:
        raw = os.path.join(staging, binary_asset.name)
        fetch(binary_asset.url, raw)
        actual = sha256_file(raw)
        expected = normalise_digest(binary_asset.sha256)
        if actual != expected:
            raise FailClosed(
                "sha256 mismatch for pinned asset %s: expected sha256:%s got sha256:%s"
                % (binary_asset.name, expected, actual)
            )
        stat_size = os.path.getsize(raw)
        if stat_size != binary_asset.size:
            raise FailClosed(
                "size mismatch for pinned asset %s: expected %d got %d"
                % (binary_asset.name, binary_asset.size, stat_size)
            )
        extracted = os.path.join(staging, "mihomo")
        gunzip_single_file(raw, extracted)
        check_elf(extracted, expect=asset_suffix)

        geo_staged: list[tuple[Asset, str]] = []
        for asset in geo_assets:
            dest = os.path.join(staging, asset.name)
            fetch(asset.url, dest)
            got = sha256_file(dest)
            want = normalise_digest(asset.sha256)
            if got != want:
                raise FailClosed(
                    "sha256 mismatch for pinned geo asset %s: expected sha256:%s got sha256:%s"
                    % (asset.name, want, got)
                )
            if os.path.getsize(dest) != asset.size:
                raise FailClosed(f"size mismatch for pinned geo asset {asset.name}")
            geo_staged.append((asset, dest))

        version_dir = layout.libexec_version_dir(version)
        ensure_dir(version_dir, 0o755)
        final_binary = layout.libexec_binary(version)
        with open(extracted, "rb") as handle:
            write_atomic(final_binary, handle.read(), mode=0o755, mkdir=False)
        for asset, staged in geo_staged:
            target = os.path.join(layout.var_lib, asset.install_as)
            with open(staged, "rb") as handle:
                write_atomic(target, handle.read(), mode=0o600, mkdir=True)
        make_symlink_atomic(version_dir, layout.libexec_current)
        return InstallOutcome(
            version=version,
            binary=final_binary,
            asset=binary_asset.name,
            sha256=expected,
            geo=[asset.install_as for asset, _ in geo_staged],
        )


def probe_release_metadata(url: str, *, timeout: float = 20.0) -> dict[str, object]:
    """Read-only official GitHub API metadata query; no credentials, no fallback.

    A failure here is fatal for install (spec: an unavailable API must never
    degrade into an unverified download).
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "mihomo-proxy-management-supply", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise FailClosed(f"release metadata unavailable: {HOST.text(str(exc))}") from exc


def audit_lock_against_metadata(lock: Lock, assets: list[dict[str, object]]) -> None:
    """Cross-check the committed lock against live official metadata.

    Only *compares*; it never installs.  Any digest ambiguity fails closed.
    """
    for asset in lock.assets.values():
        if asset.kind != "binary":
            continue
        entry = select_asset_by_name(assets, asset.name)
        live = digest_from_metadata(entry)
        if live != normalise_digest(asset.sha256):
            raise FailClosed(
                "lock digest disagrees with official metadata for %s (lock sha256:%s, live sha256:%s)"
                % (asset.name, normalise_digest(asset.sha256), live)
            )


__all__ = [
    "Asset",
    "ELF_MAGIC",
    "GZIP_MAGIC",
    "InstallOutcome",
    "Lock",
    "audit_lock_against_metadata",
    "check_elf",
    "check_gzip_magic",
    "digest_from_metadata",
    "gunzip_single_file",
    "install_verified",
    "load_lock",
    "normalise_digest",
    "probe_release_metadata",
    "select_asset_by_name",
    "sha256_bytes",
    "sha256_file",
]
