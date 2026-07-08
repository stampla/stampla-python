"""Executing rename and copy plans: validate, lock, apply, undo.

The order of protections:

1. **Global validation before any I/O.** Every source must exist, no two
   renames may share a source or a target, keys must be unique, and
   everything must stay inside the archive root. One problem anywhere
   means nothing is touched.
2. **One process at a time.** An exclusive lock file under the archive
   root guards every apply and undo; a second stampla cannot race the
   first past the no-clobber checks.
3. **Write-ahead journal** (see :mod:`stampla.journal`).
4. **Atomic no-clobber.** Renames claim their target with a hard link
   where the platform allows it, so an existing target fails atomically
   instead of being silently replaced; copies claim the final name with
   an exclusive create before the verified data replaces it, and are
   fsynced before they count.
5. **Per-family atomicity.** A family's renames either all happen or —
   if one fails midway — the already-done ones are reverted on the spot.
6. **Resume and verified undo.** Re-applying a journal skips done
   families and recognizes families a crash completed but never marked.
   Undoing a copy journal re-hashes each destination and refuses to
   delete anything that is no longer the copy this run made.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from stampla.hashing import compute_digests
from stampla.journal import FamilyMove, Journal, Rename


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


class ArchiveLockError(RuntimeError):
    """Another apply or undo is already running against this archive."""


@contextmanager
def archive_lock(root: Path) -> Iterator[None]:
    """Exclusive per-archive lock held for the duration of an apply/undo."""
    lock_dir = root / ".stampla"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ArchiveLockError(
            f"another stampla apply/undo appears to be running (lock file"
            f" {lock_path}); if you are sure it is not, remove the file"
        ) from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def validate_plan(
    moves: tuple[FamilyMove, ...], root: Path, sources_outside_root: bool = False
) -> list[str]:
    """All problems that make the plan unsafe; empty list means go.

    ``sources_outside_root`` fits copy plans, where sources live on a
    memory card; targets must always stay inside the archive root.
    """
    problems: list[str] = []
    sources: set[Path] = set()
    targets: set[Path] = set()
    keys: set[str] = set()
    root = root.resolve()
    for move in moves:
        if move.key in keys:
            problems.append(f"duplicate journal key: {move.key}")
        keys.add(move.key)
        if not move.renames:
            problems.append(f"{move.key}: empty family move")
        for rename in move.renames:
            checked: tuple[tuple[str, Path], ...] = (
                ("source", rename.old),
                ("target", rename.new),
            )
            if sources_outside_root:
                checked = (("target", rename.new),)
            for label, path in checked:
                if not path.resolve().is_relative_to(root):
                    problems.append(f"{move.key}: {label} escapes the root: {path}")
            if rename.old in sources:
                problems.append(f"{move.key}: duplicate source {rename.old}")
            sources.add(rename.old)
            if rename.new in targets:
                problems.append(f"{move.key}: duplicate target {rename.new}")
            targets.add(rename.new)
            if not rename.old.is_file():
                problems.append(f"{move.key}: source missing: {rename.old}")
            if rename.new.exists() and not _is_case_only_rename(rename.old, rename.new):
                problems.append(f"{move.key}: target already exists: {rename.new}")
    overlap = sources & targets
    for path in sorted(overlap):
        problems.append(f"path is both a source and a target: {path}")
    return problems


def apply_plan(journal: Journal) -> ApplyResult:
    """Apply a journaled plan; families already done (or found complete
    on disk after a crash) are skipped or recovered, never redone."""
    result = ApplyResult()
    done = journal.done_keys()
    is_copy = journal.kind == "copy"
    with archive_lock(journal.root):
        for move in journal.moves:
            if move.key in done:
                result.skipped.append(move.key)
                continue
            if _already_applied(move, is_copy, journal.algorithm):
                # a crash after the last change but before mark_done
                error = _record_done(journal, move.key)
                if error is None:
                    result.applied.append(move.key)
                else:
                    result.failed.append((move.key, error))
                continue
            error = _copy_family(move) if is_copy else _apply_family(move)
            if error is None:
                error = _record_done(journal, move.key)
            if error is None:
                result.applied.append(move.key)
            else:
                result.failed.append((move.key, error))
    return result


def undo_journal(journal: Journal) -> ApplyResult:
    """Revert every done family of a journal, most recent first.

    Undoing a rename renames back; undoing a copy deletes the copies —
    after verifying each destination still matches the recorded digest,
    so an edited or replaced file is never deleted.
    """
    result = ApplyResult()
    done = journal.done_keys()
    with archive_lock(journal.root):
        for move in reversed(journal.moves):
            if move.key not in done:
                result.skipped.append(move.key)
                continue
            if journal.kind == "copy":
                error = _delete_copies(move, journal.algorithm)
            else:
                reverted = FamilyMove(
                    key=move.key,
                    renames=tuple(
                        Rename(old=rename.new, new=rename.old) for rename in reversed(move.renames)
                    ),
                )
                error = _apply_family(reverted)
            if error is None:
                journal.mark_undone(move.key)
                result.applied.append(move.key)
            else:
                result.failed.append((move.key, error))
    return result


def _record_done(journal: Journal, key: str) -> str | None:
    try:
        journal.mark_done(key)
        return None
    except OSError as error:
        return (
            f"applied on disk but could not be recorded in the done-log: {error}"
            " — fix the journal location before resuming"
        )


def _already_applied(move: FamilyMove, is_copy: bool, algorithm: str) -> bool:
    """Whether a crash completed this family without marking it done."""
    for rename in move.renames:
        if not rename.new.is_file():
            return False
        if not is_copy and rename.old.exists():
            return False
        if is_copy and rename.digest is not None:
            actual = compute_digests(rename.new, [algorithm])[algorithm]
            if actual != rename.digest:
                return False
    return True


def _apply_family(move: FamilyMove) -> str | None:
    """Rename one family all-or-nothing; returns an error message on failure."""
    completed: list[tuple[Path, Path]] = []
    for rename in move.renames:
        try:
            _no_clobber_rename(rename.old, rename.new)
        except OSError as error:
            for done_old, done_new in reversed(completed):
                try:
                    _no_clobber_rename(done_new, done_old)
                except OSError as rollback_error:
                    return (
                        f"{error}; ROLLBACK ALSO FAILED for {done_new}: {rollback_error}"
                        " — family is in a mixed state, restore from the journal manually"
                    )
            return str(error)
        completed.append((rename.old, rename.new))
    return None


def _copy_family(move: FamilyMove) -> str | None:
    """Copy one family all-or-nothing; sources are never touched."""
    completed: list[Path] = []
    for rename in move.renames:
        try:
            _no_clobber_copy(rename.old, rename.new)
        except OSError as error:
            for copied in reversed(completed):
                copied.unlink(missing_ok=True)
            return str(error)
        completed.append(rename.new)
    for directory in {path.parent for path in completed}:
        _fsync_best_effort(directory)
    return None


def _delete_copies(move: FamilyMove, algorithm: str) -> str | None:
    for rename in move.renames:
        if rename.digest is None or not rename.new.is_file():
            continue
        try:
            actual = compute_digests(rename.new, [algorithm])[algorithm]
        except OSError as error:
            return f"cannot verify {rename.new}: {error}"
        if actual != rename.digest:
            return (
                f"{rename.new} no longer matches the copied content"
                " (edited or replaced since the import); refusing to delete it"
            )
    for rename in reversed(move.renames):
        try:
            rename.new.unlink(missing_ok=True)
        except OSError as error:
            return str(error)
    return None


def _is_case_only_rename(old: Path, new: Path) -> bool:
    """On case-insensitive filesystems, .FP2 -> .fp2 'exists' as itself."""
    if old.name.lower() != new.name.lower() or old.parent != new.parent:
        return False
    try:
        return old.exists() and new.exists() and old.samefile(new)
    except OSError:
        return False


def _no_clobber_rename(old: Path, new: Path) -> None:
    """Rename without ever overwriting an existing file.

    POSIX ``os.rename`` silently replaces its target, so the target is
    claimed atomically with a hard link first wherever the platform
    allows; Windows ``os.rename`` already refuses existing targets.
    """
    new.parent.mkdir(parents=True, exist_ok=True)
    if _is_case_only_rename(old, new):
        os.rename(old, new)
        return
    if sys.platform == "win32":
        os.rename(old, new)  # refuses an existing target natively
        return
    try:
        os.link(old, new)
    except FileExistsError:
        raise FileExistsError(f"target already exists: {new}") from None
    except OSError:
        # filesystem without hard links: fall back to check-then-rename
        if new.exists():
            raise FileExistsError(f"target already exists: {new}") from None
        os.rename(old, new)
        return
    os.unlink(old)


def _no_clobber_copy(old: Path, new: Path) -> None:
    """Copy with an atomic claim on the final name and durable bytes.

    The final name is claimed with an exclusive create so a concurrent
    writer fails loudly; the data lands under a unique scratch name, is
    fsynced, and only then replaces the claim.
    """
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(new, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        raise FileExistsError(f"target already exists: {new}") from None
    scratch = new.with_name(f"{new.name}.{os.getpid()}.part")
    try:
        shutil.copy2(old, scratch)
        if scratch.stat().st_size != old.stat().st_size:
            raise OSError(f"size mismatch copying {old}")
        # Windows can only fsync a writable handle
        fd = os.open(scratch, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(scratch, new)
    except OSError:
        scratch.unlink(missing_ok=True)
        new.unlink(missing_ok=True)  # remove the empty claim
        raise


def _fsync_best_effort(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
