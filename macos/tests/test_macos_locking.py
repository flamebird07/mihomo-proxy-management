"""``flock`` locking semantics, exercised with an injected fake on any OS."""

from __future__ import annotations

import os

import pytest

from mpm_macos.locking import FileLock, LockBusy, is_held, read_metadata

from macos_test_support import FakeFlock

pytestmark = pytest.mark.macos


def _lock(path, fake, label="core"):
    return FileLock(str(path), label=label, flock=fake)


def test_first_acquire_succeeds_and_records_non_secret_metadata(tmp_path):
    path = tmp_path / "run" / "core.lock"
    fake = FakeFlock()
    lock = _lock(path, fake)
    lock.acquire()
    try:
        assert lock.held is True
        assert path.is_file()
        metadata = read_metadata(str(path))
        assert metadata["pid"] == os.getpid()
        assert metadata["mode"] == "core"
        assert isinstance(metadata["time"], float)
        assert "secret" not in path.read_text(encoding="utf-8").lower()
    finally:
        lock.release()
    assert lock.held is False


def test_second_instance_fails_immediately(tmp_path):
    path = tmp_path / "core.lock"
    fake = FakeFlock()
    first = _lock(path, fake)
    first.acquire()
    try:
        with pytest.raises(LockBusy):
            _lock(path, fake).acquire()
    finally:
        first.release()


def test_lock_is_reusable_after_release(tmp_path):
    path = tmp_path / "core.lock"
    fake = FakeFlock()
    first = _lock(path, fake)
    first.acquire()
    first.release()
    second = _lock(path, fake)
    second.acquire()
    second.release()
    assert fake.held is False


def test_lock_file_is_not_deleted_on_release(tmp_path):
    path = tmp_path / "core.lock"
    fake = FakeFlock()
    lock = _lock(path, fake)
    lock.acquire()
    lock.release()
    assert path.exists(), "deleting the lock file would reintroduce the stale-file race"


def test_exception_inside_the_context_releases_the_lock(tmp_path):
    path = tmp_path / "core.lock"
    fake = FakeFlock()
    with pytest.raises(RuntimeError):
        with _lock(path, fake):
            raise RuntimeError("boom")
    assert fake.held is False
    with _lock(path, fake):
        pass


def test_is_held_reflects_the_current_owner(tmp_path):
    path = tmp_path / "core.lock"
    fake = FakeFlock()
    lock = _lock(path, fake)
    lock.acquire()
    try:
        assert is_held(str(path), flock=fake) is True
    finally:
        lock.release()
    assert is_held(str(path), flock=fake) is False


def test_is_held_on_a_missing_file(tmp_path):
    assert is_held(str(tmp_path / "missing.lock"), flock=FakeFlock()) is False
