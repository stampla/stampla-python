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
from pathlib import Path

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


#: synthetic chain source: a timestamp recovered from the filename.
#: Not an ExifTool tag — commands inject it after reading metadata, so
#: a chain can rank it explicitly (usually after every metadata source).
NAME_TIMESTAMP_TAG = "File:NameTimestamp"

# Year-first, sortable timestamps only: YYYY MM DD then HH MM SS,
# each part with one consistent separator out of - . _ (or none),
# joined by at most one of T, space, - or _. Day-first or month-first
# forms (31.12.2016, 12/31/2016) are never interpreted: month and day
# are indistinguishable across locales, and a wrong-but-plausible date
# is worse than none. A trailing 3-digit block right after compact
# seconds is accepted as milliseconds (phone cameras) and ignored.
_NAME_TIMESTAMP = re.compile(
    r"""(?<![0-9])
    (?P<year>19\d{2}|20\d{2}) (?P<dsep>[-._]?)
    (?P<month>0[1-9]|1[0-2]) (?P=dsep)
    (?P<day>0[1-9]|[12]\d|3[01])
    [T\ _-]?
    (?P<hour>[01]\d|2[0-3]) (?P<tsep>[-._]?)
    (?P<minute>[0-5]\d) (?P=tsep)
    (?P<second>[0-5]\d)
    (?P<ms>\d{3})?
    (?![0-9])""",
    re.VERBOSE,
)


def timestamp_from_name(name: str) -> datetime | None:
    """A capture time recovered from a filename, or ``None``.

    Only complete, year-first timestamps qualify — a bare date has no
    time and would fabricate midnight.
    """
    match = _NAME_TIMESTAMP.search(name)
    if match is None:
        return None
    if match.group("ms") and match.group("tsep"):
        return None  # digits after separated seconds are not milliseconds
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
        )
    except ValueError:
        return None


def augment_with_name_timestamps(
    metadata: dict[Path, dict[str, object]], paths: Sequence[Path]
) -> None:
    """Offer each file's name-recovered timestamp as a chain source.

    Injected under ``NAME_TIMESTAMP_TAG``; chains that do not list the
    tag never see it.
    """
    for path in paths:
        value = timestamp_from_name(path.name)
        if value is not None:
            metadata.setdefault(path, {})[NAME_TIMESTAMP_TAG] = value.strftime("%Y:%m:%d %H:%M:%S")


def chain_tags(chain: Sequence[str]) -> set[str]:
    """The bare tag names a chain needs from ExifTool (markers stripped)."""
    tags = set()
    for raw_entry in chain:
        entry, _ = _split_utc_marker(raw_entry)
        if entry == NAME_TIMESTAMP_TAG:
            continue  # synthetic — never queried from ExifTool
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
