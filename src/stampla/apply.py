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
5. **Per-group atomicity.** A group's renames either all happen or —
   if one fails midway — the already-done ones are reverted on the spot.
6. **Resume and verified undo.** Re-applying a journal skips done
   groups and recognizes groups a crash completed but never marked.
   Undoing a copy journal re-hashes each destination and refuses to
   delete anything that is no longer the copy this run made.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from stampla.hashing import compute_digests
from stampla.journal import GroupMove, Journal, Rename
from stampla.progress import Monitor


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
    moves: tuple[GroupMove, ...], root: Path, sources_outside_root: bool = False
) -> list[str]:
    """All problems that make the plan unsafe; empty list means go.

    ``sources_outside_root`` fits copy plans, where sources live on a
    memory card; targets must always stay inside the archive root.
    """
    problems: list[str] = []
    sources: set[Path] = set()
    targets: set[Path] = set()
    folded_targets: dict[str, Path] = {}
    keys: set[str] = set()
    root = root.resolve()
    for move in moves:
        if move.key in keys:
            problems.append(f"duplicate journal key: {move.key}")
        keys.add(move.key)
        if not move.renames:
            problems.append(f"{move.key}: empty group move")
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
            # archives travel between filesystems, so two targets that
            # differ only by case must never both be planned: on APFS or
            # exFAT the second would collide mid-apply
            folded = str(rename.new).casefold()
            other = folded_targets.setdefault(folded, rename.new)
            if other != rename.new:
                problems.append(
                    f"{move.key}: target collides with {other} on a"
                    f" case-insensitive filesystem: {rename.new}"
                )
            if not rename.old.is_file():
                problems.append(f"{move.key}: source missing: {rename.old}")
            if rename.new.exists() and not _is_case_only_rename(rename.old, rename.new):
                problems.append(f"{move.key}: target already exists: {rename.new}")
    # WindowsPath comparison folds case, so a case-only rename's own
    # source and target compare equal there; that pair is not a conflict.
    case_only: set[Path] = set()
    for move in moves:
        for rename in move.renames:
            if _is_case_only_rename(rename.old, rename.new):
                case_only.add(rename.old)
                case_only.add(rename.new)
    overlap = (sources & targets) - case_only
    for path in sorted(overlap):
        problems.append(f"path is both a source and a target: {path}")
    return problems


def apply_plan(journal: Journal, monitor: Monitor | None = None) -> ApplyResult:
    """Apply a journaled plan; groups already done (or found complete
    on disk after a crash) are skipped or recovered, never redone.

    The monitor sees one event per group and can cancel between
    groups — an interruption in the journal's own terms: finish with
    resume, or revert with undo.
    """
    monitor = monitor or Monitor()
    result = ApplyResult()
    done = journal.done_keys()
    is_copy = journal.kind == "copy"
    with archive_lock(journal.root):
        for index, move in enumerate(journal.moves):
            # cancel only between groups; done counts completed ones
            monitor.step(
                journal.kind,
                index,
                len(journal.moves),
                move.renames[0].old if move.renames else None,
            )
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
            error = _containment_error(move, journal.root)
            if error is None:
                error = _copy_group(move, journal.algorithm) if is_copy else _apply_group(move)
            if error is None:
                error = _record_done(journal, move.key)
            if error is None:
                result.applied.append(move.key)
            else:
                result.failed.append((move.key, error))
        monitor.emit(journal.kind, len(journal.moves), len(journal.moves))
    return result


def undo_journal(journal: Journal, monitor: Monitor | None = None) -> ApplyResult:
    """Revert every done group of a journal, most recent first.

    Undoing a rename renames back; undoing a copy deletes the copies —
    after verifying each destination still matches the recorded digest,
    so an edited or replaced file is never deleted.
    """
    monitor = monitor or Monitor()
    result = ApplyResult()
    done = journal.done_keys()
    with archive_lock(journal.root):
        for index, move in enumerate(reversed(journal.moves)):
            monitor.step(
                "undo",
                index,
                len(journal.moves),
                move.renames[0].new if move.renames else None,
            )
            if move.key not in done:
                if journal.kind != "copy":
                    # a crash mid-group leaves renames done but the group
                    # unmarked; those must be reverted, not skipped — a
                    # master split from its sidecars is the worst state
                    # to leave behind while reporting "ok"
                    undone, error = _revert_partial(move)
                    if error is not None:
                        result.failed.append((move.key, error))
                    elif undone:
                        result.applied.append(move.key)
                    else:
                        result.skipped.append(move.key)
                else:
                    result.skipped.append(move.key)
                continue
            if journal.kind == "copy":
                error = _delete_copies(move, journal.algorithm)
            else:
                reverted = GroupMove(
                    key=move.key,
                    renames=tuple(
                        Rename(old=rename.new, new=rename.old) for rename in reversed(move.renames)
                    ),
                )
                error = _apply_group(reverted)
            if error is None:
                journal.mark_undone(move.key)
                result.applied.append(move.key)
            else:
                result.failed.append((move.key, error))
        monitor.emit("undo", len(journal.moves), len(journal.moves))
    return result


def _containment_error(move: GroupMove, root: Path) -> str | None:
    """Re-check at apply time that no target escapes the root.

    Plan-time validation resolved these paths once; a directory swapped
    for a symlink since then must not let a rename land outside.
    """
    resolved_root = root.resolve()
    for rename in move.renames:
        if not rename.new.resolve().is_relative_to(resolved_root):
            return f"target escapes the root at apply time: {rename.new}"
    return None


