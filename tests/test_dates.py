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
    timestamp_from_name,
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


class TestTimestampFromName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("20190504_101112.jpg", datetime(2019, 5, 4, 10, 11, 12)),
            ("VID_20190504_101112.mp4", datetime(2019, 5, 4, 10, 11, 12)),
            ("2016-12-31 23.59.59.png", datetime(2016, 12, 31, 23, 59, 59)),
            ("2016-12-31T23-59-59 party.jpg", datetime(2016, 12, 31, 23, 59, 59)),
            # phone cameras: compact seconds followed by milliseconds
            ("PXL_20220612_133017259.jpg", datetime(2022, 6, 12, 13, 30, 17)),
            ("IMG_20180707_083818835.jpg", datetime(2018, 7, 7, 8, 38, 18)),
            # canonical archive names carry their timestamp too
            ("20200530_125438_7ea4f4fd_ref.jpg", datetime(2020, 5, 30, 12, 54, 38)),
        ],
    )
    def test_recovers_year_first_timestamps(self, name: str, expected: datetime) -> None:
        assert timestamp_from_name(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "31.12.2016 party.jpg",  # day-first: never interpreted
            "doc_12312016_121212.jpg",  # US month-first: never interpreted
            "IMG-20161231-WA0001.jpg",  # date only — no time to recover
            "20210329_Hania_thumbnail.jpg",  # date only
            "20260101_888888.jpg",  # not a valid time
            "20261331_101112.jpg",  # not a valid month
            "20260230_101112.jpg",  # February 30th
            "2016-1231_101112.jpg",  # inconsistent date separators
            "2016-12-31 23.5959.jpg",  # inconsistent time separators
            # a real phone template bug: HH.<month>.SS in the time slot —
            # the extra text between date and time keeps it unmatched, and
            # that is correct, because its minutes field lies
            ("2008.03.05 godz. 16.03.06.jpg"),
            "IMG_4231.jpg",
        ],
    )
    def test_never_guesses(self, name: str) -> None:
        assert timestamp_from_name(name) is None


class TestUtcChainMarker:
    def test_marked_entry_converts_and_flags(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        tags = {"QuickTime:CreateDate": "2025:06:15 14:09:10"}
        result = resolve_date(tags, ("QuickTime:CreateDate@utc",), zone)
        assert result == ResolvedDate(
            value=datetime(2025, 6, 15, 16, 9, 10), source="QuickTime:CreateDate@utc"
        )

    def test_unqualified_marked_entry(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        tags = {"QuickTime:CreateDate": "2025:01:15 12:00:00"}
        result = resolve_date(tags, ("CreateDate@utc",), zone)
        assert result == ResolvedDate(
            value=datetime(2025, 1, 15, 13, 0, 0), source="QuickTime:CreateDate@utc"
        )

    def test_marker_requires_timezone(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            resolve_date({"QuickTime:CreateDate": "2025:06:15 14:09:10"}, ("CreateDate@utc",))

    def test_unmarked_entries_never_convert(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        tags = {"QuickTime:CreateDate": "2021:08:08 14:56:53"}  # BRAW: local in QT
        result = resolve_date(tags, ("QuickTime:CreateDate",), zone)
        assert result == ResolvedDate(
            value=datetime(2021, 8, 8, 14, 56, 53), source="QuickTime:CreateDate"
        )

    def test_chain_tags_strips_markers(self) -> None:
        from stampla.dates import chain_tags

        assert chain_tags(("EXIF:DateTimeOriginal", "QuickTime:CreateDate@utc")) == {
            "DateTimeOriginal",
            "CreateDate",
        }


class TestUtcToWallClock:
    def test_summer_offset(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        utc = datetime(2025, 6, 15, 14, 9, 10)
        assert utc_to_wall_clock(utc, zone) == datetime(2025, 6, 15, 16, 9, 10)

    def test_winter_offset(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        utc = datetime(2025, 1, 15, 12, 0, 0)
        assert utc_to_wall_clock(utc, zone) == datetime(2025, 1, 15, 13, 0, 0)
