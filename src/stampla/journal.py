"""Write-ahead journal for renames.

Before anything on disk changes, the complete plan is written to a
journal file outside the archive. As each family completes, its key is
appended to a companion done-log — an append is cheap and crash-safe,
so a run interrupted at any point can be resumed (already-done families
are skipped) or undone (done families are reverted in reverse order).

Journals live in ``~/.stampla/journals`` by default, never inside
the archive: a tool must not mix its own records into the tree it is
renaming.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Rename:
    old: Path
    new: Path


@dataclass(frozen=True)
class FamilyMove:
    """All renames of one family, applied all-or-nothing."""

    key: str
    renames: tuple[Rename, ...]


def default_journal_dir() -> Path:
    return Path.home() / ".stampla" / "journals"


class Journal:
    """One apply run's plan and progress."""

    def __init__(
        self, path: Path, root: Path, moves: tuple[FamilyMove, ...], kind: str = "rename"
    ) -> None:
        self.path = path
        self.root = root
        self.moves = moves
        self.kind = kind
        self._done_path = path.with_suffix(".done")

    @classmethod
    def create(
        cls,
        root: Path,
        moves: tuple[FamilyMove, ...],
        directory: Path | None = None,
        kind: str = "rename",
    ) -> Journal:
        """Write the full plan to a new journal file before any change.

        ``kind`` is ``rename`` (sources move away) or ``copy`` (sources
        stay; undo deletes the copies).
        """
        directory = directory or default_journal_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"journal-{stamp}-{os.getpid()}.json"
        counter = 0
        while path.exists():
            counter += 1
            path = directory / f"journal-{stamp}-{os.getpid()}-{counter}.json"
        payload = {
            "version": 1,
            "kind": kind,
            "created_at": stamp,
            "root": str(root),
            "moves": [
                {
                    "key": move.key,
                    "renames": [[str(r.old), str(r.new)] for r in move.renames],
                }
                for move in moves
            ],
        }
        scratch = path.with_suffix(".json.tmp")
        with scratch.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=1, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(scratch, path)
        return cls(path, root, moves, kind=kind)

    @classmethod
    def load(cls, path: Path) -> Journal:
        with path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
        moves = tuple(
            FamilyMove(
                key=entry["key"],
                renames=tuple(
                    Rename(old=Path(old), new=Path(new)) for old, new in entry["renames"]
                ),
            )
            for entry in payload["moves"]
        )
        return cls(path, Path(payload["root"]), moves, kind=payload.get("kind", "rename"))

    def done_keys(self) -> set[str]:
        if not self._done_path.exists():
            return set()
        return {
            line.strip()
            for line in self._done_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    def mark_done(self, key: str) -> None:
        with self._done_path.open("a", encoding="utf-8") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def clear_done(self, key: str) -> None:
        """Remove a key from the done-log (after a successful undo)."""
        remaining = [k for k in sorted(self.done_keys()) if k != key]
        self._done_path.write_text("".join(k + "\n" for k in remaining), encoding="utf-8")


def list_journals(directory: Path | None = None) -> list[Path]:
    """Journal files, oldest first."""
    directory = directory or default_journal_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("journal-*.json"))
