"""Fetch bytes and release metadata over HTTPS (stdlib urllib only).

Only :func:`fetch_to_file` and :func:`fetch_json` talk to the network in the
whole deploy kit; both take an injectable ``opener`` so they are testable
offline.  A download is atomic: the destination appears only once every byte
is on disk, so a half file never passes for a staged binary.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from typing import Optional

USER_AGENT = "mihomo-proxy-management-bootstrap"
TEMP_PREFIX = ".tmp-"
#: ``fetch_json`` reads at most this many bytes before giving up.
MAX_JSON_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


def _default_opener(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def _check_https(original_url: str, response) -> None:
    """A redirect that lands on plain http is refused outright."""
    if original_url.startswith("https://"):
        final_url = ""
        getter = getattr(response, "geturl", None)
        if callable(getter):
            final_url = getter() or ""
        else:
            final_url = getattr(response, "url", "") or ""
        if final_url and not final_url.startswith("https://"):
            raise ValueError("https_downgrade")


def _status(response) -> int:
    return int(getattr(response, "status", 0) or 0)


def fetch_to_file(url: str, destination: str, *, timeout: float = 120,
                  opener=None) -> str:
    """Download ``url`` to ``destination`` atomically; return ``destination``.

    The temporary file is created in the destination's directory (which must
    already exist) and replaces the target only after every byte is flushed
    and fsynced.  Any failure removes the temporary file and leaves the
    destination exactly as it was - absent, or holding its previous content.
    """
    download = opener or _default_opener
    handle, temporary = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(destination)), prefix=TEMP_PREFIX)
    consumed = False  # os.fdopen took over the fd (or is about to)
    try:
        try:
            response = download(url, timeout)
        except BaseException:
            os.close(handle)
            raise
        with response:
            if not 200 <= _status(response) < 300:
                raise ValueError(f"http_{_status(response)}")
            _check_https(url, response)
            stream = os.fdopen(handle, "wb")
            consumed = True
            with stream:
                while True:
                    piece = response.read(CHUNK_BYTES)
                    if not piece:
                        break
                    stream.write(piece)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        if not consumed:
            try:
                os.close(handle)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def fetch_json(url: str, *, timeout: float = 30, opener=None) -> Optional[dict]:
    """The JSON object at ``url``, or ``None`` when anything is off.

    A non-2xx status, an unreachable host, a body that is not one JSON object,
    or a body beyond :data:`MAX_JSON_BYTES` all return ``None`` - callers
    treat that as "no API digest available" and fall back.  No exception
    escapes; nothing here leaks response text.
    """
    download = opener or _default_opener
    try:
        with download(url, timeout) as response:
            if not 200 <= _status(response) < 300:
                return None
            payload = bytearray()
            while len(payload) <= MAX_JSON_BYTES:
                piece = response.read(MAX_JSON_BYTES + 1 - len(payload))
                if not piece:
                    break
                payload.extend(piece)
    except Exception:  # noqa: BLE001 - every failure is a plain None
        return None
    if len(payload) > MAX_JSON_BYTES:
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
