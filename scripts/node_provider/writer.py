"""Atomic provider YAML writer.

The output file is the only local artifact this module produces.  No state or
cache file is ever written.  The target directory must already exist; the
temporary file is created in that same directory and ``os.replace`` makes the
swap atomic on Windows/POSIX.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import yaml

TEMP_PREFIX = ".node_provider."
TEMP_SUFFIX = ".tmp"


class OutputError(Exception):
    """Output path is unusable (missing directory, unwritable target)."""


def render_provider(proxies: List[Dict[str, Any]]) -> str:
    document = {"proxies": list(proxies)}
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)


def atomic_write(path: str, proxies: List[Dict[str, Any]]) -> str:
    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    if not directory.is_dir():
        raise OutputError("output directory does not exist")
    text = render_provider(proxies)
    fd, temporary = tempfile.mkstemp(dir=str(directory), prefix=TEMP_PREFIX,
                                     suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, str(target))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return str(target)
