"""The rename command: bring names in line, directly, through the journal.

Renaming splits by who owns the file:

- In trees without DAM management, whole families are renamed to their
  freshly derived prefix — master and every member, atomically.
- In DAM-managed trees, the master and its single-extension ``.xmp``
  belong to the DAM (see the inject command); rename touches only the
  members the DAM does not know: append-style sidecars
  (``prefix.nef.xmp``), sidecars in subdirectories, labeled derivatives.
  Both sides target the same derived prefix, so the order relative to
  the DAM's own rename does not matter.

A separate pass fixes malformed names that differ from a canonical name
only by extension case (``.FP2`` → ``.fp2``).

Everything flows through the write-ahead journal: validated as a whole,
applied per family with rollback, resumable, undoable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stampla.apply import apply_plan, validate_plan
from stampla.config import Config, Tree
from stampla.dates import ResolvedDate, resolve_date
from stampla.digests import naming_digests
from stampla.exiftool import ExifTool
from stampla.family import Family, group_by_prefix
from stampla.journal import FamilyMove, Journal, Rename
from stampla.manifest import Manifest
from stampla.report import Bucket, Finding, Report
from stampla.scan import FileStatus, ScannedFile, scan_tree


@dataclass(frozen=True)
class RenameOptions:
    apply: bool = False
    workers: int | None = None
    journal_dir: Path | None = None


@dataclass
class RenamePlan:
    moves: tuple[FamilyMove, ...] = ()
    report: Report | None = None


def run_rename(
    config: Config, root: Path, paths: tuple[Path, ...] = (), options: RenameOptions | None = None
) -> tuple[Report, tuple[FamilyMove, ...]]:
    """Plan (and with ``apply``, execute) direct renames under ``root``."""
    options = options or RenameOptions()
    report = Report()
    manifest = Manifest.load(root.resolve())
    moves: list[FamilyMove] = []
    with ExifTool() as tool:
        for tree in config.trees:
            scan_root = (root / tree.path).resolve()
            if paths:
                scoped = [p.resolve() for p in paths if p.resolve().is_relative_to(scan_root)]
            else:
                scoped = [scan_root] if scan_root.is_dir() else []
            for target_root in scoped:
                moves.extend(_plan_tree(tool, tree, target_root, config, options, report, manifest))
    manifest.save()

    if not options.apply:
        for move in moves:
            for rename in move.renames:
                report.add(
                    Finding(
                        Bucket.RENAME_PENDING,
                        rename.old,
                        f"would become {rename.new.name}",
                    )
                )
        return report, tuple(moves)

    if moves:
        problems = validate_plan(tuple(moves), root)
        if problems:
            raise ValueError("plan failed validation:\n" + "\n".join(problems))
        journal = Journal.create(root, tuple(moves), directory=options.journal_dir)
        result = apply_plan(journal)
        for key, error in result.failed:
            report.add(Finding(Bucket.HASH_ERROR, Path(key), f"rename failed: {error}"))
        applied = set(result.applied)
        for move in moves:
            if move.key in applied:
                for rename in move.renames:
                    report.add(Finding(Bucket.RENAMED, rename.old, f"now {rename.new.name}"))
    return report, tuple(moves)


def _plan_tree(
    tool: ExifTool,
    tree: Tree,
    scan_root: Path,
    config: Config,
    options: RenameOptions,
    report: Report,
    manifest: Manifest,
) -> list[FamilyMove]:
    files = list(scan_tree(scan_root, config.grammar, config.excludes))
    report.scanned += len(files)
    families = group_by_prefix(files)
    report.families += len(families)
    dam_managed = config.dam is not None and tree.path in config.dam.trees

    moves = _case_fixes(files, config)

    master_extensions = config.raw_extensions if tree.media == "photo" else config.video_extensions
    chain = config.date_chain_photo if tree.media == "photo" else config.date_chain_video

    masters = {
        family.prefix: master
        for family in families
        if (master := family.master(master_extensions)) is not None
    }
    tags = sorted({entry.partition(":")[2] or entry for entry in chain})
    paths = sorted(master.path for master in masters.values())
    metadata = tool.read_metadata(paths, tags) if paths else {}
    digests, digest_errors = naming_digests(
        paths, config.pattern, tool, manifest=manifest, workers=options.workers
    )

    for family in families:
        master = masters.get(family.prefix)
        if master is None:
            continue  # verify reports orphan/ambiguous families
        path = master.path
        if path in digest_errors:
            report.add(Finding(Bucket.HASH_ERROR, path, digest_errors[path]))
            continue
        if path not in metadata or path not in digests:
            continue
        resolved = resolve_date(metadata[path], chain)
        if not isinstance(resolved, ResolvedDate):
            report.add(Finding(Bucket.UNRESOLVED_DATE, path, resolved.reason))
            continue
        derived = config.pattern.build_prefix(resolved.value, digests[path])
        if derived == family.prefix:
            report.ok += 1
            continue
        move = _family_move(family, master, derived, dam_managed)
        if move is not None:
            moves.append(move)

    return moves


def _family_move(
    family: Family, master: ScannedFile, derived: str, dam_managed: bool
) -> FamilyMove | None:
    """The renames one family calls for; ``None`` when nothing is ours."""
    renames: list[Rename] = []
    for member in family.members:
        if member.parsed is None:
            continue
        if dam_managed and _is_dam_owned(member, master):
            continue
        renames.append(
            Rename(
                old=member.path,
                new=member.path.with_name(member.parsed.rebuild(derived)),
            )
        )
    if not renames:
        return None
    return FamilyMove(key=f"{family.prefix}->{derived}", renames=tuple(renames))


def _is_dam_owned(member: ScannedFile, master: ScannedFile) -> bool:
    """The master and its plain .xmp sidecar are renamed by the DAM itself."""
    if member.path == master.path:
        return True
    parsed = member.parsed
    return (
        parsed is not None
        and parsed.ext == "xmp"
        and parsed.raw_ext is None
        and parsed.suffix == ""
        and member.path.parent == master.path.parent
    )


def _case_fixes(files: list[ScannedFile], config: Config) -> list[FamilyMove]:
    """Malformed names that become canonical by lowercasing the extensions."""
    moves: list[FamilyMove] = []
    for file in files:
        if file.status != FileStatus.MALFORMED:
            continue
        stem, dot, extensions = file.path.name.partition(".")
        candidate = f"{stem}{dot}{extensions.lower()}"
        if candidate == file.path.name or config.grammar.parse(candidate) is None:
            continue
        moves.append(
            FamilyMove(
                key=f"case:{file.path.name}",
                renames=(Rename(old=file.path, new=file.path.with_name(candidate)),),
            )
        )
    return moves
