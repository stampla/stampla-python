"""Tests for naming patterns."""

from __future__ import annotations

from datetime import datetime

import pytest

from stampla.pattern import DEFAULT_PATTERN, MAX_PREFIX_LENGTH, NamingPattern, PatternError

CAPTURED = datetime(2026, 7, 3, 15, 7, 27)
MD5_FULL = "9b677b64af8a1f4bd3e0ee5c9b011f5a"


class TestBuildPrefix:
    def test_default_pattern(self) -> None:
        assert DEFAULT_PATTERN.build_prefix(CAPTURED, MD5_FULL) == "20260703_150727_9b677b64"

    def test_digest_is_sliced_not_padded(self) -> None:
        pattern = NamingPattern(name="md5-12", digest_length=12)
        assert pattern.build_prefix(CAPTURED, MD5_FULL) == "20260703_150727_9b677b64af8a"

    def test_sha256_pattern(self) -> None:
        pattern = NamingPattern(name="sha256-12", digest="sha256", digest_length=12)
        digest = "a" * 64
        assert pattern.build_prefix(CAPTURED, digest) == "20260703_150727_aaaaaaaaaaaa"

    @pytest.mark.parametrize("bad", ["9B677B64", "9b677g64" + "0" * 24, "abc", ""])
    def test_rejects_unusable_digest(self, bad: str) -> None:
        with pytest.raises(ValueError, match="hexdigest"):
            DEFAULT_PATTERN.build_prefix(CAPTURED, bad)

    def test_zero_padding(self) -> None:
        early = datetime(2012, 1, 2, 3, 4, 5)
        assert DEFAULT_PATTERN.build_prefix(early, MD5_FULL) == "20120102_030405_9b677b64"


class TestParsePrefix:
    def test_roundtrip_datetime(self) -> None:
        prefix = DEFAULT_PATTERN.build_prefix(CAPTURED, MD5_FULL)
        assert DEFAULT_PATTERN.datetime_of(prefix) == CAPTURED

    def test_roundtrip_digest(self) -> None:
        prefix = DEFAULT_PATTERN.build_prefix(CAPTURED, MD5_FULL)
        assert DEFAULT_PATTERN.digest_of(prefix) == MD5_FULL[:8]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("20260703_150727_9b677b64", True),
            ("20260703_150727_9B677B64", False),
            ("20260703_150727_9b677b6", False),
            ("20260703_150727_9b677b649", False),
            ("2026073_150727_9b677b64", False),
            ("20260703-150727_9b677b64", False),
        ],
    )
    def test_matches_prefix(self, text: str, expected: bool) -> None:
        assert DEFAULT_PATTERN.matches_prefix(text) is expected


class TestValidation:
    def test_unknown_digest(self) -> None:
        with pytest.raises(PatternError, match="digest algorithm"):
            NamingPattern(name="bad", digest="crc32")

    def test_digest_length_above_algorithm_size(self) -> None:
        with pytest.raises(PatternError, match="digest_length"):
            NamingPattern(name="bad", digest="md5", digest_length=33)

    def test_digest_length_too_short(self) -> None:
        with pytest.raises(PatternError, match="digest_length"):
            NamingPattern(name="bad", digest_length=3)

    def test_prefix_length_cap(self) -> None:
        with pytest.raises(PatternError, match=str(MAX_PREFIX_LENGTH)):
            NamingPattern(name="bad", digest="sha256", digest_length=16)

    def test_longest_allowed_prefix(self) -> None:
        pattern = NamingPattern(name="sha256-15", digest="sha256", digest_length=15)
        assert pattern.prefix_length == MAX_PREFIX_LENGTH

    def test_unsupported_datetime_token(self) -> None:
        with pytest.raises(PatternError, match="%f"):
            NamingPattern(name="bad", datetime_format="%Y%m%d_%H%M%S%f")

    def test_separator_with_dot(self) -> None:
        with pytest.raises(PatternError, match="separator"):
            NamingPattern(name="bad", separator=".")

    def test_empty_name(self) -> None:
        with pytest.raises(PatternError, match="name"):
            NamingPattern(name="")

    def test_empty_separator_is_allowed(self) -> None:
        pattern = NamingPattern(name="tight", separator="")
        assert pattern.build_prefix(CAPTURED, MD5_FULL) == "20260703_1507279b677b64"
