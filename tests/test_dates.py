"""Tests for capture-time resolution.

The tag fixtures are real ExifTool ``-a -G0 -j`` output captured from
files of each container type; only the values matter, so they are inlined.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stampla.config import DEFAULT_DATE_CHAIN_PHOTO, DEFAULT_DATE_CHAIN_VIDEO
from stampla.dates import (
    ResolvedDate,
    UnresolvedDate,
    parse_exiftool_datetime,
    resolve_date,
    utc_to_wall_clock,
)

# A Nikon NEF: DateTimeOriginal and CreateDate agree.
NEF_TAGS = {
    "EXIF:DateTimeOriginal": "2024:06:01 13:21:10",
    "XMP:CreateDate": "2024:06:01 13:21:10.03",
    "EXIF:CreateDate": "2024:06:01 13:21:10",
}

# An XMP sidecar: only XMP-group dates exist.
XMP_SIDECAR_TAGS = {
    "XMP:DateTimeOriginal": "2024:06:01 13:21:10.03",
    "XMP:CreateDate": "2024:06:01 13:21:10.03",
    "XMP:DateCreated": "2024:06:01 13:21:10.03",
}

# A BRAW clip: no DateTimeOriginal at all; its QuickTime CreateDate is
# local wall-clock time (unusual for QuickTime, standard for BRAW).
BRAW_TAGS = {
    "QuickTime:CreateDate": "2021:08:08 14:56:53",
}

# A Nikon MOV: maker-notes times are local (16:09), QuickTime is UTC
# (14:09). QuickTime listed first to mimic ExifTool's output order —
# resolution must not be fooled by it.
MOV_TAGS = {
    "QuickTime:CreateDate": "2025:06:15 14:09:10",
    "MakerNotes:DateTimeOriginal": "2025:06:15 16:09:10",
    "MakerNotes:CreateDate": "2025:06:15 16:09:10",
}

# A RED R3D clip: same shape as the MOV.
R3D_TAGS = {
    "MakerNotes:DateTimeOriginal": "2026:02:03 14:08:40",
    "QuickTime:CreateDate": "2026:02:03 13:08:40",
    "MakerNotes:CreateDate": "2026:02:03 14:08:40",
}


class TestPhotoChain:
    def test_nef_resolves_from_datetimeoriginal(self) -> None:
        result = resolve_date(NEF_TAGS, DEFAULT_DATE_CHAIN_PHOTO)
        assert result == ResolvedDate(
            value=datetime(2024, 6, 1, 13, 21, 10), source="EXIF:DateTimeOriginal"
        )

    def test_sidecar_falls_through_to_xmp_datecreated(self) -> None:
        result = resolve_date(XMP_SIDECAR_TAGS, DEFAULT_DATE_CHAIN_PHOTO)
        assert result == ResolvedDate(
            value=datetime(2024, 6, 1, 13, 21, 10), source="XMP:DateCreated"
        )

    def test_qualified_entry_ignores_other_groups(self) -> None:
        # EXIF:DateTimeOriginal must not match the XMP DateTimeOriginal.
        result = resolve_date(XMP_SIDECAR_TAGS, ("EXIF:DateTimeOriginal",))
        assert isinstance(result, UnresolvedDate)


class TestVideoChain:
    def test_braw_uses_quicktime_local_value(self) -> None:
        result = resolve_date(BRAW_TAGS, DEFAULT_DATE_CHAIN_VIDEO)
        assert result == ResolvedDate(
            value=datetime(2021, 8, 8, 14, 56, 53), source="QuickTime:CreateDate"
        )

    def test_mov_prefers_makernotes_local_over_quicktime_utc(self) -> None:
        result = resolve_date(MOV_TAGS, DEFAULT_DATE_CHAIN_VIDEO)
        assert result == ResolvedDate(
            value=datetime(2025, 6, 15, 16, 9, 10), source="MakerNotes:DateTimeOriginal"
        )

    def test_r3d_prefers_makernotes(self) -> None:
        result = resolve_date(R3D_TAGS, DEFAULT_DATE_CHAIN_VIDEO)
        assert result == ResolvedDate(
            value=datetime(2026, 2, 3, 14, 8, 40), source="MakerNotes:DateTimeOriginal"
        )

    def test_unqualified_createdate_defers_quicktime(self) -> None:
        # Without DateTimeOriginal, the unqualified CreateDate entry must
        # skip QuickTime even though it comes first in the mapping.
        tags = {
            "QuickTime:CreateDate": "2025:06:15 14:09:10",
            "MakerNotes:CreateDate": "2025:06:15 16:09:10",
        }
        result = resolve_date(tags, DEFAULT_DATE_CHAIN_VIDEO)
        assert result == ResolvedDate(
            value=datetime(2025, 6, 15, 16, 9, 10), source="MakerNotes:CreateDate"
        )


class TestUnresolved:
    def test_empty_tags(self) -> None:
        result = resolve_date({}, DEFAULT_DATE_CHAIN_PHOTO)
        assert isinstance(result, UnresolvedDate)
        assert "no metadata tags" in result.reason

    def test_unusable_values_are_listed(self) -> None:
        result = resolve_date(
            {"EXIF:DateTimeOriginal": "0000:00:00 00:00:00"}, ("EXIF:DateTimeOriginal",)
        )
        assert isinstance(result, UnresolvedDate)
        assert "EXIF:DateTimeOriginal" in result.reason

    def test_unrelated_tags_do_not_resolve(self) -> None:
        result = resolve_date({"EXIF:Model": "NIKON Z 7_2"}, DEFAULT_DATE_CHAIN_PHOTO)
        assert isinstance(result, UnresolvedDate)


class TestParseExiftoolDatetime:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2024:06:01 13:21:10", datetime(2024, 6, 1, 13, 21, 10)),
            ("2024:06:01 13:21:10.03", datetime(2024, 6, 1, 13, 21, 10)),
            ("2024:06:01 13:21:10+02:00", datetime(2024, 6, 1, 13, 21, 10)),
            ("2024:06:01 13:21:10.03+02:00", datetime(2024, 6, 1, 13, 21, 10)),
            ("2024:06:01 13:21:10Z", datetime(2024, 6, 1, 13, 21, 10)),
            ("2024-06-01T13:21:10", None),  # not ExifTool's format
            ("2024:06:01", None),  # date only — partial dates are poison
            ("0000:00:00 00:00:00", None),
            ("2024:13:01 13:21:10", None),  # impossible month
            ("", None),
            (None, None),
            (20240601, None),
        ],
    )
    def test_parse(self, value: object, expected: datetime | None) -> None:
        assert parse_exiftool_datetime(value) == expected


class TestUtcToWallClock:
    def test_summer_offset(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        utc = datetime(2025, 6, 15, 14, 9, 10)
        assert utc_to_wall_clock(utc, zone) == datetime(2025, 6, 15, 16, 9, 10)

    def test_winter_offset(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        utc = datetime(2025, 1, 15, 12, 0, 0)
        assert utc_to_wall_clock(utc, zone) == datetime(2025, 1, 15, 13, 0, 0)
