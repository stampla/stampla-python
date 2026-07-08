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


class TestDisasterPaths:
    def test_rollback_failure_reports_mixed_state(
        self, root: Path, journal_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import stampla.apply as apply_module

        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        make_file(root, "blocked.xmp")
        move = family(root, "a", ("a.nef", "renamed.nef"), ("a.xmp", "blocked.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)

        real = apply_module._no_clobber_rename
        calls = {"n": 0}

        def flaky(old: Path, new: Path) -> None:
            calls["n"] += 1
            if calls["n"] >= 3:  # the rollback attempt
                raise OSError("device gone")
            real(old, new)

        monkeypatch.setattr(apply_module, "_no_clobber_rename", flaky)
        result = apply_plan(journal)
        assert not result.ok
        assert "ROLLBACK ALSO FAILED" in result.failed[0][1]
        assert "restore from the journal manually" in result.failed[0][1]
        assert journal.done_keys() == set()  # never marked done

    def test_copy_family_cleans_up_after_midway_failure(
        self, root: Path, journal_dir: Path
    ) -> None:
        card = root.parent / "card"
        card.mkdir()
        (card / "a.nef").write_bytes(b"a")
        (card / "a.xmp").write_bytes(b"x")
        make_file(root, "taken.xmp")  # second copy's target exists
        move = FamilyMove(
            key="a",
            renames=(
                Rename(old=card / "a.nef", new=root / "copied.nef"),
                Rename(old=card / "a.xmp", new=root / "taken.xmp"),
            ),
        )
        journal = Journal.create(root, (move,), directory=journal_dir, kind="copy")
        result = apply_plan(journal)
        assert not result.ok
        assert not (root / "copied.nef").exists()  # partial copy removed
        assert (card / "a.nef").exists()  # sources untouched
        assert (card / "a.xmp").exists()

    def test_failed_copy_leaves_no_scratch_files(
        self, root: Path, journal_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil as shutil_module

        card = root.parent / "card2"
        card.mkdir()
        (card / "a.nef").write_bytes(b"a")

        def exploding_copy(src: object, dst: object) -> None:
            Path(str(dst)).write_bytes(b"torn")  # simulate a partial write
            raise OSError("I/O error mid-copy")

        monkeypatch.setattr(shutil_module, "copy2", exploding_copy)
        move = FamilyMove(key="a", renames=(Rename(old=card / "a.nef", new=root / "b.nef"),))
        journal = Journal.create(root, (move,), directory=journal_dir, kind="copy")
        result = apply_plan(journal)
        assert not result.ok
        assert not (root / "b.nef").exists()
        assert list(root.glob("*.part")) == []  # no torn scratch remains


class TestSafetyGuarantees:
    def test_archive_lock_excludes_second_apply(self, root: Path, journal_dir: Path) -> None:
        from stampla.apply import ArchiveLockError, archive_lock

        make_file(root, "a.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        with archive_lock(root), pytest.raises(ArchiveLockError, match="lock"):
            apply_plan(journal)
        assert (root / "a.nef").exists()  # nothing happened under contention

    def test_copy_undo_refuses_edited_destination(self, root: Path, journal_dir: Path) -> None:
        import hashlib

        card = root.parent / "cardx"
        card.mkdir()
        source = card / "a.nef"
        source.write_bytes(b"payload")
        digest = hashlib.md5(b"payload").hexdigest()
        move = FamilyMove(
            key="a",
            renames=(Rename(old=source, new=root / "copied.nef", digest=digest),),
        )
        journal = Journal.create(root, (move,), directory=journal_dir, kind="copy")
        assert apply_plan(journal).ok

        (root / "copied.nef").write_bytes(b"EDITED SINCE IMPORT")
        result = undo_journal(journal)
        assert not result.ok
        assert "refusing to delete" in result.failed[0][1]
        assert (root / "copied.nef").exists()

    def test_copy_undo_deletes_verified_copy(self, root: Path, journal_dir: Path) -> None:
        import hashlib

        card = root.parent / "cardy"
        card.mkdir()
        source = card / "a.nef"
        source.write_bytes(b"payload")
        digest = hashlib.md5(b"payload").hexdigest()
        move = FamilyMove(
            key="a",
            renames=(Rename(old=source, new=root / "copied.nef", digest=digest),),
        )
        journal = Journal.create(root, (move,), directory=journal_dir, kind="copy")
        assert apply_plan(journal).ok
        assert undo_journal(journal).ok
        assert not (root / "copied.nef").exists()
        assert source.exists()

    def test_crash_completed_family_is_recovered_on_resume(
        self, root: Path, journal_dir: Path
    ) -> None:
        make_file(root, "a.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        # simulate: the rename happened but the crash hit before mark_done
        (root / "a.nef").rename(root / "b.nef")

        result = apply_plan(journal)
        assert result.ok
        assert result.applied == ["a"]  # recognized, recorded, not redone
        assert journal.done_keys() == {"a"}
        assert (root / "b.nef").read_bytes() == b"a.nef"

    def test_undo_then_reapply_via_tombstones(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = family(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        assert apply_plan(journal).ok
        assert undo_journal(journal).ok
        assert journal.done_keys() == set()
        # re-apply after undo works; the log was never rewritten
        assert apply_plan(journal).applied == ["a"]
        assert journal.done_keys() == {"a"}

    def test_duplicate_journal_keys_are_rejected(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            family(root, "k", ("a.nef", "a2.nef")),
            family(root, "k", ("b.nef", "b2.nef")),
        )
        assert any("duplicate journal key" in p for p in validate_plan(moves, root))
        with pytest.raises(ValueError, match="unique"):
            Journal.create(root, moves, directory=journal_dir)


class TestContainment:
    def test_apply_rechecks_target_containment(self, root: Path, journal_dir: Path) -> None:
        from stampla.apply import _containment_error
        from stampla.journal import FamilyMove as FM

        make_file(root, "a.nef")
        escaping = FM(
            key="a",
            renames=(Rename(old=root / "a.nef", new=root.parent / "outside.nef"),),
        )
        error = _containment_error(escaping, root)
        assert error is not None
        assert "escapes the root" in error


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

    def test_list_journals_in_creation_order(self, root: Path, journal_dir: Path) -> None:
        first = Journal.create(root, (), directory=journal_dir)
        second = Journal.create(root, (), directory=journal_dir)
        assert list_journals(journal_dir) == [first.path, second.path]

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

    def test_resume_finishes_interrupted_run(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            family(root, "a", ("a.nef", "a2.nef")),
            family(root, "b", ("b.nef", "b2.nef")),
        )
        journal = Journal.create(root, moves, directory=journal_dir)
        journal.mark_done("a")
        (root / "a.nef").rename(root / "a2.nef")  # family a was applied pre-crash

        assert main(["resume", str(journal.path)]) == 0
        assert (root / "a2.nef").exists()
        assert (root / "b2.nef").exists()
        assert journal.done_keys() == {"a", "b"}
