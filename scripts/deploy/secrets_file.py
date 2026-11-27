"""The machine's secret file: merge keys in, write it, keep it private.

Both platforms keep the Feishu credentials next to the core's own secrets in
one KEY=VALUE file (``subscription.env``).  Re-running the bootstrap must never
drop keys that are already there - a hand-added subscription URL, say - so the
merge is a read-modify-write over the existing text.  POSIX hosts get a 0600
file replaced atomically; Windows gets an explicit ACL for SYSTEM and
Administrators, applied to the empty file *before* any secret is written.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Callable, Mapping, Optional

#: ``icacls`` with well-known SIDs instead of localised group names:
#: S-1-5-18 is SYSTEM, S-1-5-32-544 is Administrators.
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
TEMP_PREFIX = ".tmp-"


class SecretFileError(Exception):
    """Raised instead of leaking file contents: a ``kind`` plus a bare detail."""

    def __init__(self, kind: str, detail: str = "") -> None:
        message = f"secret file {kind}" + (f" ({detail})" if detail else "")
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def _line_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    return stripped.split("=", 1)[0].strip()


def merge_env_text(existing_text: str, updates: Mapping[str, str]) -> str:
    """Merge ``updates`` into KEY=VALUE text (pure - never touches a file).

    Comments, blank lines and unrelated keys keep their places.  An existing
    key is rewritten where it first appears and any later duplicate of it is
    dropped: the pipeline's env reader refuses files with duplicate keys.
    """
    for key, value in updates.items():
        if (not isinstance(key, str) or not key or key.strip() != key
                or key.startswith("#") or "=" in key
                or "\n" in key or "\r" in key):
            raise ValueError("invalid secret key name")
        if not isinstance(value, str):
            raise ValueError(f"secret value must be a string: {key}")
        if "\n" in value or "\r" in value:
            raise ValueError(f"secret value must stay on one line: {key}")
    newline = "\r\n" if "\r\n" in existing_text else "\n"
    lines = existing_text.lstrip("\ufeff").split("\n")
    if newline == "\r\n":
        lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    output: list = []
    written: set = set()
    for line in lines:
        key = _line_key(line)
        if key not in updates:
            output.append(line)
        elif key in written:
            continue  # a later duplicate of a key we own: drop the whole line
        else:
            output.append(f"{key}={updates[key]}")
            written.add(key)
    while output and not output[-1].strip():
        output.pop()  # trailing blank lines collapse into the one final newline
    output.extend(f"{key}={value}" for key, value in updates.items()
                  if key not in written)
    while output and not output[0].strip():
        output.pop(0)
    if not output:
        return ""
    return newline.join(output) + newline


def icacls_argv(path: str) -> list:
    """Drop inherited access, then grant SYSTEM and Administrators full control."""
    return ["icacls", path, "/inheritance:r", "/grant:r",
            f"*{SYSTEM_SID}:(F)", f"*{ADMINISTRATORS_SID}:(F)"]


def _directory(path: str) -> str:
    return os.path.dirname(os.path.abspath(path)) or "."


def _write_posix(path: str, text: str) -> None:
    """0600 is set on the temp file *before* the secret exists anywhere."""
    handle, temporary = tempfile.mkstemp(dir=_directory(path), prefix=TEMP_PREFIX)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        _discard(temporary)
        raise


def _write_in_place(path: str, text: str) -> None:
    """Windows: rewriting the file itself keeps the ACL that was just set on it
    (a rename would hand the new file the directory's inherited access)."""
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def _discard(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def write_secret_file(path: str, text: str, *, os_name: str,
                      runner: Optional[Callable] = None) -> dict:
    """Write ``text`` to ``path`` so that only the service identities can read it.

    ``os_name`` is a parameter (not ``os.name``) so the Windows branch can be
    exercised on any host.  ``runner(argv)`` executes ``icacls`` and returns
    either the exit code or ``(exit_code, stdout, stderr)``; ``None`` means a
    real ``subprocess.run``.  A failed ACL step leaves nothing secret behind:
    a file this call created is removed, an existing one was not written yet.
    """
    try:
        if os_name != "nt":
            _write_posix(path, text)
            return {"path": path, "protection": "0600"}

        created = False
        if not os.path.exists(path):
            with open(path, "a", encoding="utf-8", newline=""):
                pass  # empty file: nothing to protect yet
            created = True
        argv = icacls_argv(path)
        try:
            if runner is None:
                code = subprocess.run(argv, capture_output=True, text=True,
                                      shell=False).returncode
            else:
                result = runner(argv)
                code = result[0] if isinstance(result, tuple) else result
        except Exception as exc:  # noqa: BLE001 - the ACL must hold before writing
            code = type(exc).__name__
        if code != 0:
            if created:
                _discard(path)
            raise SecretFileError("acl_failed", str(code))
        _write_in_place(path, text)
        return {"path": path, "protection": "acl", "argv": argv}
    except OSError as exc:
        raise SecretFileError("write_failed", type(exc).__name__) from None
