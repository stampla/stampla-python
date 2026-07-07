"""Tests for the filename grammar."""

from __future__ import annotations

from datetime import datetime

import pytest

from stampla.naming import Grammar, ParsedName
from stampla.pattern import DEFAULT_PATTERN, NamingPattern

GRAMMAR = Grammar(patterns=(DEFAULT_PATTERN,))


def parsed(filename: str) -> ParsedName:
    result = GRAMMAR.parse(filename)
    assert result is not None, f"expected {filename!r} to parse"
    return result


class TestParse:
    @pytest.mark.parametrize(
        ("filename", "suffix", "raw_ext", "ext"),
        [
            # Master RAW and its DAM sidecar
            ("20260214_125556_1355acb2.nef", "", None, "nef"),
            ("20260214_125556_1355acb2.xmp", "", None, "xmp"),
            # Append-style sidecars keeping the master's extension
            ("20260214_125556_1355acb2.nef.xmp", "", "nef", "xmp"),
            ("20220523_192742_d3147a94.nef.nksc", "", "nef", "nksc"),
            ("20170401_185236_c9e80f84.rw2.pp3", "", "rw2", "pp3"),
            # Editor derivatives with a label suffix
            ("20220401_220820_03afe50f-Edit.tif", "-Edit", None, "tif"),
            ("20240406_154315_b563e2c2-Enhanced-NR-Edit.tif", "-Enhanced-NR-Edit", None, "tif"),
            ("20221231_192158_abbc654f-Enhanced-NR.dng", "-Enhanced-NR", None, "dng"),
            # Sidecar of a suffixed derivative
            ("20170401_185139_4aac33e0_pr.dng.pp3", "_pr", "dng", "pp3"),
            ("20250221_171634_b8ea3318-Enhanced-NR.dng.xmp", "-Enhanced-NR", "dng", "xmp"),
            ("20250329_132258_20d3d649_dxo-Enhanced-SR.dng.xmp", "_dxo-Enhanced-SR", "dng", "xmp"),
            # Video
            ("20210808_145653_941930e9.braw", "", None, "braw"),
            ("20260203_140840_c4d2affb.r3d", "", None, "r3d"),
        ],
    )
    def test_canonical_names(
        self, filename: str, suffix: str, raw_ext: str | None, ext: str
    ) -> None:
        name = parsed(filename)
        assert (name.suffix, name.raw_ext, name.ext) == (suffix, raw_ext, ext)
        assert name.rebuild() == filename

    def test_prefix_and_derived_values(self) -> None:
        name = parsed("20260214_125556_1355acb2.nef.xmp")
        assert name.prefix == "20260214_125556_1355acb2"
        assert name.captured_at == datetime(2026, 2, 14, 12, 55, 56)
        assert name.digest_slice == "1355acb2"
        assert name.pattern is DEFAULT_PATTERN

    @pytest.mark.parametrize(
        "filename",
        [
            "20230815_122948_fce9dc84(1).fp3",  # stray copy marker between prefix and extension
            "20230815_122948_fce9dc84(1).FP3",  # same, with an uppercase extension
            "20180424_131902_4c497be4.rw2.8se.spd",  # two chained raw extensions
            "20260214_125556_1355acb2.NEF",  # uppercase extension is not canonical
            "20260214_125556_1355acb2",  # no extension
            "20260214_125556_1355acb2.",  # empty extension
        ],
    )
    def test_malformed_names(self, filename: str) -> None:
        assert GRAMMAR.parse(filename) is None
        assert GRAMMAR.looks_named(filename) is True

    @pytest.mark.parametrize(
        "filename",
        [
            "md5.txt",
            "generate_md5.sh",
            "DSC_1234.NEF",
            "IMG_5678.jpg",
            "20130310-20130310_172613_3577e7ff.dng",  # junk prepended before the prefix
            "x20260214_125556_1355acb2.nef",
        ],
    )
    def test_unnamed_files(self, filename: str) -> None:
        assert GRAMMAR.parse(filename) is None
        assert GRAMMAR.looks_named(filename) is False


class TestRebuild:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("20260214_125556_1355acb2.nef", "20300101_000000_deadbeef.nef"),
            ("20260214_125556_1355acb2.nef.xmp", "20300101_000000_deadbeef.nef.xmp"),
            (
                "20240406_154315_b563e2c2-Enhanced-NR-Edit.tif",
                "20300101_000000_deadbeef-Enhanced-NR-Edit.tif",
            ),
            ("20170401_185139_4aac33e0_pr.dng.pp3", "20300101_000000_deadbeef_pr.dng.pp3"),
        ],
    )
    def test_prefix_swap_preserves_everything_else(self, filename: str, expected: str) -> None:
        assert parsed(filename).rebuild("20300101_000000_deadbeef") == expected


class TestMultiplePatterns:
    def test_legacy_pattern_recognized_alongside_current(self) -> None:
        current = NamingPattern(name="sha256-12", digest="sha256", digest_length=12)
        grammar = Grammar(patterns=(current, DEFAULT_PATTERN))

        modern = grammar.parse("20260214_125556_1355acb2beef.nef")
        legacy = grammar.parse("20260214_125556_1355acb2.nef")

        assert modern is not None
        assert modern.pattern is current
        assert legacy is not None
        assert legacy.pattern is DEFAULT_PATTERN

    def test_short_hash_does_not_half_match_long_pattern(self) -> None:
        current = NamingPattern(name="sha256-12", digest="sha256", digest_length=12)
        grammar = Grammar(patterns=(current,))
        assert grammar.parse("20260214_125556_1355acb2.nef") is None


class TestGrammarValidation:
    def test_requires_at_least_one_pattern(self) -> None:
        with pytest.raises(ValueError, match="pattern"):
            Grammar(patterns=())

    def test_rejects_invalid_raw_extension(self) -> None:
        with pytest.raises(ValueError, match="raw extension"):
            Grammar(patterns=(DEFAULT_PATTERN,), raw_extensions=frozenset({"NEF"}))

    def test_tiff_and_tif_disambiguate(self) -> None:
        name = parsed("20260214_125556_1355acb2.tiff.xmp")
        assert (name.raw_ext, name.ext) == ("tiff", "xmp")
        plain = parsed("20260214_125556_1355acb2.tiff")
        assert (plain.raw_ext, plain.ext) == (None, "tiff")
