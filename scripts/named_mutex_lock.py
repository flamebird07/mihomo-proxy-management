"""Windows named-mutex lock for the GPT stability switch (CY-GUS-DEP1).

This reuses the kernel32 ctypes shape from ``sync-feishu-bitable.py``
(lines 497-558): ``CreateMutexW(name)`` -> ``WaitForSingleObject(handle, 30s)``
-> treat ``WAIT_ABANDONED`` as acquired (a crashed holder is taken over) ->
``ReleaseMutex`` + ``CloseHandle`` on exit. A wait timeout raises ``LockBusy``.

On POSIX, or when ctypes/kernel32 is unavailable, it falls back to a
process-wide serial lock so non-Windows hosts and tests still serialise.

Factory signature: ``lock_factory(name) -> context manager``. The lock name is
``gpt_switch_entry.LOCK_NAME`` (``Local\\mihomo-gpt-stability-switch``); it is
deliberately not versioned, so every policy writer contends on one name.
"""

from __future__ import annotations

import os
from typing import Any

# Reuse the entry's lock name and exception class so that ``run_once``'s
# ``except LockBusy`` keeps catching what this module raises. The fallback
# keeps the module importable on its own.
try:
    from gpt_switch_entry import LOCK_NAME, LockBusy, serial_lock_factory
except ImportError:  # pragma: no cover - defensive standalone fallback
    import contextlib
    import threading

    LOCK_NAME = r"Local\mihomo-gpt-stability-switch"

    class LockBusy(RuntimeError):  # type: ignore[no-redef]
        """Another run owns the named lock."""

    # Module-level so every caller shares the same lock objects.
    _FALLBACK_LOCKS: dict[str, threading.Lock] = {}
    _FALLBACK_GUARD = threading.Lock()

    @contextlib.contextmanager
    def _fallback_serial_lock(lock_name: str):
        with _FALLBACK_GUARD:
            lock = _FALLBACK_LOCKS.setdefault(lock_name, threading.Lock())
        if not lock.acquire(timeout=30):
            raise LockBusy(lock_name)
        try:
            yield
        finally:
            lock.release()

    def serial_lock_factory(name: str = LOCK_NAME):  # type: ignore[misc]
        return _fallback_serial_lock(name)


# Wait budget aligned with serial_lock_factory's 30s.
WAIT_TIMEOUT_MS = 30_000

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
ERROR_ALREADY_EXISTS = 183

_KERNEL32: Any = None


def _kernel32() -> Any:
    """Load kernel32 once with explicit signatures (no pointer truncation)."""
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k32.ReleaseMutex.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = k32
    return _KERNEL32


def _last_error() -> int:
    import ctypes

    return ctypes.get_last_error()


def windows_mutex_available() -> bool:
    """True only when a real named mutex can be used on this host."""
    if os.name != "nt":
        return False
    try:
        _kernel32()
    except Exception:
        return False
    return True


class _NamedMutex:
    """One acquisition of a Windows named mutex, used as a context manager."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: Any = None
        self.took_over = False

    def __enter__(self) -> "_NamedMutex":
        k32 = _kernel32()
        handle = k32.CreateMutexW(None, True, self.name)
        if not handle:
            raise LockBusy(f"mutex_create_failed err={_last_error()}")
        if _last_error() == ERROR_ALREADY_EXISTS:
            # bInitialOwner is ignored for an existing object, so wait up to
            # the 30s budget for the current holder.
            rc = k32.WaitForSingleObject(handle, WAIT_TIMEOUT_MS)
            if rc not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                k32.CloseHandle(handle)
                raise LockBusy(self.name)
            if rc == WAIT_ABANDONED:
                # Previous owner died holding it; we take over.
                self.took_over = True
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        k32 = _kernel32()
        try:
            k32.ReleaseMutex(handle)
        finally:
            k32.CloseHandle(handle)


def named_mutex_lock_factory(name: str = LOCK_NAME):
    """Return a context manager that holds the named mutex ``name``."""
    if windows_mutex_available():
        return _NamedMutex(name)
    return serial_lock_factory(name)
