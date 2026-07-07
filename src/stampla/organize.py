"""The organize command: triage a messy tree, touch absolutely nothing.

Old dumps accumulate files no automated rule should rename: photos with
stripped metadata, duplicates of duplicates, exports with fabricated
dates. organize runs the import planning pipeline over such a tree and
produces a report designed for working through it by hand:

- what each recognizable group *would* be named and where it would live,
- which files are already in the archive (byte-identical) or clash with
  it,
- which masters could only be dated from the file's modification time —
  proposed, but flagged: mtime is hearsay, not evidence,
- which groups share identical content with each other (duplicate
  clusters),
- what remains unresolvable.

There is no ``--apply``. When a batch looks right, import it with the
import command; organize itself never moves anything.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from stampla.config import Config
from stampla.importer import ImportPlan, build_plan
from stampla.report import Bucket, Finding, Report

MTIME_TAG = "File:FileModifyDate"


def run_organize(
    config: Config, root: Path, path: Path, workers: int | None = None
) -> tuple[Report, ImportPlan]:
    """Plan the messy tree as import would, then annotate for triage."""
    fallback_config = replace(
        config,
        date_chain_photo=(*config.date_chain_photo, MTIME_TAG),
        date_chain_video=(*config.date_chain_video, MTIME_TAG),
    )
    plan = build_plan(fallback_config, root, path, workers=workers)
    report = plan.report

    for master, source in sorted(plan.date_sources.items()):
        if source == MTIME_TAG:
            report.add(
                Finding(
                    Bucket.MTIME_DATED,
                    master,
                    "no capture time in metadata; proposal uses the file's"
                    " modification time — confirm before importing",
                )
            )

    owners: dict[str, list[Path]] = defaultdict(list)
    for move in plan.moves:
        first = next((r.old for r in move.renames), None)
        if first is not None:
            owners[move.key].append(first)
    for prefix, masters in sorted(owners.items()):
        if len(masters) > 1:
            for master in masters:
                report.add(
                    Finding(
                        Bucket.COLLISION,
                        master,
                        f"identical content as {len(masters) - 1} other group(s)"
                        f" here (all derive {prefix})",
                        related=tuple(m for m in masters if m != master),
                    )
                )
    return report, plan
