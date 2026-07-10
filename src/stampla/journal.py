"""Write-ahead journal for renames and copies.

Before anything on disk changes, the complete plan is written to a
journal file outside the archive. As each group completes, its key is
appended to a companion done-log — an append is cheap and crash-safe,
so a run interrupted at any point can be resumed (already-done groups
are skipped) or undone (done groups are reverted in reverse order).
Undoing appends a tombstone line rather than rewriting the log, so the
log itself can never be truncated by a crash.

Copy journals record each copy's expected digest, so undo can verify a
destination file still is the copy this run made before deleting it.

Journals live in ``~/.stampla/journals`` by default, never inside
the archive: a tool must not mix its own records into the tree it is
renaming.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Rename:
    old: Path
    new: Path
    #: expected content digest of ``new`` (copy journals), for verified undo
    digest: str | None = None


@dataclass(frozen=True)
class GroupMove:
    """All renames of one group, applied all-or-nothing."""

    key: str
    renames: tuple[Rename, ...]


def default_journal_dir() -> Path:
    return Path.home() / ".stampla" / "journals"


class Journal:
    """One apply run's plan and progress."""

    def __init__(
        self,
        path: Path,
        root: Path,
        moves: tuple[GroupMove, ...],
        kind: str = "rename",
        algorithm: str = "md5",
        command: str | None = None,
        created_at: str = "",
    ) -> None:
        self.path = path
        self.root = root
        self.moves = moves
        self.kind = kind
        self.algorithm = algorithm
        self.command = command
        self.created_at = created_at
        self._done_path = path.with_suffix(".done")

    @classmethod
    def create(
        cls,
        root: Path,
        moves: tuple[GroupMove, ...],
        directory: Path | None = None,
        kind: str = "rename",
        algorithm: str = "md5",
        command: str | None = None,
    ) -> Journal:
        """Write the full plan to a new journal file before any change.

        ``kind`` is ``rename`` (sources move away) or ``copy`` (sources
        stay; undo deletes the copies after verifying their digests).
        ``command`` records which command produced the run, purely as
        provenance for whoever reads the journal later.
        """
        keys = [move.key for move in moves]
        if len(set(keys)) != len(keys):
            raise ValueError("journal keys must be unique; refusing an ambiguous plan")
        directory = directory or default_journal_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"journal-{stamp}-{os.getpid()}.json"
        counter = 0
        while path.exists():
            counter += 1
            path = directory / f"journal-{stamp}-{os.getpid()}-{counter}.json"
        payload = {
            "version": 2,
            "kind": kind,
            "algorithm": algorithm,
            "command": command,
            "created_at": stamp,
            "root": str(root),
            "moves": [
                {
                    "key": move.key,
                    "renames": [
                        [str(r.old), str(r.new)]
                        if r.digest is None
                        else [str(r.old), str(r.new), r.digest]
                        for r in move.renames
                    ],
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
        _fsync_directory(directory)
        return cls(
            path, root, moves, kind=kind, algorithm=algorithm, command=command, created_at=stamp
        )

    @classmethod
    def load(cls, path: Path) -> Journal:
        with path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
        moves = tuple(
            GroupMove(
                key=entry["key"],
                renames=tuple(
                    Rename(
                        old=Path(item[0]),
                        new=Path(item[1]),
                        digest=item[2] if len(item) > 2 else None,
                    )
                    for item in entry["renames"]
                ),
            )
            for entry in payload["moves"]
        )
        return cls(
            path,
            Path(payload["root"]),
            moves,
            kind=payload.get("kind", "rename"),
            algorithm=payload.get("algorithm", "md5"),
            command=payload.get("command"),
            created_at=payload.get("created_at", ""),
        )

    def done_keys(self) -> set[str]:
        """Applied groups: done lines minus their undo tombstones, in order."""
        done: set[str] = set()
        for line in self._log_lines():
            if line.startswith("!"):
                done.discard(line[1:])
            else:
                done.add(line)
        return done

    def status(self) -> str:
        """Where this run stands: pending, partial, complete or undone.

        ``partial`` is the interrupted case resume finishes; ``undone``
        means groups were applied once but every one has since been
        reverted (tombstoned), as opposed to never applied at all.
        """
        done = self.done_keys()
        if len(done) == len(self.moves):
            return "complete"
        if done:
            return "partial"
        if any(line.startswith("!") for line in self._log_lines()):
            return "undone"
        return "pending"

    def summary(self) -> JournalSummary:
        return JournalSummary(
            path=self.path,
            root=self.root,
            kind=self.kind,
            command=self.command,
            created_at=self.created_at,
            groups=len(self.moves),
            status=self.status(),
        )

    def _log_lines(self) -> list[str]:
        if not self._done_path.exists():
            return []
        return [
            line
            for line in self._done_path.read_text(encoding="utf-8").splitlines()
            if line.rstrip("\n")
        ]

    def mark_done(self, key: str) -> None:
        self._append(key)

    def mark_undone(self, key: str) -> None:
        """Record an undo as a tombstone; the log is never rewritten."""
        self._append("!" + key)

    def _append(self, line: str) -> None:
        with self._done_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _fsync_directory(directory: Path) -> None:
    """Best effort: not all platforms allow opening a directory."""
    with suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def list_journals(directory: Path | None = None) -> list[Path]:
    """Journal files, oldest first by modification time."""
    directory = directory or default_journal_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("journal-*.json"), key=lambda p: p.stat().st_mtime)


@dataclass(frozen=True)
class JournalSummary:
    """One line of history: what a journal did, for whom, and its state."""

    path: Path
    root: Path
    kind: str
    command: str | None
    created_at: str
    groups: int
    status: str


def journal_summaries(
    directory: Path | None = None, root: Path | None = None
) -> list[JournalSummary]:
    """Summaries of every readable journal, oldest first.

    ``root`` narrows the history to one archive — journals are stored
    globally, but a consumer almost always asks about a single root.
    Unreadable journal files are skipped; they still appear in
    :func:`list_journals` for whoever wants to inspect them.
    """
    summaries: list[JournalSummary] = []
    for path in list_journals(directory):
        try:
            journal = Journal.load(path)
        except (OSError, ValueError, KeyError):
            continue
        if root is not None and Path(journal.root).resolve() != root.resolve():
            continue
        summaries.append(journal.summary())
    return summaries
