#!/usr/bin/env python3
"""Group-reference validator for mihomo/Clash.Meta config.yaml.

Every member listed under ``proxy-groups[].proxies[]`` must resolve to one of:
  * a leaf proxy name declared under the top-level ``proxies[].name``,
  * another ``proxy-groups[].name``,
  * a proxy-provider node name (from a provider cache file, when present), or
  * a mihomo built-in outbound policy (DIRECT / REJECT / COMPATIBLE / PASS).

Matching is EXACT (byte-for-byte). This is deliberate: near-miss variants such
as ``日本 06-×1`` vs ``日本06-×1`` (a stray space, or ``×`` vs ``x``) are real
dangling references that crash mihomo at load time, and must NOT be normalised
away.

Standalone usage::

    python check_group_refs.py path/to/config.yaml [--json]

Exit code 0 = all references resolve; 1 = dangling references found (they are
printed, one per line, as ``<group>\\t<missing member>``); 2 = usage/parse error.

The module is import-friendly: ``find_missing_group_refs(config, ...)`` returns
the list of problems so it can be unit-tested without touching the filesystem.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# mihomo built-in outbound policies that are always valid group members.
BUILTIN_POLICIES: Set[str] = {"DIRECT", "REJECT", "COMPATIBLE", "PASS"}


def _names_from_proxies(node: Any) -> Set[str]:
    """Collect ``name`` fields from a mihomo ``proxies:`` list."""
    names: Set[str] = set()
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                nm = item.get("name")
                if isinstance(nm, str):
                    names.add(nm)
            elif isinstance(item, str):
                names.add(item)
    return names


def _load_provider_names(
    config: Dict[str, Any], config_dir: Optional[str]
) -> Set[str]:
    """Best-effort: read proxy-provider cache files to learn their node names.

    A group that pulls members from a proxy-provider (via ``use:``) can list
    provider-supplied node names in its ``proxies:``. Those names are only
    valid at runtime, so we opportunistically read the provider's cached YAML
    (its ``path:``) when it exists to avoid false-positive dangling reports.
    Missing/unparseable cache files are simply ignored.
    """
    names: Set[str] = set()
    providers = config.get("proxy-providers")
    if not isinstance(providers, dict):
        return names
    for prov in providers.values():
        if not isinstance(prov, dict):
            continue
        path = prov.get("path")
        if not isinstance(path, str) or not path:
            continue
        full = path
        if not os.path.isabs(full):
            full = os.path.join(config_dir or ".", path)
        if not os.path.isfile(full):
            continue
        try:
            import yaml  # local import so the module loads without pyyaml

            with io.open(full, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception:
            continue
        if isinstance(data, dict):
            names |= _names_from_proxies(data.get("proxies"))
    return names


def collect_valid_names(
    config: Dict[str, Any], config_dir: Optional[str] = None
) -> Set[str]:
    """All names that a ``proxy-groups[].proxies[]`` member may legitimately be."""
    valid: Set[str] = set(BUILTIN_POLICIES)
    valid |= _names_from_proxies(config.get("proxies"))
    for grp in config.get("proxy-groups") or []:
        if isinstance(grp, dict):
            nm = grp.get("name")
            if isinstance(nm, str):
                valid.add(nm)
    valid |= _load_provider_names(config, config_dir)
    return valid


def find_missing_group_refs(
    config: Dict[str, Any], config_dir: Optional[str] = None
) -> List[Tuple[str, str]]:
    """Return ``[(group_name, missing_member), ...]`` for unresolvable members.

    Group members may be plain strings or ``{name: ...}`` dicts; both forms are
    handled. Comparison against the valid-name set is exact.
    """
    valid = collect_valid_names(config, config_dir)
    problems: List[Tuple[str, str]] = []
    for grp in config.get("proxy-groups") or []:
        if not isinstance(grp, dict):
            continue
        gname = grp.get("name")
        gname = gname if isinstance(gname, str) else "<unnamed>"
        members: Iterable[Any] = grp.get("proxies") or []
        for member in members:
            if isinstance(member, dict):
                mname = member.get("name")
            else:
                mname = member
            if not isinstance(mname, str):
                continue
            if mname not in valid:
                problems.append((gname, mname))
    return problems


def load_config(path: str) -> Dict[str, Any]:
    import yaml

    with io.open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config root is not a mapping")
    return data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate proxy-groups member references in a mihomo config."
    )
    parser.add_argument("config", help="path to config.yaml")
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of TSV lines"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except Exception as exc:  # parse / IO error
        print("ERROR: cannot parse %s: %s" % (args.config, exc), file=sys.stderr)
        return 2

    config_dir = os.path.dirname(os.path.abspath(args.config))
    missing = find_missing_group_refs(config, config_dir)

    if args.json:
        print(
            json.dumps(
                {
                    "config": args.config,
                    "ok": not missing,
                    "missing": [{"group": g, "member": m} for g, m in missing],
                },
                ensure_ascii=False,
            )
        )
    else:
        for g, m in missing:
            print("%s\t%s" % (g, m))
        if not missing:
            print("OK: all proxy-groups references resolve")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
