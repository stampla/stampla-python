"""Where a named file belongs: tree layouts applied to name-derived dates.

The canonical name carries the capture time, and a tree's layout maps a
capture time to a directory — so the correct shelf for every named file
is derivable from the name alone. The one exception is the ``{shoot}``
token: a shoot is chosen at import and recorded nowhere else, so a
shoot segment matches any directory name and can never be derived back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stampla.group import Group


@dataclass(frozen=True)
class ExpectedDir:
    """A layout rendered for one capture time; shoot segments are wildcards."""

    #: one entry per path segment; None means "any name" (a {shoot} segment)
    segments: tuple[str | None, ...]

    @property
    def derivable(self) -> bool:
        """True when the exact directory is known (no shoot wildcard)."""
        return all(segment is not None for segment in self.segments)

    def matches(self, relative_dir: PurePosixPath | Path) -> bool:
        parts = relative_dir.parts
        if parts == (".",):
            parts = ()
        if len(parts) != len(self.segments):
            return False
        return all(
            expected is None or actual == expected
            for expected, actual in zip(self.segments, parts, strict=True)
        )

    def path(self) -> PurePosixPath:
        """The exact directory; only meaningful when ``derivable``."""
        return PurePosixPath(*(segment or "*" for segment in self.segments))

    def __str__(self) -> str:
        return "/".join(segment if segment is not None else "…" for segment in self.segments)


def expected_dir(layout: str, moment: datetime) -> ExpectedDir:
    """Render a tree layout for a capture time, shoot segments wild."""
    rendered = (
        layout.replace("{yyyy}", f"{moment.year:04d}")
        .replace("{mm}", f"{moment.month:02d}")
        .replace("{dd}", f"{moment.day:02d}")
    )
    return ExpectedDir(
        tuple(
            None if "{shoot}" in segment else segment for segment in rendered.split("/") if segment
        )
    )


def group_placement(
    group: Group, layout: str, tree_root: Path
) -> tuple[Path, ExpectedDir, Path] | None:
    """(home path, expected dir, actual dir relative to the tree) or None.

    Subdirectory sidecars sit one level below their master, so the
    group's home is its shallowest parsed member's directory.
    """
    home = min(
        (file for file in group.members if file.parsed is not None),
        key=lambda file: len(file.path.parts),
        default=None,
    )
    if home is None or home.parsed is None:
        return None
    try:
        moment = home.parsed.pattern.datetime_of(group.prefix)
    except ValueError:
        return None  # an impossible date in the name; the date checks own that story
    return home.path, expected_dir(layout, moment), home.path.parent.relative_to(tree_root)
