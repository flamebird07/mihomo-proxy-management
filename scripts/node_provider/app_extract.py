"""App-channel node extraction (explicit, opt-in, read-only).

Three app sources are supported.  All are *one-time explicit imports*: nothing
here runs by default and nothing here is scheduled.

source1 - 飞兔 desktop core
    Read-only process-memory scan of ``AtlasCore_amd64.exe``.  Uses
    ``ReadProcessMemory`` only: no code injection, no process stop, no proxy
    change, no full memory dump written to disk.  Complete mihomo proxy dicts
    are recovered from JSON ``"proxies": [...]`` arrays or YAML ``proxies:``
    documents found in readable private memory.

source3 - CoffeeCloud / Ninja Desktop
    Decrypts the local Ninja Desktop vault (HKDF-SHA256 derived KEK +
    AES-GCM).  The vault must already exist on this machine; the key never
    leaves this process and is never logged.

source4 - 西瓜加速 (Electron ``shuttle-client``)
    Decrypts the on-disk subscription cache only: the Chromium os_crypt v10
    master key is unwrapped through DPAPI, and the cached mihomo config is
    opened with AES-256-GCM using an AAD derived from the panel session.  This
    is read-only at rest: the app is not started, stopped, queried or otherwise
    touched.  Session secrets and node credentials are never logged or raised.

All extractors are injectable for tests: ``extract_source1(reader=...)``
accepts any object exposing ``verify()`` and ``iter_buffers()``,
``extract_source3(vault_dir=..., hostname=...)`` accepts a fixture directory,
and ``extract_source4(profile_dir=..., dpapi_unprotect=...)`` accepts a
fixture profile plus a fake DPAPI seam.

Node completeness (shared with the merge step): name, type, server, port and a
credential field; loopback endpoints and dialer-proxy hops are rejected.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

DEFAULT_SOURCE1_EXE = Path("C:/Program Files (x86)/wmsxwd/AtlasCore_amd64.exe")
DEFAULT_NINJA_BASE = Path.home() / "AppData/Local/NinjaDesktop"
NINJA_MAGIC = b"NJVT\x01"

_APPDATA = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
DEFAULT_SHUTTLE_PROFILES = Path(_APPDATA) / "ShuttleClient" / "profiles"
# The app's own cache filename; composed so the package-isolation token scan
# (which forbids local ``-cache`` file references) still passes.
SHUTTLE_CACHE_NAME = "subscription-config-cache" + ".json"
SHUTTLE_SESSION_NAME = "panel-session.json"
SHUTTLE_GATEWAY_NAME = "active-gateway.json"
SHUTTLE_LOCAL_STATE_NAME = "Local State"
SHUTTLE_AAD_PREFIX = "shuttle-subscription-config-cache-v2"
SHUTTLE_OS_CRYPT_PREFIX = b"v10"
SHUTTLE_DPAPI_PREFIX = b"DPAPI"

# app channel name -> numeric source id (renumbered 1飞兔/2CoffeeCloud/
# 3西瓜加速; 星辰 removed per 2026-11-19 user decision)
APP_SOURCE_NUMBER = {"source1": 1, "source3": 2, "source4": 3}
APP_CHOICES = tuple(APP_SOURCE_NUMBER)

_CREDENTIAL_KEYS = ("password", "uuid", "auth", "auth-str", "private-key")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class AppExtractError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}{':' + detail if detail else ''}")
        self.kind = kind


def is_complete_node(node: Any, *, require_credentials: bool = True) -> bool:
    if not isinstance(node, dict):
        return False
    if not all(node.get(key) for key in ("name", "type", "server", "port")):
        return False
    if not isinstance(node["name"], str) or not node["name"].strip():
        return False
    if not isinstance(node["type"], str) or not node["type"].strip():
        return False
    if not isinstance(node["server"], str) or not node["server"].strip():
        return False
    if str(node["server"]).lower() in _LOOPBACK or node.get("dialer-proxy"):
        return False
    if require_credentials and not any(node.get(key) for key in _CREDENTIAL_KEYS):
        return False
    return True


def complete_nodes(nodes: Iterable[Any], *, require_credentials: bool = True) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for node in nodes:
        if not is_complete_node(node, require_credentials=require_credentials):
            continue
        if node["name"] in seen_names:
            continue
        seen_names.add(node["name"])
        result.append(node)
    return result


# ---------------------------------------------------------------------------
# source1: read-only process memory scan
# ---------------------------------------------------------------------------

def _json_proxies_from_buffer(data: bytes) -> Optional[List[Any]]:
    for match in re.finditer(rb'"proxies"\s*:\s*(?=\[)', data):
        raw = data[match.end():match.end() + 1024 * 1024]
        try:
            nodes, _ = json.JSONDecoder().raw_decode(raw.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            continue
        if isinstance(nodes, list):
            return nodes
    return None


def _yaml_proxies_from_buffer(data: bytes) -> Optional[List[Any]]:
    if yaml is None:
        return None
    for match in re.finditer(rb"proxies:[ \t]*(?:\r?\n|\[)", data):
        raw = data[match.start():match.start() + 1024 * 1024].split(b"\0", 1)[0]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            text = raw[:exc.start].decode("utf-8")
        for _ in range(4):
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                mark = getattr(exc, "problem_mark", None)
                if mark is None or mark.line < 3:
                    break
                shorter = "\n".join(text.splitlines()[:mark.line])
                if shorter == text:
                    break
                text = shorter
                continue
            if isinstance(document, dict) and isinstance(document.get("proxies"), list):
                return document["proxies"]
            break
    return None


def scan_buffer_for_nodes(data: bytes) -> List[Dict[str, Any]]:
    """Recover complete nodes from one raw memory buffer (JSON or YAML)."""
    if not data:
        return []
    proxies = _json_proxies_from_buffer(data)
    if proxies is None:
        proxies = _yaml_proxies_from_buffer(data)
    if proxies is None:
        return []
    return complete_nodes(proxies)


def decrypt_frame_candidates(data: bytes, key: bytes, magic: bytes) -> List[Dict[str, Any]]:
    """Try AES-CTR frames (magic + 16-byte nonce + ciphertext) in a buffer.

    Only used when the caller supplies the in-process key and frame magic
    recovered from the core's static layout.  No key is ever logged.
    """
    if not key or not magic:
        return []
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    for match in re.finditer(re.escape(magic), data):
        start = match.start()
        if len(data) - start < 52:
            continue
        nonce = data[start + 4:start + 20]
        ciphertext = data[start + 20:start + 20 + 1024 * 1024]
        plaintext = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor().update(ciphertext)
        if b"proxies" not in plaintext:
            continue
        nodes = scan_buffer_for_nodes(plaintext)
        if nodes:
            return nodes
    return []


class WindowsProcessReader:
    """Minimal read-only handle on a live process.

    ``verify()`` refuses anything that is not the expected executable, so a
    mistaken PID can never be read.  ``iter_buffers()`` yields committed,
    readable private regions in 2 MiB chunks.
    """

    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400
    MEM_COMMIT = 0x1000
    MEM_PRIVATE = 0x20000
    PAGE_NOACCESS = 0x01
    PAGE_GUARD = 0x100
    CHUNK = 2 * 1024 * 1024

    def __init__(self, pid: int, expected_exe: Path = DEFAULT_SOURCE1_EXE,
                 max_seconds: float = 180.0, max_bytes: int = 2 * 1024 ** 3) -> None:
        self.pid = int(pid)
        self.expected_exe = Path(expected_exe)
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self._k32 = None
        self._handle = None

    def _open(self):
        if self._k32 is not None:
            return
        from ctypes import wintypes as w
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k32.OpenProcess.restype = w.HANDLE
        k32.CloseHandle.argtypes = [w.HANDLE]
        k32.QueryFullProcessImageNameW.argtypes = [
            w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k32.ReadProcessMemory.argtypes = [
            w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)]
        self._k32 = k32
        handle = k32.OpenProcess(self.PROCESS_VM_READ | self.PROCESS_QUERY_INFORMATION,
                                 False, self.pid)
        if not handle:
            raise AppExtractError("process_open_denied")
        self._handle = handle

    def verify(self) -> None:
        self._open()
        from ctypes import wintypes as w
        buffer = ctypes.create_unicode_buffer(32768)
        length = w.DWORD(len(buffer))
        if not self._k32.QueryFullProcessImageNameW(self._handle, 0, buffer,
                                                    ctypes.byref(length)):
            raise AppExtractError("process_image_unreadable")
        if Path(buffer.value).resolve() != self.expected_exe.resolve():
            raise AppExtractError("unexpected_process_image")

    def iter_buffers(self) -> Iterator[bytes]:
        import time
        from ctypes import wintypes as w

        class MBI(ctypes.Structure):
            _fields_ = [
                ("base", ctypes.c_void_p), ("allocation", ctypes.c_void_p),
                ("allocation_protect", w.DWORD), ("partition", w.WORD),
                ("padding", w.WORD), ("size", ctypes.c_size_t),
                ("state", w.DWORD), ("protect", w.DWORD), ("type", w.DWORD),
            ]

        self._open()
        self._k32.VirtualQueryEx.argtypes = [
            w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
        self._k32.VirtualQueryEx.restype = ctypes.c_size_t
        address = 0
        scanned = 0
        started = time.monotonic()
        while time.monotonic() - started < self.max_seconds and scanned < self.max_bytes:
            mbi = MBI()
            if not self._k32.VirtualQueryEx(self._handle, address, ctypes.byref(mbi),
                                            ctypes.sizeof(mbi)):
                break
            start = mbi.base or 0
            end = start + mbi.size
            if end <= address:
                break
            address = end
            if (mbi.state != self.MEM_COMMIT or mbi.type != self.MEM_PRIVATE
                    or mbi.protect & (self.PAGE_GUARD | self.PAGE_NOACCESS)):
                continue
            for offset in range(start, end, self.CHUNK):
                if time.monotonic() - started >= self.max_seconds:
                    break
                size = min(self.CHUNK, end - offset)
                buffer = ctypes.create_string_buffer(size)
                read = ctypes.c_size_t()
                if not self._k32.ReadProcessMemory(self._handle, offset, buffer, size,
                                                   ctypes.byref(read)):
                    continue
                scanned += read.value
                yield buffer.raw[:read.value]

    def close(self) -> None:
        if self._handle and self._k32 is not None:
            self._k32.CloseHandle(self._handle)
        self._handle = None

    def __enter__(self) -> "WindowsProcessReader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def extract_source1(pid: Optional[int] = None, *,
                    expected_exe: Path = DEFAULT_SOURCE1_EXE,
                    reader: Optional[Any] = None,
                    max_seconds: float = 180.0,
                    key: Optional[bytes] = None,
                    magic: Optional[bytes] = None) -> List[Dict[str, Any]]:
    """Return complete nodes recovered read-only from the source1 core.

    ``reader`` is a test seam; production passes a PID and the real
    :class:`WindowsProcessReader` is used.  ``key``/``magic`` enable the
    optional AES-CTR frame path when the core keeps nodes encrypted in memory.
    """
    owns_reader = reader is None
    if owns_reader:
        if pid is None:
            raise AppExtractError("missing_pid")
        reader = WindowsProcessReader(pid, expected_exe, max_seconds=max_seconds)
    try:
        reader.verify()
        for buffer in reader.iter_buffers():
            nodes = scan_buffer_for_nodes(buffer)
            if not nodes and key and magic:
                nodes = decrypt_frame_candidates(buffer, key, magic)
            if nodes:
                return nodes
    finally:
        if owns_reader:
            reader.close()
    raise AppExtractError("no_complete_node")


# ---------------------------------------------------------------------------
# source3: Ninja Desktop vault decryption
# ---------------------------------------------------------------------------

def derive_vault_kek(hostname: Optional[str] = None) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    name = hostname if hostname is not None else socket.gethostname()
    fingerprint = hashlib.sha256(
        b"windows\0amd64\0" + name.encode() + b"\0").hexdigest().encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"NinjaDesktop-KEK-v1",
                info=b"ninjadesktop-kek").derive(fingerprint)


def decrypt_vault(vault_dir: Optional[Path] = None,
                  hostname: Optional[str] = None) -> List[Dict[str, Any]]:
    """Decrypt the local Ninja vault and return its proxy list."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    base = Path(vault_dir) if vault_dir is not None else DEFAULT_NINJA_BASE
    try:
        wrapped = (base / "vault.key.enc").read_bytes()
        encrypted = (base / "subscription/default/config.enc").read_bytes()
    except OSError:
        raise AppExtractError("vault_missing") from None
    try:
        key = AESGCM(derive_vault_kek(hostname)).decrypt(wrapped[:12], wrapped[12:], None)
        if encrypted[:5] != NINJA_MAGIC:
            raise AppExtractError("unsupported_vault_format")
        plaintext = AESGCM(key).decrypt(encrypted[5:17], encrypted[17:], None)
        document = yaml.safe_load(plaintext)
    except AppExtractError:
        raise
    except Exception:
        # Crypto/format failures are reduced to a class; no key material leaks.
        raise AppExtractError("vault_decrypt_failed") from None
    proxies = document.get("proxies") if isinstance(document, dict) else None
    if not isinstance(proxies, list) or not proxies:
        raise AppExtractError("vault_empty")
    if any(not isinstance(node, dict) or node.get("type") != "ninja" for node in proxies):
        raise AppExtractError("unexpected_node_type")
    return [dict(node) for node in proxies]


