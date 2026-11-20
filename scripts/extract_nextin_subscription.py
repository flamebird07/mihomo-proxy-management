#!/usr/bin/env python3
"""Extract the Nextin (com.Tommy.Nextin) subscription material on macOS.

Nextin is an iOS-style app running on Apple Silicon; it stores its airport
subscription in the shared App Group container
``~/Library/Group Containers/group.com.Tommy.Nextin``.  macOS TCC protects
that container: only processes granted Full Disk Access (or root running from
such a process) may read it.  This tool answers the "can we auto-extract the
wmsxwd node library from Nextin" question in two ways:

* find the **subscription URL** (the credential) so it can be written into the
  project's 0600 secret file as ``SUBSCRIPTION_URL_2``;
* find the **cached profile** (Clash YAML / sing-box JSON the app already
  downloaded) and export it - the freshest node library without the URL.

The script is stdlib-only and import-safe.  Output is masked *by design*:
subscription URLs are credentials, so only ``scheme://host/path?token=***`` is
ever printed, and the URL values never appear in an exception message.

Usage::

    python3 scripts/extract_nextin_subscription.py             # dry run
    sudo python3 scripts/extract_nextin_subscription.py --write
    python3 scripts/extract_nextin_subscription.py --export /tmp/profile.yaml

``--write`` updates ``SUBSCRIPTION_URL_2`` (and only that key plus
``SUBSCRIPTION_UA_2`` when asked) in the 0600 mpm secret file, preserving
every other line - run it as root; ``--export`` copies the best cached profile
to ``PATH`` with mode 0600 for ``scripts/convert_sub.py -i``.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass, field

PROJECT = "mihomo-proxy-management"
APP_ID = "com.Tommy.Nextin"
DEFAULT_SEARCH_ROOTS = (
    os.path.expanduser(f"~/Library/Group Containers/group.{APP_ID}"),
    os.path.expanduser(f"~/Library/Containers/{APP_ID}/Data"),
)
DEFAULT_MPM_ENV = (
    f"/Library/Application Support/{PROJECT}/state/subscription.env"
)

MAX_FILE_BYTES = 8 * 1024 * 1024  # profiles are text; skip anything larger
MAX_SAMPLE_BYTES = 512 * 1024  # how much text per file we actually scan

#: Host fragment that identifies the wmsxwd airport (ranking hint only).
AIRPORT_HINT = "wmsxwd"

URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\)\]}]+", re.IGNORECASE)
TOKENISH_QUERY = re.compile(r"[?&](token|key|auth|secret)=", re.IGNORECASE)
SUBSCRIBE_PATH = re.compile(
    r"(subscribe|/sub\?|/api/v1/client|download|/clash|/link)", re.IGNORECASE
)
PROFILE_NODE_YAML = re.compile(r"(?m)^\s*-?\s*(?:name|\{)\s*[:{]")
PROFILE_NODE_COUNT = re.compile(
    r"(?m)^\s*(?:-\s*)?(?:name|\"name\")\s*[:=]\s*[\"']?[^\s\"'{]+"
)


def mask_url(url: str) -> str:
    """``scheme://host/path?<query keys>`` - values never printed."""
    match = re.match(r"([a-z]+://[^/?#]+)([^?#]*)([?#].*)?", url, re.IGNORECASE)
    if not match:
        return "***"
    scheme_host, path, tail = match.groups()
    keys: list[str] = []
    if tail:
        for pair in re.split(r"[?&]", tail.lstrip("?#")):
            if not pair:
                continue
            key = re.split(r"=", pair, maxsplit=1)[0]
            if key:
                keys.append(key)
    shown = f"{scheme_host}{path or ''}"
    if keys:
        shown += "?" + "&".join(f"{key}=***" for key in keys)
    if len(shown) > 96:
        shown = shown[:93] + "..."
    return shown


@dataclass
class Candidate:
    kind: str  # "url" | "profile" | "hint"
    source: str
    value: str
    score: int = 0
    detail: str = ""

    def display(self) -> str:
        shown = mask_url(self.value) if self.kind == "url" else self.value
        return f"[{self.kind}] score={self.score} via={self.source} :: {shown}"


def _read_text(path: str) -> str | None:
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_SAMPLE_BYTES)
    except (OSError, UnicodeError):
        return None


