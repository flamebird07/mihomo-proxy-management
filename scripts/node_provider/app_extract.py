"""App-channel node extraction (explicit, opt-in, read-only).

Two app sources are supported.  Both are *one-time explicit imports*: nothing
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

Both extractors are injectable for tests: ``extract_source1(reader=...)``
accepts any object exposing ``verify()`` and ``iter_buffers()``, and
``extract_source3(vault_dir=..., hostname=...)`` accepts a fixture directory.

Node completeness (shared with the merge step): name, type, server, port and a
credential field; loopback endpoints and dialer-proxy hops are rejected.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
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

# app channel name -> numeric source id (1飞兔/2星辰/3CoffeeCloud/4西瓜加速)
APP_SOURCE_NUMBER = {"source1": 1, "source3": 3}
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


def extract_app(source: str, *, pid: Optional[int] = None,
                source1_exe: Path = DEFAULT_SOURCE1_EXE,
                source1_reader: Optional[Any] = None,
                source1_key: Optional[bytes] = None,
                source1_magic: Optional[bytes] = None,
                vault_dir: Optional[Path] = None,
                hostname: Optional[str] = None) -> List[Dict[str, Any]]:
    if source == "source1":
        return extract_source1(pid, expected_exe=source1_exe, reader=source1_reader,
                               key=source1_key, magic=source1_magic)
    if source == "source3":
        return extract_source3(vault_dir, hostname=hostname)
    raise AppExtractError("unknown_app_source")
