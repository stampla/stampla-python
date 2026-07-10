"""The relocate command: put named files where their names say they belong.

A tree's layout maps each capture time to a directory, and the capture
time is in the name — so after a layout change (or a hand-move gone
wrong) the correct shelf for every named group is derivable, and
relocate moves whole groups there through the same validated,
write-ahead-journaled apply as rename.

Two kinds of tree are never moved:

- **DAM-managed trees.** The DAM's catalog tracks files by path; moving
  them behind its back orphans the catalog entries. Relocate reports
  the misplacement and emits a folder checklist to execute *inside* the
  DAM (Lightroom Classic: drag in the Folders panel), where moves are
  catalog-safe. Explicitly targeting a DAM-managed tree with ``apply``
  is an error, never a partial run.
- **``{shoot}`` segments.** A shoot is chosen at import and recorded
  nowhere else, so a shoot directory can never be derived back from a
  name. Groups misplaced *around* the shoot segment (say, the wrong
  year) are reported, not moved.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from stampla.apply import apply_plan, validate_plan
from stampla.config import Config
from stampla.group import group_by_prefix
from stampla.journal import GroupMove, Journal, Rename
from stampla.places import group_placement
from stampla.progress import Monitor
from stampla.report import Bucket, Finding, Report
from stampla.scan import scan_tree, tree_targets


@dataclass(frozen=True)
class RelocateOptions:
    apply: bool = False
    journal_dir: Path | None = None


def run_relocate(
    config: Config,
    root: Path,
    paths: Sequence[Path] = (),
    options: RelocateOptions | None = None,
    monitor: Monitor | None = None,
) -> tuple[Report, tuple[GroupMove, ...]]:
    """Plan (and with ``apply``, execute) layout moves under ``root``."""
    options = options or RelocateOptions()
    monitor = monitor or Monitor()
    report = Report()
    moves: list[GroupMove] = []
    checklist: dict[tuple[str, str], int] = defaultdict(int)
    shoot_trees_hit: set[str] = set()

    for tree, tree_root, scan_root in tree_targets(config, root, paths):
        dam_managed = config.dam is not None and tree.path in config.dam.trees
        if dam_managed and options.apply and paths:
            raise ValueError(
                f"tree {tree.path!r} is DAM-managed: relocate never moves files the"
                " DAM tracks — its catalog would lose them. Run without --apply for"
                " the folder checklist and move them inside the DAM (Lightroom"
                " Classic: drag in the Folders panel), then verify."
            )
        files = []
        for file in scan_tree(scan_root, config.grammar, config.excludes):
            files.append(file)
            if len(files) % 512 == 0:
                monitor.step("scan", len(files), 0, file.path)
        monitor.step("scan", len(files), 0)
        report.scanned += len(files)
        groups = group_by_prefix(files)
        report.groups += len(groups)

        for group in groups:
            placement = group_placement(group, tree.layout, tree_root)
            if placement is None:
                continue
            home, expected, actual = placement
            if expected.matches(actual):
                report.ok += 1
                continue
            report.add(
                Finding(
                    Bucket.MISPLACED,
                    home,
                    f"sits in {actual}/ but its name belongs in {expected}/",
                    data={"actual": str(actual), "expected": str(expected)},
                )
            )
            if dam_managed:
                checklist[(str(actual), str(expected))] += 1
                continue
            if not expected.derivable:
                shoot_trees_hit.add(tree.path)
                continue
            target_dir = tree_root / expected.path()
            renames = tuple(
                Rename(old=file.path, new=target_dir / file.path.relative_to(home.parent))
                for file in group.members
            )
            if len({rename.new for rename in renames}) != len(renames):
                # one prefix, several homes: merging them would collide
                report.add(
                    Finding(
                        Bucket.COLLISION,
                        home,
                        "the same name exists in more than one place; resolve by"
                        " hand before relocating",
                    )
                )
                continue
            moves.append(GroupMove(key=group.prefix, renames=renames))

    for (from_dir, to_dir), count in sorted(checklist.items()):
        report.hints.append(
            f"DAM-managed: move {from_dir}/ to {to_dir}/ inside the DAM"
            f" (Lightroom Classic: Folders panel), {count} group(s) — never in the Finder"
        )
    for tree_path in sorted(shoot_trees_hit):
        report.hints.append(
            f"tree {tree_path!r} files by shoot; the shoot is not derivable from"
            " names — re-import those groups with --shoot or move them by hand"
        )

    if not options.apply:
        for move in moves:
            first = move.renames[0]
            report.add(
                Finding(
                    Bucket.RELOCATE_PENDING,
                    first.old,
                    f"would move to {first.new.parent}/"
                    + (
                        f" with {len(move.renames) - 1} group member(s)"
                        if len(move.renames) > 1
                        else ""
                    ),
                    data={"target_dir": str(first.new.parent)},
                )
            )
        return report, tuple(moves)

    if moves:
        problems = validate_plan(tuple(moves), root)
        if problems:
            raise ValueError("plan failed validation:\n" + "\n".join(problems))
        journal = Journal.create(
            root, tuple(moves), directory=options.journal_dir, command="relocate"
        )
        print(f"journal: {journal.path}", file=sys.stderr)
        result = apply_plan(journal, monitor=monitor)
        for key, error in result.failed:
            report.add(Finding(Bucket.APPLY_FAILED, Path(key), f"relocate failed: {error}"))
        applied = set(result.applied)
        for move in moves:
            if move.key in applied:
                first = move.renames[0]
                report.add(
                    Finding(
                        Bucket.RELOCATED,
                        first.old,
                        f"now in {first.new.parent}/",
                        data={"target_dir": str(first.new.parent)},
                    )
                )
    return report, tuple(moves)
