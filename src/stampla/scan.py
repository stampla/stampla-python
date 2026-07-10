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
from typing import TYPE_CHECKING

from stampla.naming import Grammar, ParsedName

if TYPE_CHECKING:
    from stampla.config import Config, Tree


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


def tree_targets(
    config: Config, root: Path, paths: Sequence[Path]
) -> list[tuple[Tree, Path, Path]]:
    """(tree, tree root, scan root) for every tree a command should walk.

    Without ``paths`` every existing tree is walked whole; with them,
    each path must be a directory inside some configured tree and scopes
    the walk to that subtree.
    """
    targets: list[tuple[Tree, Path, Path]] = []
    for tree in config.trees:
        tree_root = (root / tree.path).resolve()
        if not paths:
            if tree_root.is_dir():
                targets.append((tree, tree_root, tree_root))
            continue
        for path in paths:
            resolved = path.resolve()
            if resolved.is_relative_to(tree_root):
                if not resolved.is_dir():
                    raise ValueError(f"expected a directory, got: {path}")
                targets.append((tree, tree_root, resolved))
    if not targets:
        raise ValueError(
            "no configured tree matches "
            + (", ".join(str(p) for p in paths) if paths else str(root))
        )
    return targets
