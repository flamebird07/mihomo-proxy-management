"""Production policy: region filters and the "no matching node" verdicts.

Pure functions over a list of provider node names - no IO, no platform code.
The macOS renderer/installer must call :func:`enforce` before a config is
installed:

* ``CURSOR-US`` + ``CURSOR-JP`` matching **zero** nodes -> error (fail closed):
  CURSOR has no DIRECT fallback, so an empty pool would silently drop traffic.
* a ``CODEBUDDY-*`` region subgroup with no match -> warning only: CODEBUDDY
  lists DIRECT first and is therefore still usable.

The filter expressions here are the single source of truth; the template must
carry exactly these strings (a structural test compares them).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .errors import FailClosed

# Region -> (group name, filter expression).  The expressions come from the
# network-ops ruling and must not be "improved" locally.
CURSOR_REGIONS: dict[str, tuple[str, str]] = {
    "US": ("CURSOR-US", r"(?i)美国|美國|\bUS\b|USA|United States|🇺🇸"),
    "JP": ("CURSOR-JP", r"(?i)日本|\bJP\b|Japan|🇯🇵"),
}

CODEBUDDY_REGIONS: dict[str, tuple[str, str]] = {
    "HK": ("CODEBUDDY-HK", r"(?i)香港|\bHK\b|Hong ?Kong|🇭🇰"),
    "TW": ("CODEBUDDY-TW", r"(?i)台湾|台灣|\bTW\b|Taiwan|🇹🇼"),
    "SG": ("CODEBUDDY-SG", r"(?i)新加坡|\bSG\b|Singapore|🇸🇬"),
    "JP": ("CODEBUDDY-JP", r"(?i)日本|\bJP\b|Japan|🇯🇵"),
}

# Group name -> filter expression (template drift check).
GROUP_FILTERS: dict[str, str] = {
    group: pattern for group, pattern in CURSOR_REGIONS.values()
}
GROUP_FILTERS.update({group: pattern for group, pattern in CODEBUDDY_REGIONS.values()})

CURSOR_GROUP = "CURSOR"
CODEBUDDY_GROUP = "CODEBUDDY"


@dataclass
class PolicyReport:
    cursor: dict[str, int] = field(default_factory=dict)
    codebuddy: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def cursor_total(self) -> int:
        return sum(self.cursor.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "cursor": dict(self.cursor),
            "codebuddy": dict(self.codebuddy),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def matches(name: str, pattern: str) -> bool:
    """True when ``name`` matches ``pattern`` (patterns carry their own ``(?i)``)."""
    try:
        return re.search(pattern, name) is not None
    except re.error as exc:  # pragma: no cover - constants are validated below
        raise FailClosed(f"invalid region filter: {exc}") from exc


def count_matches(names: Iterable[str], pattern: str) -> int:
    return sum(1 for name in names if matches(name, pattern))


def evaluate(names: Iterable[str]) -> PolicyReport:
    """Count region matches; never raises, so callers can report first."""
    pool = [str(name) for name in names if name]
    report = PolicyReport()
    for region, (_group, pattern) in CURSOR_REGIONS.items():
        report.cursor[region] = count_matches(pool, pattern)
    for region, (group, pattern) in CODEBUDDY_REGIONS.items():
        report.codebuddy[region] = count_matches(pool, pattern)
        if report.codebuddy[region] == 0:
            report.warnings.append(
                f"{group} has no matching node; CODEBUDDY falls back to DIRECT"
            )
    if report.cursor_total == 0:
        groups = ", ".join(group for group, _pattern in CURSOR_REGIONS.values())
        report.errors.append(
            f"{groups} match no provider node; CURSOR has no DIRECT fallback and would "
            f"drop traffic (fail closed)"
        )
    return report


def enforce(names: Iterable[str]) -> PolicyReport:
    """Like :func:`evaluate`, but a CURSOR pool with zero matches is fatal."""
    report = evaluate(names)
    if report.errors:
        raise FailClosed("; ".join(report.errors))
    return report


def expected_filters() -> Mapping[str, str]:
    """Group name -> filter expression, for template consistency checks."""
    return dict(GROUP_FILTERS)


__all__ = [
    "CODEBUDDY_GROUP",
    "CODEBUDDY_REGIONS",
    "CURSOR_GROUP",
    "CURSOR_REGIONS",
    "GROUP_FILTERS",
    "PolicyReport",
    "count_matches",
    "enforce",
    "evaluate",
    "expected_filters",
    "matches",
]