def _record_done(journal: Journal, key: str) -> str | None:
    try:
        journal.mark_done(key)
        return None
    except OSError as error:
        return (
            f"applied on disk but could not be recorded in the done-log: {error}"
            " — fix the journal location before resuming"
        )


def _already_applied(move: GroupMove, is_copy: bool, algorithm: str) -> bool:
    """Whether a crash completed this group without marking it done."""
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


def _rename_crash_completed(rename: Rename) -> bool:
    """Whether an earlier interrupted run already performed this rename.

    Source gone with the target in place is the fingerprint of a rename
    done before its group could be marked; source and target existing as
    the *same file* is the window between the hard-link claim and the
    source unlink, finished here by dropping the leftover source link.
    Case-only renames are excluded — their two names are one directory
    entry, so there is no crash window and unlinking would lose the file.
    """
    if _is_case_only_rename(rename.old, rename.new):
        return False
    try:
        if not rename.old.exists() and rename.new.is_file():
            return True
        if rename.old.exists() and rename.new.exists() and rename.old.samefile(rename.new):
            os.unlink(rename.old)
            return True
    except OSError:
        return False
    return False


def _revert_partial(move: GroupMove) -> tuple[int, str | None]:
    """Revert the renames a crash completed in a never-marked group."""
    undone = 0
    for rename in reversed(move.renames):
        if _is_case_only_rename(rename.old, rename.new):
            continue
        try:
            if not rename.old.exists() and rename.new.is_file():
                _no_clobber_rename(rename.new, rename.old)
                undone += 1
            elif rename.old.exists() and rename.new.exists() and rename.old.samefile(rename.new):
                os.unlink(rename.new)  # drop the leftover hard-link claim
                undone += 1
        except OSError as error:
            return undone, (
                f"could not revert the partially applied group: {error}"
                " — finish it with resume instead"
            )
    return undone, None


def _apply_group(move: GroupMove) -> str | None:
    """Rename one group all-or-nothing; returns an error message on failure.

    A rename an interrupted run already performed is recognized and
    carried forward, so resume completes a half-done group instead of
    failing on its own leftovers.
    """
    completed: list[tuple[Path, Path]] = []
    for rename in move.renames:
        if _rename_crash_completed(rename):
            completed.append((rename.old, rename.new))
            continue
        try:
            _no_clobber_rename(rename.old, rename.new)
        except OSError as error:
            for done_old, done_new in reversed(completed):
                try:
                    _no_clobber_rename(done_new, done_old)
                except OSError as rollback_error:
                    return (
                        f"{error}; ROLLBACK ALSO FAILED for {done_new}: {rollback_error}"
                        " — group is in a mixed state, restore from the journal manually"
                    )
            return str(error)
        completed.append((rename.old, rename.new))
    return None


def _copy_group(move: GroupMove, algorithm: str) -> str | None:
    """Copy one group all-or-nothing; sources are never touched.

    Every copy that carries an expected digest is re-hashed at its
    destination before the group can be marked done — the promise that a
    transfer error cannot go unnoticed holds on *every* path that copies,
    resume included, not just a full import run.
    """
    completed: list[Path] = []

    def fail(message: str) -> str:
        for copied in reversed(completed):
            copied.unlink(missing_ok=True)
        return message

    for rename in move.renames:
        try:
            _no_clobber_copy(rename.old, rename.new)
        except OSError as error:
            return fail(str(error))
        completed.append(rename.new)
        if rename.digest is not None:
            try:
                actual = compute_digests(rename.new, [algorithm])[algorithm]
            except OSError as error:
                return fail(f"cannot verify {rename.new} after copying: {error}")
            if actual != rename.digest:
                return fail(
                    f"{rename.new} does not match the source digest after copying"
                    " — transfer error; the source is untouched"
                )
    for directory in {path.parent for path in completed}:
        _fsync_best_effort(directory)
    return None


def _delete_copies(move: GroupMove, algorithm: str) -> str | None:
    missing = [
        rename.new
        for rename in move.renames
        if rename.digest is not None and not rename.new.is_file()
    ]
    if missing:
        # a destination that vanished was almost certainly renamed or
        # relocated since the import; calling this group "reverted"
        # while its files live on in the archive would be a lie
        listed = ", ".join(str(path) for path in missing[:3])
        if len(missing) > 3:
            listed += ", …"
        return (
            f"{len(missing)} of {len(move.renames)} copied file(s) no longer exist"
            f" ({listed}) — probably renamed since; undo cannot revert this import"
        )
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
    _drop_stale_scratch(new)
    try:
        os.close(os.open(new, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        try:
            existing_size = new.stat().st_size
        except OSError:
            existing_size = -1
        if existing_size != 0:
            raise FileExistsError(f"target already exists: {new}") from None
        # a zero-byte file at exactly the target is this tool's own claim
        # from an interrupted run — the archive lock excludes concurrent
        # stamplas — so it is reused rather than wedging resume forever
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
    except BaseException:
        # BaseException: a Ctrl-C mid-copy must not leak the claim either
        scratch.unlink(missing_ok=True)
        with suppress(OSError):
            if new.stat().st_size == 0:
                new.unlink()  # remove the empty claim, never a full copy
        raise


def _drop_stale_scratch(new: Path) -> None:
    """Remove ``<name>.<pid>.part`` leftovers from interrupted runs.

    The archive lock excludes concurrent stamplas, so any scratch file
    for this target belongs to a run that is no longer alive.
    """
    try:
        siblings = os.listdir(new.parent)
    except OSError:
        return
    for name in siblings:
        if name.startswith(new.name + ".") and name.endswith(".part"):
            with suppress(OSError):
                (new.parent / name).unlink()


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
