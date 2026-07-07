"""Tests for the journaled rename engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from stampla.apply import apply_plan, undo_journal, validate_plan
from stampla.cli import main
from stampla.journal import FamilyMove, Journal, Rename, list_journals


@pytest.fixture
def root(tmp_path: Path) -> Path:
    archive = tmp_path / "archive"
    archive.mkdir()
    return archive


@pytest.fixture
def journal_dir(tmp_path: Path) -> Path:
    return tmp_path / "journals"


def make_file(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    return path


def family(root: Path, key: str, *pairs: tuple[str, str]) -> FamilyMove:
    return FamilyMove(
        key=key,
        renames=tuple(Rename(old=root / old, new=root / new) for old, new in pairs),
    )


class TestValidatePlan:
    def test_clean_plan(self, root: Path) -> None:
        make_file(root, "a.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        assert validate_plan((move,), root) == []

    def test_missing_source(self, root: Path) -> None:
        move = family(root, "a", ("ghost.nef", "b.nef"))
        assert any("source missing" in p for p in validate_plan((move,), root))

    def test_existing_target(self, root: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        assert any("already exists" in p for p in validate_plan((move,), root))

    def test_duplicate_targets_across_families(self, root: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            family(root, "a", ("a.nef", "same.nef")),
            family(root, "b", ("b.nef", "same.nef")),
        )
        assert any("duplicate target" in p for p in validate_plan(moves, root))

    def test_target_escaping_root(self, root: Path) -> None:
        make_file(root, "a.nef")
        move = FamilyMove(
            key="a",
            renames=(Rename(old=root / "a.nef", new=root.parent / "outside.nef"),),
        )
        assert any("escapes the root" in p for p in validate_plan((move,), root))

    def test_source_also_target(self, root: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            family(root, "a", ("a.nef", "c.nef")),
            family(root, "b", ("b.nef", "a.nef")),
        )
        assert any("both a source and a target" in p for p in validate_plan(moves, root))

    def test_empty_family(self, root: Path) -> None:
        move = FamilyMove(key="a", renames=())
        assert any("empty" in p for p in validate_plan((move,), root))


class TestApply:
    def test_applies_and_journals(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        move = family(root, "a", ("a.nef", "b.nef"), ("a.xmp", "b.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        result = apply_plan(journal)

        assert result.ok
        assert result.applied == ["a"]
        assert not (root / "a.nef").exists()
        assert (root / "b.nef").read_bytes() == b"a.nef"
        assert (root / "b.xmp").exists()
        assert journal.done_keys() == {"a"}

    def test_resume_skips_done_families(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (family(root, "a", ("a.nef", "a2.nef")), family(root, "b", ("b.nef", "b2.nef")))
        journal = Journal.create(root, moves, directory=journal_dir)
        journal.mark_done("a")  # simulate a crash after family a completed

        result = apply_plan(journal)
        assert result.skipped == ["a"]
        assert result.applied == ["b"]
        assert (root / "a.nef").exists()  # was never actually renamed here
        assert (root / "b2.nef").exists()

    def test_family_rolls_back_on_midway_failure(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        make_file(root, "blocked.xmp")  # second rename's target already exists
        move = family(root, "a", ("a.nef", "renamed.nef"), ("a.xmp", "blocked.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)

        result = apply_plan(journal)
        assert not result.ok
        assert result.failed[0][0] == "a"
        # the first rename was rolled back
        assert (root / "a.nef").exists()
        assert not (root / "renamed.nef").exists()
        assert journal.done_keys() == set()

    def test_other_families_proceed_after_one_fails(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "taken.nef")
        make_file(root, "b.nef")
        moves = (
            family(root, "a", ("a.nef", "taken.nef")),
            family(root, "b", ("b.nef", "fine.nef")),
        )
        journal = Journal.create(root, moves, directory=journal_dir)
        result = apply_plan(journal)
        assert [key for key, _ in result.failed] == ["a"]
        assert result.applied == ["b"]
        assert (root / "fine.nef").exists()


class TestUndo:
    def test_round_trip(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        move = family(root, "a", ("a.nef", "b.nef"), ("a.xmp", "b.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        assert apply_plan(journal).ok

        result = undo_journal(journal)
        assert result.ok
        assert result.applied == ["a"]
        assert (root / "a.nef").exists()
        assert (root / "a.xmp").exists()
        assert not (root / "b.nef").exists()
        assert journal.done_keys() == set()

    def test_undo_skips_never_applied_families(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        result = undo_journal(journal)  # nothing was applied
        assert result.applied == []
        assert result.skipped == ["a"]

    def test_undo_refuses_to_clobber(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        assert apply_plan(journal).ok
        make_file(root, "a.nef")  # someone recreated the original name
        result = undo_journal(journal)
        assert not result.ok
        assert (root / "b.nef").exists()  # untouched


class TestJournal:
    def test_persists_and_reloads(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "zdjęcie ą.nef")
        move = family(root, "rodzina-ą", ("zdjęcie ą.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)

        reloaded = Journal.load(journal.path)
        assert reloaded.root == root
        assert reloaded.moves == (move,)

    def test_list_journals_ordered(self, root: Path, journal_dir: Path) -> None:
        first = Journal.create(root, (), directory=journal_dir)
        second = Journal.create(root, (), directory=journal_dir)
        assert list_journals(journal_dir) == sorted([first.path, second.path])

    def test_list_journals_empty_dir(self, tmp_path: Path) -> None:
        assert list_journals(tmp_path / "nope") == []


class TestUndoCli:
    def test_undo_by_path(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        assert apply_plan(journal).ok

        assert main(["undo", str(journal.path)]) == 0
        assert (root / "a.nef").exists()

    def test_undo_missing_journal_is_an_error(self, tmp_path: Path) -> None:
        assert main(["undo", str(tmp_path / "missing.json")]) == 2
