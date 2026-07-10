"""Tests for group grouping."""

from __future__ import annotations

from pathlib import Path

from stampla.config import SidecarDirRule
from stampla.group import Group, group_by_prefix, group_originals
from stampla.naming import DEFAULT_RAW_EXTENSIONS, Grammar
from stampla.pattern import DEFAULT_PATTERN
from stampla.scan import FileStatus, ScannedFile

GRAMMAR = Grammar(patterns=(DEFAULT_PATTERN,))

VIDEO_EXTENSIONS = frozenset({"mov", "braw", "nev", "r3d"})

# Camera-native masters for import grouping: excludes tif on purpose, so
# editor output like -Edit.tif counts as a derivative.
CAMERA_EXTENSIONS = frozenset({"nef", "cr2", "cr3", "raf", "rw2", "dng"})


def scanned(relative: str) -> ScannedFile:
    path = Path(relative)
    parsed = GRAMMAR.parse(path.name)
    status = FileStatus.NAMED if parsed else FileStatus.UNNAMED
    return ScannedFile(path=path, status=status, parsed=parsed)


class TestGroupByPrefix:
    def test_full_photo_group_including_cross_directory_sidecar(self) -> None:
        files = [
            scanned("2022/2022-05/20220523_192742_d3147a94.nef"),
            scanned("2022/2022-05/20220523_192742_d3147a94.xmp"),
            scanned("2022/2022-05/20220523_192742_d3147a94.nef.xmp"),
            scanned("2022/2022-05/NKSC_PARAM/20220523_192742_d3147a94.nef.nksc"),
            scanned("2022/2022-05/20220523_192742_d3147a94-Edit.tif"),
            scanned("2022/2022-05/20220523_192742_d3147a94-Edit.tif.pp3"),
            scanned("2022/2022-05/20220524_100000_aaaaaaaa.nef"),
        ]
        groups = group_by_prefix(files)
        assert [group.prefix for group in groups] == [
            "20220523_192742_d3147a94",
            "20220524_100000_aaaaaaaa",
        ]
        assert len(groups[0].members) == 6

    def test_master_identification(self) -> None:
        group = group_by_prefix(
            [
                scanned("a/20220523_192742_d3147a94.nef"),
                scanned("a/20220523_192742_d3147a94.xmp"),
                scanned("a/20220523_192742_d3147a94-Edit.tif"),
            ]
        )[0]
        master = group.master(DEFAULT_RAW_EXTENSIONS)
        assert master is not None
        assert master.path.name == "20220523_192742_d3147a94.nef"

    def test_orphan_sidecar_group_has_no_master(self) -> None:
        group = group_by_prefix([scanned("a/20220523_192742_d3147a94.xmp")])[0]
        assert group.master(DEFAULT_RAW_EXTENSIONS) is None

    def test_suffixed_derivative_is_not_a_master(self) -> None:
        # A -Edit.tif is a derivative even though tif is a raw extension.
        group = group_by_prefix([scanned("a/20220523_192742_d3147a94-Edit.tif")])[0]
        assert group.master(DEFAULT_RAW_EXTENSIONS) is None

    def test_ambiguous_masters_return_none(self) -> None:
        group = group_by_prefix(
            [
                scanned("a/20220523_192742_d3147a94.nef"),
                scanned("a/20220523_192742_d3147a94.dng"),
            ]
        )[0]
        assert group.master(DEFAULT_RAW_EXTENSIONS) is None
        assert len(group.master_candidates(DEFAULT_RAW_EXTENSIONS)) == 2

    def test_video_master_with_video_extensions(self) -> None:
        group = group_by_prefix([scanned("v/20210808_145653_941930e9.braw")])[0]
        master = group.master(VIDEO_EXTENSIONS)
        assert master is not None

    def test_unnamed_files_are_ignored(self) -> None:
        assert group_by_prefix([scanned("a/DSC_1234.NEF")]) == []


class TestGroupOriginals:
    def test_basic_grouping_by_base(self) -> None:
        groups = group_originals(
            [
                Path("card/DSC_1234.NEF"),
                Path("card/DSC_1234.xmp"),
                Path("card/DSC_1234.NEF.xmp"),
                Path("card/DSC_1235.NEF"),
            ],
            master_extensions=CAMERA_EXTENSIONS,
        )
        assert [(group.base, len(group.members)) for group in groups] == [
            ("DSC_1234", 3),
            ("DSC_1235", 1),
        ]

    def test_sidecar_directory_rule_maps_to_master_home(self) -> None:
        groups = group_originals(
            [
                Path("card/DSC_1234.NEF"),
                Path("card/NKSC_PARAM/DSC_1234.NEF.nksc"),
            ],
            sidecar_dirs=(SidecarDirRule(subdir="NKSC_PARAM", strip=".nksc"),),
            master_extensions=CAMERA_EXTENSIONS,
        )
        assert len(groups) == 1
        assert groups[0].directory == Path("card")
        assert len(groups[0].members) == 2

    def test_labeled_derivative_merges_into_master_group(self) -> None:
        groups = group_originals(
            [
                Path("card/DSC1234.NEF"),
                Path("card/DSC1234-Edit.tif"),
                Path("card/DSC1234_hdr.tif"),
            ],
            master_extensions=CAMERA_EXTENSIONS,
        )
        assert len(groups) == 1
        assert groups[0].base == "DSC1234"

    def test_underscore_base_is_not_split_without_a_master(self) -> None:
        # DSC_1234 must not merge into a hypothetical "DSC" group.
        groups = group_originals(
            [Path("card/DSC_1234.NEF"), Path("card/DSC_1235.NEF")],
            master_extensions=CAMERA_EXTENSIONS,
        )
        assert [group.base for group in groups] == ["DSC_1234", "DSC_1235"]

    def test_label_does_not_merge_into_masterless_group(self) -> None:
        groups = group_originals(
            [Path("card/DSC1234.xmp"), Path("card/DSC1234-Edit.tif")],
            master_extensions=CAMERA_EXTENSIONS,
        )
        assert [group.base for group in groups] == ["DSC1234", "DSC1234-Edit"]

    def test_groups_are_per_directory(self) -> None:
        groups = group_originals(
            [Path("card/100ND780/DSC_1234.NEF"), Path("card/101ND780/DSC_1234.NEF")],
            master_extensions=CAMERA_EXTENSIONS,
        )
        assert len(groups) == 2

    def test_longest_parent_base_wins(self) -> None:
        groups = group_originals(
            [
                Path("card/IMG.NEF"),
                Path("card/IMG_01.NEF"),
                Path("card/IMG_01-Edit.tif"),
            ],
            master_extensions=CAMERA_EXTENSIONS,
        )
        bases = {group.base: len(group.members) for group in groups}
        assert bases == {"IMG": 1, "IMG_01": 2}


def test_group_is_frozen() -> None:
    group = Group(prefix="20220523_192742_d3147a94", members=())
    assert group.prefix == "20220523_192742_d3147a94"
