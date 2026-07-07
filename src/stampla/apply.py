"""Executing rename plans: validate globally, apply per-family, undo.

The order of protections:

1. **Global validation before any I/O.** Every source must exist, no two
   renames may share a source or a target, no target may already exist
   on disk, and everything must stay inside the archive root. One
   problem anywhere means nothing is touched.
2. **Write-ahead journal.** The complete plan is persisted before the
   first rename (see :mod:`stampla.journal`).
3. **Per-family atomicity.** A family's renames either all happen or —
   if one fails midway — the already-done ones are reverted on the spot
   and the family is reported as failed; other families proceed.
4. **Undo.** Done families can be reverted in reverse order, with the
   same no-clobber checks.

Renames never overwrite: a target that exists is a refusal, not a
replacement.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from stampla.journal import FamilyMove, Journal, Rename


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


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
    root = root.resolve()
    for move in moves:
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
    """Apply a journaled plan; families already in the done-log are skipped."""
    result = ApplyResult()
    done = journal.done_keys()
    run_family = _copy_family if journal.kind == "copy" else _apply_family
    for move in journal.moves:
        if move.key in done:
            result.skipped.append(move.key)
            continue
        error = run_family(move)
        if error is None:
            journal.mark_done(move.key)
            result.applied.append(move.key)
        else:
            result.failed.append((move.key, error))
    return result


def undo_journal(journal: Journal) -> ApplyResult:
    """Revert every done family of a journal, most recent first.

    Undoing a rename renames back; undoing a copy deletes the copies
    (the sources never moved).
    """
    result = ApplyResult()
    done = journal.done_keys()
    for move in reversed(journal.moves):
        if move.key not in done:
            result.skipped.append(move.key)
            continue
        if journal.kind == "copy":
            error = _delete_copies(move)
        else:
            reverted = FamilyMove(
                key=move.key,
                renames=tuple(
                    Rename(old=rename.new, new=rename.old) for rename in reversed(move.renames)
                ),
            )
            error = _apply_family(reverted)
        if error is None:
            journal.clear_done(move.key)
            result.applied.append(move.key)
        else:
            result.failed.append((move.key, error))
    return result


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
    return None


def _delete_copies(move: FamilyMove) -> str | None:
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
    """Rename without ever overwriting an existing file."""
    if new.exists() and not _is_case_only_rename(old, new):
        raise FileExistsError(f"target already exists: {new}")
    new.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old, new)


def _no_clobber_copy(old: Path, new: Path) -> None:
    """Copy via a scratch name so a torn copy never bears the final name."""
    if new.exists():
        raise FileExistsError(f"target already exists: {new}")
    new.parent.mkdir(parents=True, exist_ok=True)
    scratch = new.with_name(new.name + ".part")
    try:
        shutil.copy2(old, scratch)
        if scratch.stat().st_size != old.stat().st_size:
            raise OSError(f"size mismatch copying {old}")
        os.replace(scratch, new)
    except OSError:
        scratch.unlink(missing_ok=True)
        raise
