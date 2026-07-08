"""Capture-time resolution from metadata tags.

A file's capture time is resolved by trying an ordered chain of tag names
against the group-qualified metadata ExifTool returned. The first entry
that yields a complete, plausible timestamp wins. A file for which no
entry matches is *unresolved* — reported, never guessed at, and never
given a partial date.

Chain entries come in two forms:

- ``Group:Tag`` matches exactly that group.
- ``Tag`` matches the tag in any group, *except* groups that some entry
  of the chain names explicitly for the same tag. This encodes rankings
  such as "any CreateDate, but QuickTime's only as a last resort": list
  ``CreateDate`` first and ``QuickTime:CreateDate`` later. QuickTime
  timestamps are usually UTC while maker-notes ones are local wall-clock,
  so the local source must win when both are present.

An entry suffixed ``@utc`` (e.g. ``QuickTime:CreateDate@utc``) declares
that the tag stores UTC: its value is converted, DST-aware, into the
configured timezone, and the resolution's source carries the marker so
reports always show that a conversion happened. Do not mark sources
that store local time (BRAW's QuickTime atoms do).

All resolved values are naive local wall-clock time, as a person at the
scene would have read off a watch. Timezone suffixes in metadata values
are deliberately ignored; the wall-clock part is the identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

_DATETIME_RE = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


@dataclass(frozen=True)
class ResolvedDate:
    """A successfully resolved capture time and the tag it came from."""

    value: datetime
    source: str


@dataclass(frozen=True)
class UnresolvedDate:
    """No chain entry yielded a usable capture time."""

    reason: str


def parse_exiftool_datetime(value: object) -> datetime | None:
    """Parse ExifTool's ``YYYY:mm:dd HH:MM:SS[.frac][±HH:MM]`` format.

    Subseconds and timezone suffixes are dropped. Returns ``None`` for
    anything incomplete or implausible (zero dates, bare dates, garbage);
    a partial date must never become part of a filename.
    """
    match = _DATETIME_RE.match(str(value))
    if match is None:
        return None
    try:
        return datetime(*(int(part) for part in match.groups()))  # type: ignore[arg-type]
    except ValueError:
        return None


def resolve_date(
    tags: Mapping[str, object],
    chain: Sequence[str],
    zone: tzinfo | None = None,
) -> ResolvedDate | UnresolvedDate:
    """Resolve a capture time from group-qualified tags using a chain."""
    reserved_groups: dict[str, set[str]] = {}
    for raw_entry in chain:
        entry, _ = _split_utc_marker(raw_entry)
        if ":" in entry:
            group, _, tag = entry.partition(":")
            reserved_groups.setdefault(tag, set()).add(group)

    for raw_entry in chain:
        entry, is_utc = _split_utc_marker(raw_entry)
        if is_utc and zone is None:
            raise ValueError(f"chain entry {raw_entry!r} needs a configured timezone")
        if ":" in entry:
            parsed = parse_exiftool_datetime(tags.get(entry, ""))
            if parsed is not None:
                if is_utc:
                    assert zone is not None
                    return ResolvedDate(value=utc_to_wall_clock(parsed, zone), source=raw_entry)
                return ResolvedDate(value=parsed, source=entry)
        else:
            deferred = reserved_groups.get(entry, set())
            for key, value in tags.items():
                group, _, tag = key.partition(":")
                if tag == entry and group not in deferred:
                    parsed = parse_exiftool_datetime(value)
                    if parsed is not None:
                        if is_utc:
                            assert zone is not None
                            return ResolvedDate(
                                value=utc_to_wall_clock(parsed, zone), source=f"{key}@utc"
                            )
                        return ResolvedDate(value=parsed, source=key)

    if not tags:
        return UnresolvedDate(reason="no metadata tags present")
    return UnresolvedDate(reason="no usable capture time among: " + ", ".join(sorted(tags)))


def chain_tags(chain: Sequence[str]) -> set[str]:
    """The bare tag names a chain needs from ExifTool (markers stripped)."""
    tags = set()
    for raw_entry in chain:
        entry, _ = _split_utc_marker(raw_entry)
        tags.add(entry.partition(":")[2] or entry)
    return tags


def _split_utc_marker(entry: str) -> tuple[str, bool]:
    if entry.endswith("@utc"):
        return entry[: -len("@utc")], True
    return entry, False


def utc_to_wall_clock(value: datetime, zone: tzinfo) -> datetime:
    """Convert a naive UTC timestamp to naive local wall-clock time.

    For sources that only store UTC (e.g. phone videos), DST-aware.
    """
    return value.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
