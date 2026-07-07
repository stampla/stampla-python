"""Walking archive trees and classifying what lives there.

Every regular file is classified against the grammar: canonically *named*,
*malformed* (starts like a canonical prefix but breaks the grammar), or
*unnamed*. Hidden files (dotfiles, AppleDouble ``._*`` companions,
``.DS_Store``) are skipped outright, as are paths matching the configured
exclude globs. Globs use fnmatch semantics where ``*`` crosses directory
separators, so ``**/CaptureOne/**`` and ``*.cot`` behave as expected; a
matching directory is pruned without descending into it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path

from stampla.naming import Grammar, ParsedName


class FileStatus(Enum):
    NAMED = "named"
    MALFORMED = "malformed"
    UNNAMED = "unnamed"


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    status: FileStatus
    parsed: ParsedName | None = None


def scan_tree(root: Path, grammar: Grammar, excludes: Sequence[str] = ()) -> Iterator[ScannedFile]:
    """Yield every relevant file under ``root``, deterministically ordered."""
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        rel_dir = directory.relative_to(root)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".")
            and not _dir_excluded((rel_dir / name).as_posix(), name, excludes)
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if _excluded((rel_dir / name).as_posix(), name, excludes):
                continue
            parsed = grammar.parse(name)
            if parsed is not None:
                status = FileStatus.NAMED
            elif grammar.looks_named(name):
                status = FileStatus.MALFORMED
            else:
                status = FileStatus.UNNAMED
            yield ScannedFile(path=directory / name, status=status, parsed=parsed)


def _excluded(relative: str, name: str, excludes: Sequence[str]) -> bool:
    return any(fnmatchcase(relative, pattern) or fnmatchcase(name, pattern) for pattern in excludes)


def _dir_excluded(relative: str, name: str, excludes: Sequence[str]) -> bool:
    # "Tether/**" and "**/CaptureOne/**" should prune the directory itself,
    # hence the trailing-slash form is also tried.
    return any(
        fnmatchcase(relative, pattern)
        or fnmatchcase(relative + "/", pattern)
        or fnmatchcase(name, pattern)
        for pattern in excludes
    )
