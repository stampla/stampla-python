"""The import command: memory card to archive, named on arrival.

Files on the card are grouped by original base name (master plus its
sidecars and labeled derivatives), the master's capture time and content
hash produce the group's prefix, and every member is **copied** into
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

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from stampla.apply import apply_plan, validate_plan
from stampla.config import LOOSE_MASTER_EXTENSIONS, Config, Tree
from stampla.dates import (
    ResolvedDate,
    augment_with_name_timestamps,
    chain_tags,
    resolve_date,
)
from stampla.digests import naming_digests
from stampla.exiftool import ExifTool
from stampla.group import CardGroup, group_originals
from stampla.hashing import compute_digests, hash_files
from stampla.journal import GroupMove, Journal, Rename
from stampla.progress import Monitor
from stampla.report import Bucket, Finding, Report


@dataclass
class ImportPlan:
    algorithm: str
    moves: tuple[GroupMove, ...] = ()
    report: Report = field(default_factory=Report)
    #: expected digest of each copied master at its destination
    expected: dict[Path, str] = field(default_factory=dict)
    #: which metadata tag dated each planned master
    date_sources: dict[Path, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportVerdict:
    """An applied import's bottom line: is the card fully accounted for?

    ``safe_to_format`` is the README's promise made structural: every
    card file was copied and verified, already sat in the archive
    byte-identical, or is explicitly ignored. Anything else — an
    unresolvable date, a same-name collision, a failed copy — makes it
    false, exactly when the exit code reports a problem.
    """

    safe_to_format: bool
    imported: int
    already_imported: int
    ignored: int


def verdict_of(report: Report, applied: bool, whole_card: bool = True) -> ImportVerdict | None:
    """The verdict of an applied import run; ``None`` for a dry run.

    A selective import (``only`` paths) also gets ``None``: the verdict
    speaks for the whole card, and a run that never examined the whole
    card must not clear it for formatting.
    """
    if not applied or not whole_card:
        return None
    return ImportVerdict(
        safe_to_format=not report.has_problems,
        imported=report.ok,
        already_imported=sum(1 for f in report.findings if f.bucket is Bucket.ALREADY_IMPORTED),
        ignored=sum(1 for f in report.findings if f.bucket is Bucket.IGNORED),
    )


def build_plan(
    config: Config,
    root: Path,
    card: Path,
    workers: int | None = None,
    monitor: Monitor | None = None,
    only: Sequence[Path] = (),
    shoot: str | None = None,
) -> ImportPlan:
    """Work out every copy the card calls for, without touching anything.

    ``only`` narrows the plan to directories on the card (a triaged
    batch out of organize, a selection in a front end); files outside
    the selection are out of scope entirely — neither planned nor
    reported.

    ``shoot`` names the shoot/job/session this card belongs to and fills
    the ``{shoot}`` layout token — the shoot lives in the directory, so
    filenames and verification are untouched (see docs/shoots.md for why
    the filename is off limits). A tree whose layout files by shoot
    refuses to import without one: guessing a shoot would file files
    somewhere a later import would not find them.
    """
    if not card.is_dir():
        raise ValueError(f"card path is not a directory: {card}")
    shoot = normalize_shoot(shoot)
    _check_shoot_against_config(config, shoot)
    monitor = monitor or Monitor()
    selection = _selection_roots(card, only)
    plan = ImportPlan(algorithm=config.pattern.digest)
    report = plan.report

    files: list[Path] = []
    for scanned, path in enumerate(sorted(card.rglob("*"))):
        if scanned % 512 == 0:
            monitor.step("scan", scanned, 0, path)
        if not path.is_file():
            continue
        if selection is not None and not any(
            path.resolve().is_relative_to(chosen) for chosen in selection
        ):
            continue
        relative = path.relative_to(card)
        if any(part.startswith(".") for part in relative.parts):
            report.add(Finding(Bucket.IGNORED, path, "hidden path; not imported", data=_HIDDEN))
            continue
        if _matches(relative, config.import_ignore):
            report.add(
                Finding(
                    Bucket.IGNORED,
                    path,
                    "matches an import ignore pattern",
                    data=_IGNORE_PATTERN,
                )
            )
            continue
        files.append(path)
    report.scanned = len(files)
    camera_extensions = config.camera_extensions
    groups = group_originals(files, config.sidecar_dirs, camera_extensions)
    report.groups = len(groups)

    masters: dict[tuple[Path, str], Path] = {}
    for group in groups:
        master = _master_of(group, config, camera_extensions)
        if master is None:
            report.add(
                Finding(
                    Bucket.ORPHAN_GROUP,
                    group.members[0],
                    f"no master in group {group.base!r}; not imported",
                    related=group.members[1:],
                )
            )
            continue
        masters[(group.directory, group.base)] = master

    master_paths = sorted(masters.values())
    tags = sorted(chain_tags(config.date_chain_photo + config.date_chain_video))
    with ExifTool(workers=workers) as tool:
        monitor.step("dates", 0, len(master_paths))
        metadata = tool.read_metadata(master_paths, tags) if master_paths else {}
        augment_with_name_timestamps(metadata, master_paths)
        monitor.step("dates", len(master_paths), len(master_paths))
        naming, naming_errors = naming_digests(
            master_paths, config.pattern, tool, workers=workers, monitor=monitor
        )
    digests, hash_errors = hash_files(files, [plan.algorithm], workers=workers, monitor=monitor)

    moves: list[GroupMove] = []
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
                report.add(
                    Finding(
                        Bucket.IGNORED,
                        twin,
                        "JPEG twin of a RAW; skipped by policy",
                        data=_JPEG_TWIN,
                    )
                )
        prefix = config.pattern.build_prefix(resolved.value, naming[master])
        destination = root / tree.path / _render_layout(tree.layout, resolved, shoot)
        trimmed = CardGroup(directory=group.directory, base=group.base, members=members)
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
                        data={
                            "prefix": prefix,
                            "destination": str(destination.relative_to(root)),
                        },
                    )
                )
            continue
        recorded = tuple(
            Rename(old=r.old, new=r.new, digest=digests[r.old][plan.algorithm]) for r in renames
        )
        moves.append(GroupMove(key=prefix, renames=recorded))
        for rename in recorded:
            assert rename.digest is not None
            plan.expected[rename.new] = rename.digest
        report.ok += 1

    plan.moves = tuple(moves)
    return plan


def apply_import(
    plan: ImportPlan,
    root: Path,
    journal_dir: Path | None = None,
    monitor: Monitor | None = None,
) -> Report:
    """Copy a built plan into the archive and verify the copies."""
    monitor = monitor or Monitor()
    report = plan.report
    if not plan.moves:
        return report
    problems = validate_plan(plan.moves, root, sources_outside_root=True)
    if problems:
        raise ValueError("plan failed validation:\n" + "\n".join(problems))

    journal = Journal.create(
        root,
        plan.moves,
        directory=journal_dir,
        kind="copy",
        algorithm=plan.algorithm,
        command="import",
    )
    print(f"journal: {journal.path}", file=sys.stderr)
    result = apply_plan(journal, monitor=monitor)
    for key, error in result.failed:
        report.ok -= 1
        report.add(Finding(Bucket.APPLY_FAILED, Path(key), f"import failed: {error}"))

    failed_targets = {
        rename.new
        for move in plan.moves
        if move.key in {key for key, _ in result.failed}
        for rename in move.renames
    }
    for index, (target, expected) in enumerate(sorted(plan.expected.items())):
        monitor.step("verify-copies", index, len(plan.expected), target)
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
    monitor.emit("verify-copies", len(plan.expected), len(plan.expected))
    return report


def _selection_roots(card: Path, only: Sequence[Path]) -> list[Path] | None:
    """Resolved selection directories, or ``None`` for the whole card."""
    if not only:
        return None
    selection: list[Path] = []
    for path in only:
        resolved = path.resolve()
        if not resolved.is_relative_to(card.resolve()):
            raise ValueError(f"path is outside the card: {path}")
        if not resolved.is_dir():
            raise ValueError(f"expected a directory on the card, got: {path}")
        selection.append(resolved)
    return selection


#: why a file was ignored, machine-readably (Finding.data)
_HIDDEN = {"reason": "hidden-path"}
_IGNORE_PATTERN = {"reason": "ignore-pattern"}
_JPEG_TWIN = {"reason": "jpeg-twin"}


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


def _master_of(group: CardGroup, config: Config, camera_extensions: frozenset[str]) -> Path | None:
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
    # a lone photo without a camera-native RAW (a JPEG or HEIC shot,
    # a standalone DNG) is its own master
    loose = [
        member
        for member in group.members
        if base_named(member) and member.suffix.lstrip(".").lower() in LOOSE_MASTER_EXTENSIONS
    ]
    return loose[0] if len(loose) == 1 else None


def _member_targets(group: CardGroup, prefix: str, destination: Path) -> list[Rename]:
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


def _render_layout(layout: str, resolved: ResolvedDate, shoot: str | None = None) -> Path:
    value = resolved.value
    rendered = (
        layout.replace("{yyyy}", f"{value.year:04d}")
        .replace("{mm}", f"{value.month:02d}")
        .replace("{dd}", f"{value.day:02d}")
    )
    if "{shoot}" in rendered:
        if shoot is None:
            raise ValueError(
                f"this tree files by shoot ({layout!r}); pass --shoot to import into it"
            )
        rendered = rendered.replace("{shoot}", shoot)
    return Path(rendered)


#: what a shoot name may contain after normalization; anything else is
#: rejected rather than silently mangled — names must be predictable
_SHOOT_CHARS = re.compile(r"[A-Za-z0-9._-]+\Z")
_SHOOT_MAX = 64


def normalize_shoot(shoot: str | None) -> str | None:
    """One predictable rule: trim, collapse whitespace runs to ``_``.

    Rejects anything else (path separators, unicode punctuation, an
    empty result) instead of guessing — a mangled shoot name would file
    photos somewhere the photographer would not look.
    """
    if shoot is None:
        return None
    collapsed = "_".join(shoot.split())
    if not collapsed:
        raise ValueError("shoot name is empty")
    if len(collapsed) > _SHOOT_MAX:
        raise ValueError(f"shoot name exceeds {_SHOOT_MAX} characters: {collapsed!r}")
    if not _SHOOT_CHARS.match(collapsed):
        bad = sorted({c for c in collapsed if not re.match(r"[A-Za-z0-9._-]", c)})
        raise ValueError(
            f"shoot name contains unsupported character(s) {bad!r}; allowed:"
            " letters, digits, dot, underscore, hyphen (spaces become underscores)"
        )
    return collapsed


def _check_shoot_against_config(config: Config, shoot: str | None) -> None:
    """A shoot that would be silently ignored is an error, not a shrug."""
    uses_shoot = any("{shoot}" in tree.layout for tree in config.trees)
    if shoot is not None and not uses_shoot:
        raise ValueError(
            "a shoot name was given, but no tree layout contains {shoot} —"
            " it would be silently ignored"
        )
