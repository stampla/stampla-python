"""Tests for progress reporting and cooperative cancellation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from stampla.apply import apply_plan
from stampla.hashing import hash_files
from stampla.journal import FamilyMove, Journal, Rename
from stampla.progress import Cancelled, Monitor, ProgressEvent
from stampla.verify import run_verify

requires_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def phases(self) -> set[str]:
        return {event.phase for event in self.events}


class TestMonitor:
    def test_inert_by_default(self) -> None:
        monitor = Monitor()
        monitor.step("hash", 1, 2, Path("x"))  # no callback, no cancel: no effect

    def test_step_emits_after_checking(self) -> None:
        recorder = Recorder()
        monitor = Monitor(callback=recorder)
        monitor.step("hash", 1, 2, Path("x"))
        assert recorder.events == [ProgressEvent("hash", 1, 2, Path("x"))]

    def test_cancel_raises_before_emitting(self) -> None:
        recorder = Recorder()
        monitor = Monitor(callback=recorder, should_cancel=lambda: True)
        with pytest.raises(Cancelled):
            monitor.step("hash", 1, 2)
        assert recorder.events == []


class TestHashingProgress:
    def make_files(self, tmp_path: Path, count: int) -> list[Path]:
        paths = []
        for index in range(count):
            path = tmp_path / f"file-{index}.bin"
            path.write_bytes(bytes([index]) * 64)
            paths.append(path)
        return paths

    def test_one_event_per_file(self, tmp_path: Path) -> None:
        paths = self.make_files(tmp_path, 4)
        recorder = Recorder()
        digests, errors = hash_files(paths, workers=2, monitor=Monitor(callback=recorder))
        assert not errors
        assert len(digests) == 4
        hash_events = [e for e in recorder.events if e.phase == "hash"]
        assert [event.done for event in hash_events] == [1, 2, 3, 4]
        assert all(event.total == 4 for event in hash_events)

    def test_cancellation_stops_hashing(self, tmp_path: Path) -> None:
        paths = self.make_files(tmp_path, 8)
        seen: list[int] = []

        def cancel_after_two() -> bool:
            return len(seen) >= 2

        monitor = Monitor(
            callback=lambda event: seen.append(event.done), should_cancel=cancel_after_two
        )
        with pytest.raises(Cancelled):
            hash_files(paths, workers=2, monitor=monitor)
        assert len(seen) < 8  # it stopped early, queued files were dropped


class TestApplyProgress:
    def test_events_per_family_and_cancel_leaves_journal_resumable(self, tmp_path: Path) -> None:
        root = tmp_path / "archive"
        root.mkdir()
        for name in ("a.nef", "b.nef"):
            (root / name).write_bytes(name.encode())
        moves = (
            FamilyMove("a", (Rename(old=root / "a.nef", new=root / "a2.nef"),)),
            FamilyMove("b", (Rename(old=root / "b.nef", new=root / "b2.nef"),)),
        )
        journal = Journal.create(root, moves, directory=tmp_path / "journals")

        # cancel before the second family: the first is applied and journaled
        checks: list[int] = []

        def count_and_cancel() -> bool:
            checks.append(1)
            return len(checks) > 1

        monitor = Monitor(should_cancel=count_and_cancel)
        with pytest.raises(Cancelled):
            apply_plan(journal, monitor=monitor)
        assert (root / "a2.nef").exists()
        assert (root / "b.nef").exists()  # untouched
        assert journal.done_keys() == {"a"}

        # the interruption is the journal's own case: resume finishes it
        result = apply_plan(journal)
        assert result.skipped == ["a"]
        assert result.applied == ["b"]
        assert (root / "b2.nef").exists()

    def test_apply_reports_each_family(self, tmp_path: Path) -> None:
        root = tmp_path / "archive"
        root.mkdir()
        (root / "a.nef").write_bytes(b"a")
        moves = (FamilyMove("a", (Rename(old=root / "a.nef", new=root / "a2.nef"),)),)
        journal = Journal.create(root, moves, directory=tmp_path / "journals")
        recorder = Recorder()
        apply_plan(journal, monitor=Monitor(callback=recorder))
        assert [e.done for e in recorder.events if e.phase == "rename"] == [0, 1]


@requires_exiftool
class TestVerifyProgress:
    def test_phases_are_reported(self, tmp_path: Path) -> None:
        from tests.test_verify import CONFIG_TEMPLATE, make_master

        (tmp_path / "config.toml").write_text(CONFIG_TEMPLATE.format(root=str(tmp_path)))
        make_master(tmp_path / "Photos" / "2026" / "2026-01", "2026:01:05 12:30:00")

        from stampla.config import load_config

        config = load_config(tmp_path / "config.toml")
        recorder = Recorder()
        run_verify(config, tmp_path, monitor=Monitor(callback=recorder))
        assert {"scan", "dates", "hash"} <= recorder.phases()

    def test_cancelled_verify_raises(self, tmp_path: Path) -> None:
        from tests.test_verify import CONFIG_TEMPLATE, make_master

        (tmp_path / "config.toml").write_text(CONFIG_TEMPLATE.format(root=str(tmp_path)))
        make_master(tmp_path / "Photos" / "2026" / "2026-01", "2026:01:05 12:30:00")

        from stampla.config import load_config

        config = load_config(tmp_path / "config.toml")
        with pytest.raises(Cancelled):
            run_verify(config, tmp_path, monitor=Monitor(should_cancel=lambda: True))
