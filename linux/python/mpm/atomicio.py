"""Atomic, mode-0600 file writes and strict permission checks.

Rules enforced here (phase-3 spec, section 5):

* secret files are written as ``tmp in the same directory`` -> ``0600`` ->
  ``os.replace`` (atomic on the same filesystem)
* temporary directories get a random name and mode ``0700``
* temporary secret files get mode ``0600``
* secret *inputs* must be a regular file, must not be a symlink, must be owned
  by root for the system profile and must have zero group/other permission bits
* text is normalised to UTF-8, no BOM, LF endings
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import shutil
import stat
from typing import Iterator

from .errors import FailClosed

TEXT_MODE = 0o600
DIR_MODE = 0o700


def strip_bom(text: str) -> str:
    """Remove a leading BOM and normalise CRLF/CR to LF."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_text(text: str) -> str:
    """BOM-free, LF-only, guaranteed trailing newline."""
    text = strip_bom(text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def tmp_name(prefix: str = "tmp") -> str:
    return f"{prefix}.{secrets.token_hex(8)}"


@contextlib.contextmanager
def secure_tempdir(parent: str, prefix: str = "staging") -> Iterator[str]:
    """Create a ``0700`` directory with a random name under ``parent``."""
    os.makedirs(parent, exist_ok=True)
    os.chmod(parent, 0o700)
    path = os.path.join(parent, tmp_name(prefix))
    os.mkdir(path, DIR_MODE)
    os.chmod(path, DIR_MODE)
    try:
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(path, ignore_errors=True)


def write_atomic(path: str, content: str | bytes, *, mode: int = 0o644, mkdir: bool = True) -> None:
    """Write ``content`` then atomically replace ``path`` with mode ``mode``."""
    directory = os.path.dirname(path) or "."
    if mkdir:
        os.makedirs(directory, exist_ok=True)
    if isinstance(content, str):
        data = normalize_text(content).encode("utf-8")
    else:
        data = content
    tmp = os.path.join(directory, tmp_name(".mpm"))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        os.chmod(path, mode)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_text_strict(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError as exc:
        raise FailClosed(f"missing file: {path}") from exc
    except OSError as exc:
        raise FailClosed(f"cannot read file: {path} ({_errno_name(exc)})") from exc
    try:
        return strip_bom(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise FailClosed(f"file is not valid UTF-8: {path}") from exc


def _errno_name(exc: OSError) -> str:
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) == errno.EACCES:
        return "EACCES"
    if getattr(exc, "errno", None) == errno.EPERM:
        return "EPERM"
    return f"errno={getattr(exc, 'errno', '?')}"


def check_secret_input(path: str, *, require_root_owned: bool = True) -> os.stat_result:
    """Fail closed unless ``path`` is a safe secret input file.

    Rejected: symlink, non-regular file, missing, group/other permission bits,
    non-root ownership (system profile).
    """
    if not os.path.exists(path) and not os.path.islink(path):
        raise FailClosed(f"secret input not found: {path}")
    if os.path.islink(path):
        raise FailClosed(f"refusing symlinked secret input: {path}")
    if not os.path.isfile(path):
        raise FailClosed(f"secret input is not a regular file: {path}")
    info = os.stat(path, follow_symlinks=False)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise FailClosed(
            "secret input permissions too wide: %s (mode %o, need no group/other bits)"
            % (path, stat.S_IMODE(info.st_mode))
        )
    if require_root_owned and (info.st_uid != 0 or info.st_gid != 0):
        raise FailClosed(
            f"secret input must be owned by root:root for the system profile: {path}"
        )
    return info


def check_output_mode(path: str, *, max_mode: int = 0o600) -> None:
    """Assert the on-disk mode is no wider than ``max_mode``."""
    if not os.path.exists(path):
        raise FailClosed(f"expected file missing: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    extra = mode & ~max_mode
    if extra:
        raise FailClosed(
            "file permission too wide: %s (mode %o, extra bits %o)" % (path, mode, extra)
        )


def chmod_strict(path: str, mode: int) -> None:
    os.chmod(path, mode)


def ensure_dir(path: str, mode: int) -> None:
    """Create ``path`` (parents included) and force its mode."""
    os.makedirs(path, exist_ok=True)
    os.chmod(path, mode)


def is_noop(path: str, content: str | bytes, *, mode: int) -> bool:
    """True when the file already holds exactly ``content`` at ``mode``.

    Used by ``configure`` to avoid meaningless rewrites (section 10).
    """
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as handle:
            existing = handle.read()
    except OSError:
        return False
    expected = normalize_text(content).encode("utf-8") if isinstance(content, str) else content
    if existing != expected:
        return False
    try:
        return stat.S_IMODE(os.stat(path).st_mode) == mode
    except OSError:
        return False


def make_symlink_atomic(target: str, link_path: str) -> None:
    """Atomically (re)point ``link_path`` at ``target``."""
    directory = os.path.dirname(link_path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, tmp_name(".mpmlink"))
    os.symlink(target, tmp)
    os.replace(tmp, link_path)


def remove_if_present(path: str) -> bool:
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
            return True
        if os.path.isdir(path):
            shutil.rmtree(path)
            return True
    except FileNotFoundError:
        return False
    return False


def temp_path_in(directory: str, prefix: str = ".mpm") -> str:
    return os.path.join(directory, tmp_name(prefix))


__all__ = [
    "DIR_MODE",
    "TEXT_MODE",
    "check_output_mode",
    "check_secret_input",
    "chmod_strict",
    "ensure_dir",
    "is_noop",
    "make_symlink_atomic",
    "normalize_text",
    "read_text_strict",
    "remove_if_present",
    "secure_tempdir",
    "strip_bom",
    "tmp_name",
    "write_atomic",
]
