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
from stampla.dates import NAME_TIMESTAMP_TAG
from stampla.importer import ImportPlan, build_plan
from stampla.progress import Monitor
from stampla.report import Bucket, Finding, Report

MTIME_TAG = "File:FileModifyDate"


def run_organize(
    config: Config,
    root: Path,
    path: Path,
    workers: int | None = None,
    monitor: Monitor | None = None,
) -> tuple[Report, ImportPlan]:
    """Plan the messy tree as import would, then annotate for triage."""
    fallback_config = replace(
        config,
        date_chain_photo=(*config.date_chain_photo, NAME_TIMESTAMP_TAG, MTIME_TAG),
        date_chain_video=(*config.date_chain_video, NAME_TIMESTAMP_TAG, MTIME_TAG),
    )
    plan = build_plan(fallback_config, root, path, workers=workers, monitor=monitor)
    report = plan.report

    for master, source in sorted(plan.date_sources.items()):
        if source == NAME_TIMESTAMP_TAG:
            report.add(
                Finding(
                    Bucket.NAME_DATED,
                    master,
                    "no capture time in metadata; proposal uses the timestamp from the filename",
                )
            )
        elif source == MTIME_TAG:
            report.add(
                Finding(
                    Bucket.MTIME_DATED,
                    master,
                    "no capture time in metadata; proposal uses the file's"
                    " modification time — confirm before importing",
                )
            )

    # import plans one copy of duplicated content and marks the others;
    # for triage every member of such a cluster deserves a collision row
    clusters: dict[Path, list[Path]] = defaultdict(list)
    for finding in list(report.findings):
        data = finding.data or {}
        if data.get("reason") == "card-duplicate":
            clusters[Path(str(data["duplicate_of"]))].append(finding.path)
    for kept, duplicates in sorted(clusters.items()):
        members = [kept, *duplicates]
        for master in members:
            report.add(
                Finding(
                    Bucket.COLLISION,
                    master,
                    f"identical content as {len(members) - 1} other group(s) here",
                    related=tuple(m for m in members if m != master),
                )
            )
    return report, plan
