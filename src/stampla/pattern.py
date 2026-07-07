"""Naming patterns: how a canonical name prefix is built and recognized.

A pattern turns a capture time and a content digest into a name prefix such
as ``20260703_150727_9b677b64`` and recognizes such prefixes in existing
names. Several patterns can be active at once — the current one plus any
additional recognized ones — so files can be classified consistently
while an archive migrates between schemes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property

# IPTC's TransmissionReference field, used as a rename token by DAM
# integrations, is limited to 32 characters. Staying a character below
# keeps every prefix safely inside that budget.
MAX_PREFIX_LENGTH = 31

MIN_DIGEST_LENGTH = 4

_DATETIME_TOKENS = {
    "%Y": (r"\d{4}", 4),
    "%m": (r"\d{2}", 2),
    "%d": (r"\d{2}", 2),
    "%H": (r"\d{2}", 2),
    "%M": (r"\d{2}", 2),
    "%S": (r"\d{2}", 2),
}


class PatternError(ValueError):
    """Invalid naming pattern definition."""


def _compile_datetime_format(fmt: str) -> tuple[str, int]:
    """Translate a strftime format into a regex and its fixed width."""
    parts: list[str] = []
    width = 0
    i = 0
    while i < len(fmt):
        if fmt[i] == "%":
            token = fmt[i : i + 2]
            if token not in _DATETIME_TOKENS:
                raise PatternError(f"unsupported datetime token {token!r} in format {fmt!r}")
            regex, token_width = _DATETIME_TOKENS[token]
            parts.append(regex)
            width += token_width
            i += 2
        else:
            parts.append(re.escape(fmt[i]))
            width += 1
            i += 1
    return "".join(parts), width


#: digests ExifTool can compute over image data only
_IMAGE_HASH_DIGESTS = frozenset({"md5", "sha256", "sha512"})


@dataclass(frozen=True)
class NamingPattern:
    """A single naming scheme: datetime format, digest algorithm and length.

    The default values describe ``YYYYMMDD_hhmmss_<md5:8>``.

    ``image_hash`` lists extensions whose digest is computed over the
    image data only (metadata excluded), so that names of formats edited
    in place — keywords, ratings, rename tokens — never drift. All other
    extensions use the whole file. The mapping is part of the pattern's
    identity: changing it changes what every name means, i.e. it defines
    a new pattern and calls for a migration.
    """

    name: str
    datetime_format: str = "%Y%m%d_%H%M%S"
    digest: str = "md5"
    digest_length: int = 8
    separator: str = "_"
    image_hash: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name:
            raise PatternError("pattern name must not be empty")
        for extension in self.image_hash:
            if not re.fullmatch(r"[a-z0-9]+", extension):
                raise PatternError(f"invalid image_hash extension {extension!r}")
        if self.image_hash and self.digest not in _IMAGE_HASH_DIGESTS:
            raise PatternError(
                f"image-data hashing supports {sorted(_IMAGE_HASH_DIGESTS)}, not {self.digest!r}"
            )
        try:
            digest_size = hashlib.new(self.digest).digest_size
        except (ValueError, TypeError) as exc:
            raise PatternError(f"unknown digest algorithm {self.digest!r}") from exc
        hex_length = digest_size * 2
        if not MIN_DIGEST_LENGTH <= self.digest_length <= hex_length:
            raise PatternError(
                f"digest_length must be between {MIN_DIGEST_LENGTH} and {hex_length}"
                f" for {self.digest}, got {self.digest_length}"
            )
        if any(ch in self.separator for ch in "./\\"):
            raise PatternError(f"separator {self.separator!r} contains a reserved character")
        _, datetime_width = _compile_datetime_format(self.datetime_format)
        prefix_length = datetime_width + len(self.separator) + self.digest_length
        if prefix_length > MAX_PREFIX_LENGTH:
            raise PatternError(
                f"prefix would be {prefix_length} characters,"
                f" above the maximum of {MAX_PREFIX_LENGTH}"
            )

    @cached_property
    def datetime_length(self) -> int:
        return _compile_datetime_format(self.datetime_format)[1]

    @cached_property
    def prefix_length(self) -> int:
        return self.datetime_length + len(self.separator) + self.digest_length

    @cached_property
    def prefix_regex(self) -> re.Pattern[str]:
        datetime_regex, _ = _compile_datetime_format(self.datetime_format)
        return re.compile(
            f"{datetime_regex}{re.escape(self.separator)}[0-9a-f]{{{self.digest_length}}}"
        )

    def build_prefix(self, captured_at: datetime, hexdigest: str) -> str:
        """Build a prefix from a capture time and a full content hexdigest."""
        if len(hexdigest) < self.digest_length or not re.fullmatch(r"[0-9a-f]+", hexdigest):
            raise ValueError(f"not a usable lowercase hexdigest: {hexdigest!r}")
        return (
            captured_at.strftime(self.datetime_format)
            + self.separator
            + hexdigest[: self.digest_length]
        )

    def matches_prefix(self, text: str) -> bool:
        return self.prefix_regex.fullmatch(text) is not None

    def datetime_of(self, prefix: str) -> datetime:
        """Extract the capture time encoded in a prefix."""
        return datetime.strptime(prefix[: self.datetime_length], self.datetime_format)

    def digest_of(self, prefix: str) -> str:
        """Extract the digest slice encoded in a prefix."""
        return prefix[self.datetime_length + len(self.separator) :]

    def digest_source_for(self, extension: str) -> str:
        """``image`` or ``file``: what this pattern hashes for the extension."""
        return "image" if extension.lower() in self.image_hash else "file"


DEFAULT_PATTERN = NamingPattern(name="md5-8")