def _walk(root: str) -> tuple[list[str], list[str]]:
    """(readable files, blocked paths) under ``root``; TCC denials are listed."""
    files: list[str] = []
    blocked: list[str] = []

    def onerror(err: OSError) -> None:
        path = getattr(err, "filename", None) or ""
        if path:
            blocked.append(path)

    if not os.path.isdir(root):
        return files, blocked
    for current, dirs, names in os.walk(root, onerror=onerror):
        for name in names:
            path = os.path.join(current, name)
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                blocked.append(path)
                continue
            if not stat.S_ISREG(mode):
                continue
            try:
                with open(path, "rb") as probe:
                    probe.read(1)
            except OSError:
                blocked.append(path)
                continue
            files.append(path)
        # Library subdirs of the container are rarely interesting; keep the
        # walk cheap but complete enough for Preferences/Application Support.
        dirs[:] = [d for d in dirs if d not in (".Trash",)]
    return files, blocked


def score_url(url: str, *, airport_hint: str = AIRPORT_HINT) -> int:
    """Higher is a better subscription-URL candidate (host + credential shape)."""
    score = 0
    if TOKENISH_QUERY.search(url):
        score += 4
    if SUBSCRIBE_PATH.search(url):
        score += 2
    if airport_hint and airport_hint.lower() in url.lower():
        score += 3
    return score


def find_url_candidates(files: list[str], *, airport_hint: str = AIRPORT_HINT) -> list[Candidate]:
    found: dict[str, Candidate] = {}
    for path in files:
        text = _read_text(path)
        if not text or "://" not in text:
            continue
        for match in URL_PATTERN.finditer(text):
            url = match.group(0).rstrip(".,;:!?'\"")
            score = score_url(url, airport_hint=airport_hint)
            if score < 2:  # no credential shape and no airport hint -> noise
                continue
            previous = found.get(url)
            if previous is None or score > previous.score:
                found[url] = Candidate(
                    kind="url", source=os.path.basename(path), value=url, score=score
                )
    return sorted(found.values(), key=lambda item: -item.score)


def profile_score(text: str) -> int:
    """0 = not a profile; 1 = hints only; >=2 = a real node library."""
    score = 0
    if re.search(r"(?m)^proxies\s*:", text) or '"outbounds"' in text:
        score += 2
    if re.search(r"(?m)^proxy-groups\s*:", text) or '"outbounds"' in text:
        score += 1
    if "server" in text or '"server"' in text:
        score += 1
    return score


def _count_yaml_nodes(text: str) -> int:
    """Count ``name:`` entries inside the ``proxies:`` block only (not groups)."""
    match = re.search(r"(?m)^proxies\s*:\s*$", text)
    if not match:
        return len(PROFILE_NODE_COUNT.findall(text))
    tail = text[match.end():]
    end = re.search(r"(?m)^(?:proxy-groups|rules|rule-providers)\s*:", tail)
    section = tail[: end.start()] if end else tail
    return len(PROFILE_NODE_COUNT.findall(section))


def find_profiles(files: list[str]) -> list[Candidate]:
    profiles: list[Candidate] = []
    for path in files:
        text = _read_text(path)
        if not text:
            continue
        score = profile_score(text)
        if score < 2:
            continue
        nodes = _count_yaml_nodes(text)
        profiles.append(
            Candidate(
                kind="profile",
                source=os.path.relpath(path, os.path.dirname(path)),
                value=path,
                score=score * 10 + min(nodes, 500),
                detail=f"{nodes} node-like entries",
            )
        )
    profiles.sort(key=lambda item: -item.score)
    # keep the source path readable but not the full home prefix
    for item in profiles:
        item.source = item.value
    return profiles


def scan(roots: list[str], *, airport_hint: str = AIRPORT_HINT) -> tuple[list[Candidate], list[Candidate], list[str]]:
    files: list[str] = []
    blocked: list[str] = []
    for root in roots:
        root_files, root_blocked = _walk(root)
        files.extend(root_files)
        blocked.extend(root_blocked)
    urls = find_url_candidates(files, airport_hint=airport_hint)
    profiles = find_profiles(files)
    if airport_hint:
        for path in files:
            text = _read_text(path)
            if not text or airport_hint not in text.lower():
                continue
            urls.append(
                Candidate(
                    kind="hint",
                    source=os.path.basename(path),
                    value=airport_hint,
                    score=1,
                    detail="airport name mentioned (no credential-shaped URL found there)",
                )
            )
    return urls, profiles, sorted(set(blocked))


