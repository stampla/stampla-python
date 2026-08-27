"""Tests for tree scanning and classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from stampla.naming import Grammar
from stampla.pattern import DEFAULT_PATTERN
from stampla.scan import FileStatus, scan_tree

GRAMMAR = Grammar(patterns=(DEFAULT_PATTERN,))


def touch(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    touch(tmp_path, "2026/2026-02/20260214_125556_1355acb2.nef")
    touch(tmp_path, "2026/2026-02/20260214_125556_1355acb2.xmp")
    touch(tmp_path, "2026/2026-02/20260214_125556_1355acb2(1).fp3")
    touch(tmp_path, "2026/2026-02/DSC_9999.NEF")
    touch(tmp_path, "2026/2026-02/.DS_Store")
    touch(tmp_path, "2026/2026-02/._20260214_125556_1355acb2.nef")
    touch(tmp_path, "2023/2023-01/CaptureOne/Cache/thumb.cot")
    touch(tmp_path, "2023/2023-01/20230106_121300_7f7d8bc8.raf")
    touch(tmp_path, "notes.cot")
    return tmp_path


class TestScanTree:
    def test_classification(self, tree: Path) -> None:
        results = {file.path.name: file.status for file in scan_tree(tree, GRAMMAR)}
        assert results["20260214_125556_1355acb2.nef"] == FileStatus.NAMED
        assert results["20260214_125556_1355acb2.xmp"] == FileStatus.NAMED
        assert results["20260214_125556_1355acb2(1).fp3"] == FileStatus.MALFORMED
        assert results["DSC_9999.NEF"] == FileStatus.UNNAMED

    def test_hidden_files_are_skipped(self, tree: Path) -> None:
        names = {file.path.name for file in scan_tree(tree, GRAMMAR)}
        assert ".DS_Store" not in names
        assert "._20260214_125556_1355acb2.nef" not in names

    def test_named_files_carry_parse_results(self, tree: Path) -> None:
        named = [file for file in scan_tree(tree, GRAMMAR) if file.status == FileStatus.NAMED]
        assert all(file.parsed is not None for file in named)
        assert all(
            file.parsed is None
            for file in scan_tree(tree, GRAMMAR)
            if file.status != FileStatus.NAMED
        )

    def test_exclude_by_extension_glob(self, tree: Path) -> None:
        names = {file.path.name for file in scan_tree(tree, GRAMMAR, excludes=("*.cot",))}
        assert "notes.cot" not in names
        assert "thumb.cot" not in names

    def test_exclude_prunes_directories(self, tree: Path) -> None:
        scanned = list(scan_tree(tree, GRAMMAR, excludes=("**/CaptureOne/**",)))
        assert all("CaptureOne" not in file.path.parts for file in scanned)
        # the sibling file in the same month directory is still scanned
        assert any(file.path.name == "20230106_121300_7f7d8bc8.raf" for file in scanned)

    def test_exclude_top_level_tree(self, tmp_path: Path) -> None:
        touch(tmp_path, "Tether/roll1/scan_01.NEF")
        touch(tmp_path, "keep/20260214_125556_1355acb2.nef")
        scanned = list(scan_tree(tmp_path, GRAMMAR, excludes=("Tether/**",)))
        assert [file.path.name for file in scanned] == ["20260214_125556_1355acb2.nef"]

    def test_deterministic_order(self, tree: Path) -> None:
        first = [file.path for file in scan_tree(tree, GRAMMAR)]
        second = [file.path for file in scan_tree(tree, GRAMMAR)]
        assert first == second
        # within one directory, files come alphabetically
        per_dir: dict[Path, list[str]] = {}
        for path in first:
            per_dir.setdefault(path.parent, []).append(path.name)
        assert all(names == sorted(names) for names in per_dir.values())


class TestEmptyTree:
    def test_empty_directory(self, tmp_path: Path) -> None:
        assert list(scan_tree(tmp_path, GRAMMAR)) == []


class TestScanErrors:
    def test_unreadable_directory_reaches_the_handler(self, tmp_path: Path) -> None:
        import sys

        if sys.platform == "win32":
            pytest.skip("permission bits do not translate")
        touch(tmp_path, "2026/2026-02/20260214_125556_1355acb2.nef")
        sealed = tmp_path / "2019"
        sealed.mkdir()
        sealed.chmod(0)
        errors: list[tuple[Path, str]] = []
        try:
            files = list(scan_tree(tmp_path, GRAMMAR, on_error=lambda p, m: errors.append((p, m))))
        finally:
            sealed.chmod(0o755)
        assert len(files) == 1
        assert errors
        assert errors[0][0] == sealed
        assert "cannot scan" in errors[0][1]

    def test_unreadable_directory_raises_without_a_handler(self, tmp_path: Path) -> None:
        import sys

        if sys.platform == "win32":
            pytest.skip("permission bits do not translate")
        sealed = tmp_path / "2019"
        sealed.mkdir()
        sealed.chmod(0)
        try:
            with pytest.raises(PermissionError):
                list(scan_tree(tmp_path, GRAMMAR))
        finally:
            sealed.chmod(0o755)

    def test_newline_names_are_reported_not_yielded(self, tmp_path: Path) -> None:
        touch(tmp_path, "2026/20260214_125556_1355acb2.nef")
        weird = tmp_path / "2026" / "evil\n-tag.nef"
        weird.touch()
        errors: list[tuple[Path, str]] = []
        files = list(scan_tree(tmp_path, GRAMMAR, on_error=lambda p, m: errors.append((p, m))))
        assert [f.path.name for f in files] == ["20260214_125556_1355acb2.nef"]
        assert errors
        assert errors[0][0] == weird
        assert "newline" in errors[0][1]
