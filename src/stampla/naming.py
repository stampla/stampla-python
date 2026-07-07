"""Filename grammar: parsing and rebuilding canonical names.

A canonical name decomposes as ``<prefix>[<suffix>][.<raw_ext>].<ext>``:

- ``prefix`` — the pattern-defined identity, e.g. ``20260214_125556_1355acb2``
- ``suffix`` — optional tool or human label on a derivative, starting with
  ``-`` or ``_``, e.g. ``-Edit``, ``-Enhanced-NR``, ``_pr``
- ``raw_ext`` — optional extension of the master a sidecar belongs to, for
  tools that append rather than replace (``.nef.xmp``, ``.rw2.pp3``)
- ``ext`` — the file's own extension

Only the prefix ever changes when a file is renamed; suffix, raw extension
and extension are always preserved. All files sharing a prefix form a family
and are renamed together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property

from stampla.pattern import NamingPattern

DEFAULT_RAW_EXTENSIONS = frozenset({"nef", "cr2", "cr3", "raf", "rw2", "dng", "arw", "tif", "tiff"})


@dataclass(frozen=True)
class ParsedName:
    """A canonical filename decomposed into its grammar parts."""

    prefix: str
    suffix: str
    raw_ext: str | None
    ext: str
    pattern: NamingPattern

    @property
    def captured_at(self) -> datetime:
        return self.pattern.datetime_of(self.prefix)

    @property
    def digest_slice(self) -> str:
        return self.pattern.digest_of(self.prefix)

    def rebuild(self, new_prefix: str | None = None) -> str:
        """Reassemble the filename, optionally with a different prefix."""
        prefix = self.prefix if new_prefix is None else new_prefix
        raw = f".{self.raw_ext}" if self.raw_ext else ""
        return f"{prefix}{self.suffix}{raw}.{self.ext}"


@dataclass(frozen=True)
class Grammar:
    """Recognizes canonical names under one or more patterns.

    Patterns are tried in order; list the current pattern first and legacy
    patterns after it.
    """

    patterns: tuple[NamingPattern, ...]
    raw_extensions: frozenset[str] = DEFAULT_RAW_EXTENSIONS

    def __post_init__(self) -> None:
        if not self.patterns:
            raise ValueError("at least one naming pattern is required")
        for extension in self.raw_extensions:
            if not re.fullmatch(r"[a-z0-9]+", extension):
                raise ValueError(f"invalid raw extension {extension!r}")

    @cached_property
    def _tail_regex(self) -> re.Pattern[str]:
        raw_alternatives = "|".join(sorted(self.raw_extensions))
        return re.compile(
            rf"(?P<suffix>[-_][^.]*)?(?:\.(?P<raw_ext>{raw_alternatives}))?\.(?P<ext>[a-z0-9]+)"
        )

    def parse(self, filename: str) -> ParsedName | None:
        """Decompose a canonical filename; ``None`` if it is not one.

        A ``None`` for a name where :meth:`looks_named` is true means the
        name starts like a canonical prefix but violates the grammar —
        worth reporting as malformed rather than merely unnamed.
        """
        for pattern in self.patterns:
            prefix_match = pattern.prefix_regex.match(filename)
            if prefix_match is None:
                continue
            tail_match = self._tail_regex.fullmatch(filename, prefix_match.end())
            if tail_match is None:
                continue
            return ParsedName(
                prefix=prefix_match.group(0),
                suffix=tail_match.group("suffix") or "",
                raw_ext=tail_match.group("raw_ext"),
                ext=tail_match.group("ext"),
                pattern=pattern,
            )
        return None

    def looks_named(self, filename: str) -> bool:
        """Whether the filename starts with any known pattern's prefix."""
        return any(pattern.prefix_regex.match(filename) for pattern in self.patterns)