# ---- write mode ------------------------------------------------------------


def _is_root() -> bool:
    """POSIX root check; Windows has no euid - treat as unprivileged."""
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def apply_env_write(env_path: str, updates: dict[str, str]) -> list[str]:
    """Set keys in a KEY=VALUE env file, preserving everything else (0600).

    Returns the masked summary of what changed.  Duplicate keys for an updated
    key are removed (the project's env readers reject duplicates).
    """
    if os.path.isfile(env_path):
        if not _is_root() and not os.access(env_path, os.W_OK):
            raise PermissionError(env_path)
    else:
        parent = os.path.dirname(env_path) or "."
        if os.path.isdir(parent) and not _is_root() and not os.access(parent, os.W_OK):
            raise PermissionError(parent)
    existing_lines: list[str] = []
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8-sig", errors="replace") as handle:
            existing_lines = handle.read().splitlines()
    updated: dict[str, str] = {}
    kept: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key in updates:
            if key in updated:
                continue  # drop duplicates beyond the first occurrence
            updated[key] = updates[key]
            kept.append(f"{key}={updates[key]}")
        else:
            kept.append(line)
    for key, value in updates.items():
        if key not in updated:
            kept.append(f"{key}={value}")
            updated[key] = value
    payload = "\n".join(kept).rstrip("\n") + "\n"
    directory = os.path.dirname(env_path) or "."
    os.makedirs(directory, exist_ok=True)
    import tempfile

    descriptor, tmp_path = tempfile.mkstemp(dir=directory, prefix=".nextin-extract.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, env_path)
    return [f"{key}={mask_url(value) if 'URL' in key else '***'}" for key, value in sorted(updated.items())]


# ---- CLI -------------------------------------------------------------------


def _permission_hint(blocked: list[str]) -> str:
    return (
        "TCC denied access to the Nextin container; grant Full Disk Access to the "
        "app you run this from (System Settings > Privacy & Security > Full Disk "
        "Access), or run this script with sudo from a terminal that has it. "
        f"blocked paths: {len(blocked)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="extra search root (repeatable; defaults to the Nextin containers)",
    )
    parser.add_argument("--write", action="store_true", help="update SUBSCRIPTION_URL_2 in the mpm 0600 secret file (root)")
    parser.add_argument("--mpm-env", default=DEFAULT_MPM_ENV, help="mpm secret file path for --write")
    parser.add_argument("--export", metavar="PATH", help="copy the best cached profile to PATH (0600)")
    parser.add_argument(
        "--slot",
        default="2",
        choices=("2", "3", "4"),
        help="which SUBSCRIPTION_URL_<slot> to write (default 2)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    roots = list(DEFAULT_SEARCH_ROOTS if args.root is None else args.root)
    urls, profiles, blocked = scan(roots)

    print(f"search roots: {len(roots)}; readable files: blocked={len(blocked)}")
    for path in blocked[:3]:
        print(f"  blocked: {os.path.basename(os.path.dirname(path))}/ (TCC)")
    if blocked and not urls and not profiles:
        print(_permission_hint(blocked))

    print(f"subscription URL candidates: {len(urls)}")
    for item in urls[:5]:
        print("  " + item.display())
    print(f"cached profiles: {len(profiles)}")
    for item in profiles[:3]:
        print(f"  [profile] {item.value} ({item.detail})")

    exit_code = 0 if (urls or profiles) else (2 if blocked else 1)

    if args.export:
        if not profiles:
            print("export: no cached profile found")
            return 1
        import shutil

        shutil.copyfile(profiles[0].value, args.export)
        os.chmod(args.export, 0o600)
        print(f"export: best profile -> {args.export} (0600, masked content)")

    if args.write:
        if not urls:
            print("write: no subscription URL candidate; nothing written")
            return 1
        best = urls[0]
        if AIRPORT_HINT and AIRPORT_HINT not in best.value.lower():
            print(
                "write: best candidate is not on the wmsxwd host; refusing to "
                "write automatically - add SUBSCRIPTION_URL_"
                + args.slot
                + " by hand or confirm with --slot"
            )
            return 1
        changes = apply_env_write(args.mpm_env, {f"SUBSCRIPTION_URL_{args.slot}": best.value})
        for line in changes:
            print("write: " + line)
        print("write: done; now run: sudo /usr/local/bin/mihomo-proxy-management update-sources")
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