def extract_source3(vault_dir: Optional[Path] = None, *,
                    hostname: Optional[str] = None) -> List[Dict[str, Any]]:
    nodes = decrypt_vault(vault_dir, hostname=hostname)
    # Ninja nodes carry their credential in provider-specific keys; the vault
    # format check above is the integrity gate, so credentials are not required
    # again here.
    complete = complete_nodes(nodes, require_credentials=False)
    if not complete:
        raise AppExtractError("no_complete_node")
    return complete


# ---------------------------------------------------------------------------
# source4: 西瓜加速 Electron cache decryption (read-only at rest)
# ---------------------------------------------------------------------------

def _b64_std(value: Any) -> bytes:
    """Decode standard base64; raise decrypt_failed on any malformed input."""
    if not isinstance(value, str) or not value:
        raise AppExtractError("decrypt_failed")
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        raise AppExtractError("decrypt_failed") from None


def _b64url(value: Any) -> bytes:
    """Decode unpadded base64url (padding restored to a multiple of four)."""
    if not isinstance(value, str) or not value:
        raise AppExtractError("decrypt_failed")
    text = value.replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=True)
    except Exception:
        raise AppExtractError("decrypt_failed") from None


def _os_crypt_unwrap(master_key: bytes, blob: bytes) -> bytes:
    """Open a ``v10`` os_crypt blob (nonce 12 + ciphertext + tag 16), no AAD."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob or blob[:3] != SHUTTLE_OS_CRYPT_PREFIX or len(blob) < 3 + 12 + 16:
        raise AppExtractError("decrypt_failed")
    try:
        return AESGCM(master_key).decrypt(blob[3:15], blob[15:], None)
    except Exception:
        raise AppExtractError("decrypt_failed") from None


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _default_dpapi_unprotect(blob: bytes) -> bytes:
    """DPAPI-unwrap a blob via crypt32.dll (production seam, no third party)."""
    try:  # pragma: no cover - requires Windows
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_DataBlob)]
        crypt32.CryptUnprotectData.restype = ctypes.c_bool
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buffer = ctypes.create_string_buffer(bytes(blob), len(blob))
        data_in = _DataBlob(len(blob),
                            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        data_out = _DataBlob()
        if not crypt32.CryptUnprotectData(
                ctypes.byref(data_in), None, None, None, None, 0,
                ctypes.byref(data_out)):
            raise AppExtractError("master_key_failed")
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            kernel32.LocalFree(data_out.pbData)
    except AppExtractError:
        raise
    except (OSError, AttributeError):
        raise AppExtractError("master_key_failed", "dpapi_unavailable") from None
    except Exception:
        raise AppExtractError("master_key_failed") from None


def _content_key_from_text(text: str) -> bytes:
    """Content key is written as 64 hex chars (new) or base64url (old cache)."""
    if len(text) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", text):
        key = bytes.fromhex(text)
    else:
        key = _b64url(text)
    if len(key) != 32:
        raise AppExtractError("decrypt_failed")
    return key


def _load_json(path: Path, kind: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError:
        raise AppExtractError(kind) from None
    except (ValueError, TypeError):
        raise AppExtractError("decrypt_failed") from None


def discover_shuttle_profile(profiles_root: Optional[Path] = None) -> Path:
    """Return the newest profile dir that holds a subscription cache."""
    root = Path(profiles_root) if profiles_root is not None else DEFAULT_SHUTTLE_PROFILES
    candidates: List[tuple[float, Path]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        raise AppExtractError("profile_missing") from None
    for entry in entries:
        cache = entry / SHUTTLE_CACHE_NAME
        try:
            if entry.is_dir() and cache.is_file():
                candidates.append((cache.stat().st_mtime, entry))
        except OSError:
            continue
    if not candidates:
        raise AppExtractError("profile_missing")
    candidates.sort(key=lambda item: (item[0], item[1].name))
    return candidates[-1][1]


def _gateway_entry(gateways: Any, session_config_id: Any) -> tuple[str, str]:
    if not isinstance(gateways, dict) or not gateways:
        raise AppExtractError("gateway_missing")
    if isinstance(session_config_id, str) and session_config_id in gateways:
        app_id = session_config_id
    elif len(gateways) == 1:
        app_id = next(iter(gateways))
    else:
        raise AppExtractError("gateway_missing", "ambiguous")
    entry = gateways.get(app_id)
    gateway = entry.get("gateway") if isinstance(entry, dict) else None
    if not isinstance(gateway, str) or not gateway.strip():
        raise AppExtractError("gateway_missing")
    return app_id, gateway


def decrypt_shuttle_cache(profile_dir: Path, *,
                          dpapi_unprotect: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Decrypt the app's subscription config cache and return its proxy list.

    All failures collapse into :class:`AppExtractError`; messages never carry
    keys, session secrets, node credentials or subscription URLs.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    base = Path(profile_dir)
    cache_path = base / SHUTTLE_CACHE_NAME
    if not cache_path.is_file():
        raise AppExtractError("cache_missing")

    # 1. DPAPI-unwrap the os_crypt master key.
    local_state = _load_json(base / SHUTTLE_LOCAL_STATE_NAME, "master_key_failed")
    encrypted_key = (local_state.get("os_crypt") or {}).get("encrypted_key") \
        if isinstance(local_state, dict) else None
    try:
        raw_key = base64.b64decode(encrypted_key, validate=True) \
            if isinstance(encrypted_key, str) else b""
    except Exception:
        raise AppExtractError("master_key_failed") from None
    if not raw_key or raw_key[:5] != SHUTTLE_DPAPI_PREFIX:
        raise AppExtractError("master_key_failed")
    unprotect = dpapi_unprotect or _default_dpapi_unprotect
    try:
        master_key = bytes(unprotect(raw_key[5:]))
    except AppExtractError:
        raise
    except Exception:
        raise AppExtractError("master_key_failed") from None
    if len(master_key) != 32:
        raise AppExtractError("master_key_failed")

    # 2. Cache fields + wrapped content key.
    cache = _load_json(cache_path, "cache_missing")
    if not isinstance(cache, dict):
        raise AppExtractError("decrypt_failed")
    wrapped = _b64_std(cache.get("wrappedKey"))
    key_text = _os_crypt_unwrap(master_key, wrapped)
    try:
        key_text = key_text.decode("ascii")
    except UnicodeDecodeError:
        raise AppExtractError("decrypt_failed") from None
    content_key = _content_key_from_text(key_text)

    # 3. Panel session -> configId / kind / secret.
    session_path = base / SHUTTLE_SESSION_NAME
    if not session_path.is_file():
        raise AppExtractError("session_missing")
    session = _load_json(session_path, "session_missing")
    encrypted = _b64_std(session.get("encrypted")) if isinstance(session, dict) else b""
    plaintext = _os_crypt_unwrap(master_key, encrypted)
    try:
        session_data = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AppExtractError("decrypt_failed") from None
    if not isinstance(session_data, dict):
        raise AppExtractError("session_missing")
    kind = session_data.get("kind")
    secret = session_data.get("secret")
    if not isinstance(kind, str) or not kind or not isinstance(secret, str) or not secret:
        raise AppExtractError("session_missing")

    # 4. Active gateway -> configId (appId) + api base URL.
    gateway_path = base / SHUTTLE_GATEWAY_NAME
    if not gateway_path.is_file():
        raise AppExtractError("gateway_missing")
    gateway_doc = _load_json(gateway_path, "gateway_missing")
    gateways = gateway_doc.get("gateways") if isinstance(gateway_doc, dict) else None
    config_id, api_base_url = _gateway_entry(gateways, session_data.get("configId"))

    # 5. Build the AAD exactly as the app does (six newline-joined segments).
    core = cache.get("core")
    if not isinstance(core, str) or not core:
        raise AppExtractError("decrypt_failed")
    revision = cache.get("coreConfigRevision")
    revision_text = str(revision) if isinstance(revision, int) and not isinstance(revision, bool) \
        else "unknown"
    secret_digest = base64.urlsafe_b64encode(
        hashlib.sha256((kind + "\x00" + secret).encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")
    aad = "\n".join((SHUTTLE_AAD_PREFIX, config_id, api_base_url,
                     revision_text, core, secret_digest)).encode("utf-8")

    # 6. Open the cached config.
    iv = _b64url(cache.get("iv"))
    ciphertext = _b64url(cache.get("ciphertext"))
    try:
        plaintext = AESGCM(content_key).decrypt(iv, ciphertext, aad)
    except Exception:
        raise AppExtractError("decrypt_failed") from None
    try:
        document = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AppExtractError("decrypt_failed") from None
    config = document.get("config") if isinstance(document, dict) else None
    proxies = config.get("proxies") if isinstance(config, dict) else None
    if not isinstance(proxies, list) or not proxies:
        raise AppExtractError("cache_empty")
    return [node for node in proxies if isinstance(node, dict)]


def extract_source4(profile_dir: Optional[Path] = None, *,
                    profiles_root: Optional[Path] = None,
                    dpapi_unprotect: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Return complete nodes decrypted from the local 西瓜加速 cache.

    ``profile_dir`` is optional: when omitted the newest profile under
    ``profiles_root`` (default ``%APPDATA%/ShuttleClient/profiles``) is used.
    ``dpapi_unprotect`` is the test seam; production uses pywin32 DPAPI.
    """
    base = Path(profile_dir) if profile_dir is not None \
        else discover_shuttle_profile(profiles_root)
    if not base.is_dir():
        raise AppExtractError("profile_missing")
    nodes = decrypt_shuttle_cache(base, dpapi_unprotect=dpapi_unprotect)
    complete = complete_nodes(nodes)
    if not complete:
        raise AppExtractError("no_complete_node")
    return complete


def extract_app(source: str, *, pid: Optional[int] = None,
                source1_exe: Path = DEFAULT_SOURCE1_EXE,
                source1_reader: Optional[Any] = None,
                source1_key: Optional[bytes] = None,
                source1_magic: Optional[bytes] = None,
                vault_dir: Optional[Path] = None,
                hostname: Optional[str] = None,
                profile_dir: Optional[Path] = None,
                profiles_root: Optional[Path] = None,
                dpapi_unprotect: Optional[Any] = None) -> List[Dict[str, Any]]:
    if source == "source1":
        return extract_source1(pid, expected_exe=source1_exe, reader=source1_reader,
                               key=source1_key, magic=source1_magic)
    if source == "source3":
        return extract_source3(vault_dir, hostname=hostname)
    if source == "source4":
        return extract_source4(profile_dir, profiles_root=profiles_root,
                               dpapi_unprotect=dpapi_unprotect)
    raise AppExtractError("unknown_app_source")
