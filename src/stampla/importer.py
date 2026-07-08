"""The import command: memory card to archive, named on arrival.

Files on the card are grouped by original base name (master plus its
sidecars and labeled derivatives), the master's capture time and content
hash produce the family's prefix, and every member is **copied** into
the tree's layout directory under its canonical name. Sources on the
card are never modified or deleted — the card remains the last-resort
backup until it is formatted in the camera.

After copying, every master is re-hashed at its destination and compared
against the digest computed from the card, so a transfer error cannot go
unnoticed. Groups that cannot be imported safely (unresolvable capture
time, target already present, no recognizable master) are reported and
skipped; they never abort the rest of the card.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from stampla.apply import apply_plan, validate_plan
from stampla.config import Config, Tree
from stampla.dates import ResolvedDate, chain_tags, resolve_date
from stampla.digests import naming_digests
from stampla.exiftool import ExifTool
from stampla.family import OriginalGroup, group_originals
from stampla.hashing import compute_digests, hash_files
from stampla.journal import FamilyMove, Journal, Rename
from stampla.report import Bucket, Finding, Report


@dataclass
class ImportPlan:
    algorithm: str
    moves: tuple[FamilyMove, ...] = ()
    report: Report = field(default_factory=Report)
    #: expected digest of each copied master at its destination
    expected: dict[Path, str] = field(default_factory=dict)
    #: which metadata tag dated each planned master
    date_sources: dict[Path, str] = field(default_factory=dict)


def build_plan(config: Config, root: Path, card: Path, workers: int | None = None) -> ImportPlan:
    """Work out every copy the card calls for, without touching anything."""
    if not card.is_dir():
        raise ValueError(f"card path is not a directory: {card}")
    plan = ImportPlan(algorithm=config.pattern.digest)
    report = plan.report

    files: list[Path] = []
    for path in sorted(card.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(card)
        if any(part.startswith(".") for part in relative.parts):
            report.add(Finding(Bucket.IGNORED, path, "hidden path; not imported"))
            continue
        if _matches(relative, config.import_ignore):
            report.add(Finding(Bucket.IGNORED, path, "matches an import ignore pattern"))
            continue
        files.append(path)
    report.scanned = len(files)
    camera_extensions = config.camera_extensions
    groups = group_originals(files, config.sidecar_dirs, camera_extensions)
    report.families = len(groups)

    masters: dict[tuple[Path, str], Path] = {}
    for group in groups:
        master = _master_of(group, config, camera_extensions)
        if master is None:
            report.add(
                Finding(
                    Bucket.ORPHAN_FAMILY,
                    group.members[0],
                    f"no master in group {group.base!r}; not imported",
                    related=group.members[1:],
                )
            )
            continue
        masters[(group.directory, group.base)] = master

    master_paths = sorted(masters.values())
    tags = sorted(chain_tags(config.date_chain_photo + config.date_chain_video))
    with ExifTool() as tool:
        metadata = tool.read_metadata(master_paths, tags) if master_paths else {}
        naming, naming_errors = naming_digests(master_paths, config.pattern, tool, workers=workers)
    digests, hash_errors = hash_files(files, [plan.algorithm], workers=workers)

    moves: list[FamilyMove] = []
    for group in groups:
        master = masters.get((group.directory, group.base))
        if master is None:
            continue
        is_video = master.suffix.lstrip(".").lower() in config.video_extensions
        tree = _tree_for(config, "video" if is_video else "photo")
        if tree is None:
            report.add(Finding(Bucket.UNNAMED, master, "no tree configured for this media kind"))
            continue
        broken = [m for m in group.members if m in hash_errors] + (
            [master] if master in naming_errors else []
        )
        if broken:
            first = broken[0]
            detail = naming_errors.get(first) or hash_errors.get(first, "")
            report.add(
                Finding(
                    Bucket.HASH_ERROR,
                    first,
                    f"unreadable; group {group.base!r} not imported: {detail}",
                    related=tuple(m for m in group.members if m != first),
                )
            )
            continue
        chain = config.date_chain_video if is_video else config.date_chain_photo
        resolved = resolve_date(metadata.get(master, {}), chain, config.tzinfo)
        if not isinstance(resolved, ResolvedDate):
            report.add(Finding(Bucket.UNRESOLVED_DATE, master, resolved.reason))
            continue
        plan.date_sources[master] = resolved.source

        members = group.members
        if config.skip_jpeg_twins and master.suffix.lstrip(".").lower() in config.raw_extensions:
            twins = tuple(m for m in members if _is_jpeg_twin(m, group.base))
            members = tuple(m for m in members if m not in twins)
            for twin in twins:
                report.add(Finding(Bucket.IGNORED, twin, "JPEG twin of a RAW; skipped by policy"))
        prefix = config.pattern.build_prefix(resolved.value, naming[master])
        destination = root / tree.path / _render_layout(tree.layout, resolved)
        trimmed = OriginalGroup(directory=group.directory, base=group.base, members=members)
        renames = _member_targets(trimmed, prefix, destination)
        if any(rename.new.exists() for rename in renames):
            problems = _compare_with_archive(renames, digests, plan.algorithm)
            if problems:
                report.add(
                    Finding(
                        Bucket.COLLISION,
                        master,
                        "in archive but NOT identical: " + "; ".join(problems),
                        related=tuple(m for m in members if m != master),
                    )
                )
            else:
                report.add(
                    Finding(
                        Bucket.ALREADY_IMPORTED,
                        master,
                        f"identical content already in archive ({destination / prefix}*)",
                        related=tuple(m for m in members if m != master),
                    )
                )
            continue
        recorded = tuple(
            Rename(old=r.old, new=r.new, digest=digests[r.old][plan.algorithm]) for r in renames
        )
        moves.append(FamilyMove(key=prefix, renames=recorded))
        for rename in recorded:
            assert rename.digest is not None
            plan.expected[rename.new] = rename.digest
        report.ok += 1

    plan.moves = tuple(moves)
    return plan


def apply_import(plan: ImportPlan, root: Path, journal_dir: Path | None = None) -> Report:
    """Copy a built plan into the archive and verify the copies."""
    report = plan.report
    if not plan.moves:
        return report
    problems = validate_plan(plan.moves, root, sources_outside_root=True)
    if problems:
        raise ValueError("plan failed validation:\n" + "\n".join(problems))

    journal = Journal.create(
        root, plan.moves, directory=journal_dir, kind="copy", algorithm=plan.algorithm
    )
    print(f"journal: {journal.path}", file=sys.stderr)
    result = apply_plan(journal)
    for key, error in result.failed:
        report.ok -= 1
        report.add(Finding(Bucket.APPLY_FAILED, Path(key), f"import failed: {error}"))

    failed_targets = {
        rename.new
        for move in plan.moves
        if move.key in {key for key, _ in result.failed}
        for rename in move.renames
    }
    for target, expected in sorted(plan.expected.items()):
        if target in failed_targets:
            continue  # its group failed above, already reported
        if not target.is_file():
            report.add(
                Finding(
                    Bucket.CORRUPTION,
                    target,
                    "copied file is missing at verification time — do not format the card",
                )
            )
            continue
        actual = compute_digests(target, [plan.algorithm])[plan.algorithm]
        if actual != expected:
            report.add(
                Finding(
                    Bucket.CORRUPTION,
                    target,
                    "copy verification failed: destination content differs from the card"
                    " — do not format the card",
                )
            )
    return report


def _matches(relative: Path, patterns: tuple[str, ...]) -> bool:
    # memory cards use case-insensitive filesystems, so ignore patterns
    # match case-insensitively: "*.jpg" covers DSC_0001.JPG
    posix = relative.as_posix().lower()
    name = relative.name.lower()
    return any(
        fnmatchcase(posix, pattern.lower()) or fnmatchcase(name, pattern.lower())
        for pattern in patterns
    )


def _is_jpeg_twin(member: Path, base: str) -> bool:
    """A plain <base>.jpg next to a RAW master — the in-camera preview."""
    stem, dot, extension = member.name.partition(".")
    return stem == base and dot == "." and extension.lower() in {"jpg", "jpeg"}


def _tree_for(config: Config, media: str) -> Tree | None:
    return next((tree for tree in config.trees if tree.media == media), None)


def _master_of(
    group: OriginalGroup, config: Config, camera_extensions: frozenset[str]
) -> Path | None:
    def base_named(member: Path) -> bool:
        return member.name.split(".", 1)[0] == group.base

    candidates = [
        member
        for member in group.members
        if base_named(member) and member.suffix.lstrip(".").lower() in camera_extensions
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # RAW+JPEG-style twins: the raw is the master
        raws = [
            member
            for member in candidates
            if member.suffix.lstrip(".").lower() in config.raw_extensions
        ]
        return raws[0] if len(raws) == 1 else None
    # a lone photo without a camera-native RAW (a standalone DNG or a
    # JPEG-only shot) is its own master
    loose = [
        member
        for member in group.members
        if base_named(member) and member.suffix.lstrip(".").lower() in {"jpg", "jpeg", "dng"}
    ]
    return loose[0] if len(loose) == 1 else None


def _member_targets(group: OriginalGroup, prefix: str, destination: Path) -> list[Rename]:
    """Where every group member would land in the archive."""
    renames: list[Rename] = []
    for member in group.members:
        remainder = member.name[len(group.base) :]
        suffix, dot, extensions = remainder.partition(".")
        new_name = f"{prefix}{suffix}{dot}{extensions.lower()}"
        in_sidecar_dir = member.parent != group.directory
        target_dir = destination / member.parent.name if in_sidecar_dir else destination
        renames.append(Rename(old=member, new=target_dir / new_name))
    return renames


def _compare_with_archive(
    renames: list[Rename], digests: dict[Path, dict[str, str]], algorithm: str
) -> list[str]:
    """How a partially/fully imported group differs from the archive.

    Empty means every member already sits in the archive with identical
    content — the card copy is redundant and safe to lose.
    """
    problems: list[str] = []
    for rename in renames:
        if not rename.new.is_file():
            problems.append(f"{rename.new.name} is missing from the archive")
            continue
        card_digest = digests.get(rename.old, {}).get(algorithm)
        if card_digest is None:
            problems.append(f"{rename.old.name} could not be hashed")
            continue
        archive_digest = compute_digests(rename.new, [algorithm])[algorithm]
        if archive_digest != card_digest:
            problems.append(f"{rename.new.name} differs from the card version")
    return problems


def _render_layout(layout: str, resolved: ResolvedDate) -> Path:
    value = resolved.value
    rendered = (
        layout.replace("{yyyy}", f"{value.year:04d}")
        .replace("{mm}", f"{value.month:02d}")
        .replace("{dd}", f"{value.day:02d}")
    )
    return Path(rendered)
