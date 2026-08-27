"""Tests for the journaled rename engine."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from stampla.apply import apply_plan, undo_journal, validate_plan
from stampla.cli import main
from stampla.journal import GroupMove, Journal, Rename, list_journals


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


def group(root: Path, key: str, *pairs: tuple[str, str]) -> GroupMove:
    return GroupMove(
        key=key,
        renames=tuple(Rename(old=root / old, new=root / new) for old, new in pairs),
    )


class TestValidatePlan:
    def test_clean_plan(self, root: Path) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        assert validate_plan((move,), root) == []

    def test_missing_source(self, root: Path) -> None:
        move = group(root, "a", ("ghost.nef", "b.nef"))
        assert any("source missing" in p for p in validate_plan((move,), root))

    def test_existing_target(self, root: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        assert any("already exists" in p for p in validate_plan((move,), root))

    def test_duplicate_targets_across_groups(self, root: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            group(root, "a", ("a.nef", "same.nef")),
            group(root, "b", ("b.nef", "same.nef")),
        )
        assert any("duplicate target" in p for p in validate_plan(moves, root))

    def test_target_escaping_root(self, root: Path) -> None:
        make_file(root, "a.nef")
        move = GroupMove(
            key="a",
            renames=(Rename(old=root / "a.nef", new=root.parent / "outside.nef"),),
        )
        assert any("escapes the root" in p for p in validate_plan((move,), root))

    def test_source_also_target(self, root: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            group(root, "a", ("a.nef", "c.nef")),
            group(root, "b", ("b.nef", "a.nef")),
        )
        assert any("both a source and a target" in p for p in validate_plan(moves, root))

    def test_empty_group(self, root: Path) -> None:
        move = GroupMove(key="a", renames=())
        assert any("empty" in p for p in validate_plan((move,), root))


class TestApply:
    def test_applies_and_journals(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        move = group(root, "a", ("a.nef", "b.nef"), ("a.xmp", "b.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        result = apply_plan(journal)

        assert result.ok
        assert result.applied == ["a"]
        assert not (root / "a.nef").exists()
        assert (root / "b.nef").read_bytes() == b"a.nef"
        assert (root / "b.xmp").exists()
        assert journal.done_keys() == {"a"}

    def test_resume_skips_done_groups(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (group(root, "a", ("a.nef", "a2.nef")), group(root, "b", ("b.nef", "b2.nef")))
        journal = Journal.create(root, moves, directory=journal_dir)
        journal.mark_done("a")  # simulate a crash after group a completed

        result = apply_plan(journal)
        assert result.skipped == ["a"]
        assert result.applied == ["b"]
        assert (root / "a.nef").exists()  # was never actually renamed here
        assert (root / "b2.nef").exists()

    def test_group_rolls_back_on_midway_failure(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        make_file(root, "blocked.xmp")  # second rename's target already exists
        move = group(root, "a", ("a.nef", "renamed.nef"), ("a.xmp", "blocked.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)

        result = apply_plan(journal)
        assert not result.ok
        assert result.failed[0][0] == "a"
        # the first rename was rolled back
        assert (root / "a.nef").exists()
        assert not (root / "renamed.nef").exists()
        assert journal.done_keys() == set()

    def test_other_groups_proceed_after_one_fails(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "taken.nef")
        make_file(root, "b.nef")
        moves = (
            group(root, "a", ("a.nef", "taken.nef")),
            group(root, "b", ("b.nef", "fine.nef")),
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
        move = group(root, "a", ("a.nef", "renamed.nef"), ("a.xmp", "blocked.xmp"))
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

    def test_copy_group_cleans_up_after_midway_failure(self, root: Path, journal_dir: Path) -> None:
        card = root.parent / "card"
        card.mkdir()
        (card / "a.nef").write_bytes(b"a")
        (card / "a.xmp").write_bytes(b"x")
        make_file(root, "taken.xmp")  # second copy's target exists
        move = GroupMove(
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
        move = GroupMove(key="a", renames=(Rename(old=card / "a.nef", new=root / "b.nef"),))
        journal = Journal.create(root, (move,), directory=journal_dir, kind="copy")
        result = apply_plan(journal)
        assert not result.ok
        assert not (root / "b.nef").exists()
        assert list(root.glob("*.part")) == []  # no torn scratch remains


class TestSafetyGuarantees:
    def test_archive_lock_excludes_second_apply(self, root: Path, journal_dir: Path) -> None:
        from stampla.apply import ArchiveLockError, archive_lock

        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
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
        move = GroupMove(
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
        move = GroupMove(
            key="a",
            renames=(Rename(old=source, new=root / "copied.nef", digest=digest),),
        )
        journal = Journal.create(root, (move,), directory=journal_dir, kind="copy")
        assert apply_plan(journal).ok
        assert undo_journal(journal).ok
        assert not (root / "copied.nef").exists()
        assert source.exists()

    def test_crash_completed_group_is_recovered_on_resume(
        self, root: Path, journal_dir: Path
    ) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
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
        move = group(root, "a", ("a.nef", "b.nef"))
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
            group(root, "k", ("a.nef", "a2.nef")),
            group(root, "k", ("b.nef", "b2.nef")),
        )
        assert any("duplicate journal key" in p for p in validate_plan(moves, root))
        with pytest.raises(ValueError, match="unique"):
            Journal.create(root, moves, directory=journal_dir)


class TestContainment:
    def test_apply_rechecks_target_containment(self, root: Path, journal_dir: Path) -> None:
        from stampla.apply import _containment_error
        from stampla.journal import GroupMove as FM

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
        move = group(root, "a", ("a.nef", "b.nef"), ("a.xmp", "b.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        assert apply_plan(journal).ok

        result = undo_journal(journal)
        assert result.ok
        assert result.applied == ["a"]
        assert (root / "a.nef").exists()
        assert (root / "a.xmp").exists()
        assert not (root / "b.nef").exists()
        assert journal.done_keys() == set()

    def test_undo_skips_never_applied_groups(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        result = undo_journal(journal)  # nothing was applied
        assert result.applied == []
        assert result.skipped == ["a"]

    def test_undo_refuses_to_clobber(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        assert apply_plan(journal).ok
        make_file(root, "a.nef")  # someone recreated the original name
        result = undo_journal(journal)
        assert not result.ok
        assert (root / "b.nef").exists()  # untouched


class TestJournal:
    def test_persists_and_reloads(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "zdjęcie ą.nef")
        move = group(root, "rodzina-ą", ("zdjęcie ą.nef", "b.nef"))
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

    def test_command_provenance_round_trips(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir, command="rename")
        reloaded = Journal.load(journal.path)
        assert reloaded.command == "rename"
        assert reloaded.created_at == journal.created_at

    def test_status_walks_the_lifecycle(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            group(root, "a", ("a.nef", "a2.nef")),
            group(root, "b", ("b.nef", "b2.nef")),
        )
        journal = Journal.create(root, moves, directory=journal_dir)
        assert journal.status() == "pending"
        journal.mark_done("a")
        assert journal.status() == "partial"
        journal.mark_done("b")
        assert journal.status() == "complete"
        journal.mark_undone("a")
        assert journal.status() == "partial"
        journal.mark_undone("b")
        assert journal.status() == "undone"

    def test_summaries_filter_by_root(self, tmp_path: Path, journal_dir: Path) -> None:
        from stampla.journal import journal_summaries

        first_root = tmp_path / "one"
        second_root = tmp_path / "two"
        first_root.mkdir()
        second_root.mkdir()
        Journal.create(first_root, (), directory=journal_dir, command="rename")
        Journal.create(second_root, (), directory=journal_dir, command="import", kind="copy")
        (journal_dir / "journal-19700101T000000Z-0.json").write_text("not json")

        everything = journal_summaries(directory=journal_dir)
        assert len(everything) == 2  # the unreadable one is skipped
        ours = journal_summaries(directory=journal_dir, root=second_root)
        assert [s.command for s in ours] == ["import"]
        assert ours[0].kind == "copy"
        assert ours[0].status == "complete"  # an empty plan has nothing left to do


class TestHistoryCli:
    def test_lists_runs_with_status(
        self, root: Path, journal_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "a2.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir, command="rename")
        assert apply_plan(journal).ok

        assert main(["history"]) == 0
        out = capsys.readouterr().out
        assert "rename" in out
        assert "complete" in out

        assert main(["history", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        entry = payload["history"][0]
        assert entry["command"] == "rename"
        assert entry["status"] == "complete"
        assert entry["groups"] == 1

    def test_root_filter_narrows_the_history(
        self, root: Path, journal_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        Journal.create(root, (), directory=journal_dir, command="rename")
        Journal.create(other, (), directory=journal_dir, command="import", kind="copy")

        assert main(["history", "--root", str(other), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [j["command"] for j in payload["history"]] == ["import"]

    def test_empty_history_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["history"]) == 0
        assert "no journals" in capsys.readouterr().out


class TestUndoCli:
    def test_undo_by_path(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        assert apply_plan(journal).ok

        assert main(["undo", str(journal.path)]) == 0
        assert (root / "a.nef").exists()

    def test_undo_missing_journal_is_an_error(self, tmp_path: Path) -> None:
        assert main(["undo", str(tmp_path / "missing.json")]) == 2

    def test_undo_json_reports_result_counts(
        self, root: Path, journal_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_file(root, "a.nef")
        moves = (group(root, "a", ("a.nef", "a2.nef")), group(root, "b", ("b.nef", "b2.nef")))
        journal = Journal.create(root, moves, directory=journal_dir)
        journal.mark_done("a")
        (root / "a.nef").rename(root / "a2.nef")  # only group a was applied

        assert main(["undo", str(journal.path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == {"applied": 1, "skipped": 1, "failed": 0}
        assert payload["verdict"] is None

    def test_resume_finishes_interrupted_run(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            group(root, "a", ("a.nef", "a2.nef")),
            group(root, "b", ("b.nef", "b2.nef")),
        )
        journal = Journal.create(root, moves, directory=journal_dir)
        journal.mark_done("a")
        (root / "a.nef").rename(root / "a2.nef")  # group a was applied pre-crash

        assert main(["resume", str(journal.path)]) == 0
        assert (root / "a2.nef").exists()
        assert (root / "b2.nef").exists()
        assert journal.done_keys() == {"a", "b"}


def copy_group(root: Path, source_dir: Path, key: str, *names: str) -> GroupMove:
    renames = []
    for name in names:
        source = source_dir / name
        renames.append(
            Rename(
                old=source,
                new=root / name,
                digest=hashlib.md5(source.read_bytes()).hexdigest(),
            )
        )
    return GroupMove(key=key, renames=tuple(renames))


class TestCaseFoldValidation:
    def test_targets_differing_only_by_case_are_refused(self, root: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        moves = (
            group(root, "a", ("a.nef", "x.nef")),
            group(root, "b", ("b.nef", "X.NEF")),
        )
        assert any("case-insensitive" in p for p in validate_plan(moves, root))


class TestCrashRecovery:
    def test_resume_completes_a_half_renamed_group(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        move = group(root, "a", ("a.nef", "b.nef"), ("a.xmp", "b.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        # the crash landed after the first rename, before the second
        (root / "a.nef").rename(root / "b.nef")

        result = apply_plan(journal)
        assert result.ok, result.failed
        assert result.applied == ["a"]
        assert (root / "b.nef").exists()
        assert (root / "b.xmp").exists()
        assert not (root / "a.nef").exists()
        assert not (root / "a.xmp").exists()

    def test_resume_finishes_an_interrupted_hardlink_claim(
        self, root: Path, journal_dir: Path
    ) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        # the crash landed between the link claim and the source unlink
        os.link(root / "a.nef", root / "b.nef")

        result = apply_plan(journal)
        assert result.ok, result.failed
        assert (root / "b.nef").exists()
        assert not (root / "a.nef").exists()

    def test_undo_reverts_a_partially_applied_group(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        make_file(root, "a.xmp")
        move = group(root, "a", ("a.nef", "b.nef"), ("a.xmp", "b.xmp"))
        journal = Journal.create(root, (move,), directory=journal_dir)
        (root / "a.nef").rename(root / "b.nef")  # crash mid-group, never marked

        result = undo_journal(journal)
        assert result.ok, result.failed
        assert result.applied == ["a"]  # the partial rename was reverted
        assert (root / "a.nef").exists()
        assert (root / "a.xmp").exists()
        assert not (root / "b.nef").exists()

    def test_undo_skips_groups_with_nothing_done(self, root: Path, journal_dir: Path) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)

        result = undo_journal(journal)
        assert result.ok
        assert result.skipped == ["a"]
        assert (root / "a.nef").exists()

    def test_resume_reclaims_a_zero_byte_claim(self, root: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        card.mkdir()
        (card / "a.jpg").write_bytes(b"real image bytes")
        move = copy_group(root, card, "a", "a.jpg")
        journal = Journal.create(
            root, (move,), directory=tmp_path / "journals", kind="copy", algorithm="md5"
        )
        # the crash left the exclusive-create claim with no data in it
        (root / "a.jpg").touch()
        (root / f"a.jpg.{os.getpid() + 1}.part").write_bytes(b"stale scratch")

        result = apply_plan(journal)
        assert result.ok, result.failed
        assert (root / "a.jpg").read_bytes() == b"real image bytes"
        assert not list(root.glob("*.part"))

    def test_copy_content_is_verified_before_the_group_counts(
        self, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        card = tmp_path / "card"
        card.mkdir()
        (card / "a.jpg").write_bytes(b"real image bytes")
        move = copy_group(root, card, "a", "a.jpg")
        journal = Journal.create(
            root, (move,), directory=tmp_path / "journals", kind="copy", algorithm="md5"
        )

        real_copy = shutil.copy2

        # same byte count as the source so the size check passes and only
        # the digest can catch it
        def same_size_corruption(src: Path, dst: Path) -> Path:
            real_copy(src, dst)
            data = bytearray(dst.read_bytes())
            data[0] ^= 0xFF
            dst.write_bytes(bytes(data))
            return dst

        monkeypatch.setattr(shutil, "copy2", same_size_corruption)
        result = apply_plan(journal)
        assert not result.ok
        assert "does not match the source digest" in result.failed[0][1]
        assert not (root / "a.jpg").exists()  # rolled back, nothing half-kept
        assert (card / "a.jpg").read_bytes() == b"real image bytes"  # source untouched

    def test_rename_no_clobber_without_hardlinks(
        self, root: Path, journal_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_file(root, "a.nef")
        make_file(root, "taken.nef")
        moves = (
            group(root, "a", ("a.nef", "b.nef")),
            group(root, "c", ("taken.nef", "a.nef")),
        )

        def no_links(src: object, dst: object, **kwargs: object) -> None:
            raise OSError(errno.EPERM, "filesystem does not support hard links")

        monkeypatch.setattr(os, "link", no_links)
        # plain rename still works and still refuses an existing target
        journal = Journal.create(root, (moves[0],), directory=journal_dir)
        result = apply_plan(journal)
        assert result.ok, result.failed
        assert (root / "b.nef").exists()

        blocked = GroupMove(
            key="blocked", renames=(Rename(old=root / "taken.nef", new=root / "b.nef"),)
        )
        journal2 = Journal.create(root, (blocked,), directory=journal_dir)
        result2 = apply_plan(journal2)
        assert not result2.ok
        assert "already exists" in result2.failed[0][1]
        assert (root / "taken.nef").exists()  # rolled back / untouched

    def test_done_log_failure_is_loud_and_resume_self_heals(
        self, root: Path, journal_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_file(root, "a.nef")
        move = group(root, "a", ("a.nef", "b.nef"))
        journal = Journal.create(root, (move,), directory=journal_dir)

        def broken_mark(self: Journal, key: str) -> None:
            raise OSError("done-log write refused")

        monkeypatch.setattr(Journal, "mark_done", broken_mark)
        result = apply_plan(journal)
        assert not result.ok
        assert "could not be recorded" in result.failed[0][1]
        assert (root / "b.nef").exists()  # applied on disk, said so loudly

        monkeypatch.undo()
        healed = apply_plan(journal)
        assert healed.ok, healed.failed
        assert healed.applied == ["a"]  # recognized as crash-completed
        assert (root / "b.nef").read_bytes() == b"a.nef"  # never re-renamed

    def test_undo_refuses_when_copy_destinations_vanished(self, root: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        card.mkdir()
        (card / "a.jpg").write_bytes(b"first")
        (card / "b.jpg").write_bytes(b"second")
        move = copy_group(root, card, "a", "a.jpg", "b.jpg")
        journal = Journal.create(
            root, (move,), directory=tmp_path / "journals", kind="copy", algorithm="md5"
        )
        assert apply_plan(journal).ok
        (root / "a.jpg").rename(root / "renamed-since.jpg")  # the canonical fate

        result = undo_journal(journal)
        assert not result.ok
        assert "no longer exist" in result.failed[0][1]
        assert (root / "b.jpg").exists()  # nothing was half-deleted


class TestUndoLatest:
    def test_undo_latest_reverts_the_newest_journal(
        self, root: Path, journal_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import stampla.journal as journal_module

        monkeypatch.setattr(journal_module, "default_journal_dir", lambda: journal_dir)
        make_file(root, "a.nef")
        make_file(root, "b.nef")
        first = Journal.create(root, (group(root, "a", ("a.nef", "a2.nef")),))
        assert apply_plan(first).ok
        second = Journal.create(root, (group(root, "b", ("b.nef", "b2.nef")),))
        assert apply_plan(second).ok

        assert main(["undo", "--latest"]) == 0
        assert (root / "b.nef").exists()  # newest run reverted
        assert (root / "a2.nef").exists()  # older run untouched

    def test_bare_undo_lists_journals(
        self,
        root: Path,
        journal_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import stampla.journal as journal_module

        monkeypatch.setattr(journal_module, "default_journal_dir", lambda: journal_dir)
        make_file(root, "a.nef")
        journal = Journal.create(root, (group(root, "a", ("a.nef", "a2.nef")),))
        assert main(["undo"]) == 0
        out = capsys.readouterr()
        assert str(journal.path) in out.out
        assert (root / "a.nef").exists()  # listing reverts nothing


class TestCorruptJournal:
    def test_resume_of_a_corrupt_journal_fails_cleanly(
        self, journal_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        journal_dir.mkdir(parents=True)
        corrupt = journal_dir / "journal-x.json"
        corrupt.write_text('{"version": 2, "moves": [{"renames"')
        assert main(["resume", str(corrupt)]) == 2
        err = capsys.readouterr().err
        assert "corrupt or truncated" in err
        assert "Traceback" not in err
