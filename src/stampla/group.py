"""Grouping files into groups that are renamed as one unit.

Two grouping regimes exist:

- **Named files** group by their name prefix. Prefixes embed a content
  hash, so they are unique per master across the whole archive — sidecars
  kept in subdirectories (``NKSC_PARAM/<master>.nksc``) fall into their
  master's group with no directory logic at all.

- **Card originals** (files not yet named) group by directory and original
  base name: ``DSC1234.NEF``, ``DSC1234.xmp`` and ``DSC1234.NEF.xmp``
  share the base ``DSC1234``. Sidecar-directory rules map files like
  ``NKSC_PARAM/DSC1234.NEF.nksc`` to the master's directory first. A group
  whose base extends another group's base with a ``-`` or ``_`` label
  (``DSC1234-Edit``) is merged into that group only when the shorter base
  owns a master and the labeled group does not — a labeled group with its
  own master file is a separate photo, not a derivative, so ``IMG_01.NEF``
  never merges into ``IMG.NEF``. Callers should pass camera-native master
  extensions here (not ``tif``), so editor output like ``…-Edit.tif``
  counts as a derivative rather than a master.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from stampla.config import SidecarDirRule
from stampla.scan import ScannedFile


@dataclass(frozen=True)
class Group:
    """All named files sharing one prefix."""

    prefix: str
    members: tuple[ScannedFile, ...]

    def master_candidates(self, master_extensions: frozenset[str]) -> tuple[ScannedFile, ...]:
        return tuple(
            member
            for member in self.members
            if member.parsed is not None
            and member.parsed.suffix == ""
            and member.parsed.raw_ext is None
            and member.parsed.ext in master_extensions
        )

    def master(self, master_extensions: frozenset[str]) -> ScannedFile | None:
        """The unique hash-carrying member, or ``None`` if absent/ambiguous."""
        candidates = self.master_candidates(master_extensions)
        return candidates[0] if len(candidates) == 1 else None


def group_by_prefix(files: Iterable[ScannedFile]) -> list[Group]:
    """Group named files into groups; unnamed/malformed files are ignored."""
    by_prefix: dict[str, list[ScannedFile]] = defaultdict(list)
    for file in files:
        if file.parsed is not None:
            by_prefix[file.parsed.prefix].append(file)
    return [
        Group(prefix=prefix, members=tuple(members))
        for prefix, members in sorted(by_prefix.items())
    ]


@dataclass(frozen=True)
class CardGroup:
    """Files sharing one original (pre-import) base name in one directory."""

    directory: Path
    base: str
    members: tuple[Path, ...]


def group_originals(
    paths: Iterable[Path],
    sidecar_dirs: Sequence[SidecarDirRule] = (),
    master_extensions: frozenset[str] = frozenset(),
) -> list[CardGroup]:
    """Group not-yet-named files by directory and original base name."""
    groups: dict[tuple[Path, str], list[Path]] = defaultdict(list)
    for path in paths:
        directory, base = _home_and_base(path, sidecar_dirs)
        groups[(directory, base)].append(path)

    merged: dict[tuple[Path, str], list[Path]] = {}
    for key in sorted(groups, key=lambda item: (str(item[0]), item[1])):
        directory, base = key
        parent = None
        if not _has_master(groups[key], master_extensions):
            parent = _labeled_parent(base, directory, groups, master_extensions)
        target = (directory, parent) if parent is not None else key
        merged.setdefault(target, []).extend(groups[key])

    return [
        CardGroup(directory=directory, base=base, members=tuple(sorted(members)))
        for (directory, base), members in sorted(
            merged.items(), key=lambda item: (str(item[0][0]), item[0][1])
        )
    ]


def _home_and_base(path: Path, sidecar_dirs: Sequence[SidecarDirRule]) -> tuple[Path, str]:
    """The directory and base a file belongs to, resolving sidecar subdirs."""
    directory = path.parent
    name = path.name
    for rule in sidecar_dirs:
        if directory.name == rule.subdir and name.lower().endswith(rule.strip.lower()):
            name = name[: -len(rule.strip)]
            directory = directory.parent
            break
    return directory, name.split(".", 1)[0]


def _labeled_parent(
    base: str,
    directory: Path,
    groups: dict[tuple[Path, str], list[Path]],
    master_extensions: frozenset[str],
) -> str | None:
    """The longest shorter base that ``base`` extends with a -/_ label."""
    best: str | None = None
    for candidate_directory, candidate in groups:
        if candidate_directory != directory or candidate == base:
            continue
        if not base.startswith(candidate) or base[len(candidate)] not in "-_":
            continue
        if not _has_master(groups[(candidate_directory, candidate)], master_extensions):
            continue
        if best is None or len(candidate) > len(best):
            best = candidate
    return best


def _has_master(paths: list[Path], master_extensions: frozenset[str]) -> bool:
    return any(path.suffix.lstrip(".").lower() in master_extensions for path in paths)
