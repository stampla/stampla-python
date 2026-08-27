"""The rename command: bring names in line, directly, through the journal.

Renaming splits by who owns the file:

- In trees without DAM management, whole groups are renamed to their
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
applied per group with rollback, resumable, undoable.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from stampla.apply import apply_plan, validate_plan
from stampla.config import Config, Tree
from stampla.dates import ResolvedDate, resolve_dates
from stampla.digests import naming_digests
from stampla.exiftool import ExifTool
from stampla.group import Group, group_by_prefix
from stampla.journal import GroupMove, Journal, Rename
from stampla.manifest import Manifest
from stampla.naming import ParsedName
from stampla.progress import Monitor
from stampla.report import Bucket, Finding, Report, scan_alarm
from stampla.scan import FileStatus, ScannedFile, scan_tree


@dataclass(frozen=True)
class RenameOptions:
    apply: bool = False
    workers: int | None = None
    journal_dir: Path | None = None
    full: bool = False
    use_manifest: bool = True


def run_rename(
    config: Config,
    root: Path,
    paths: tuple[Path, ...] = (),
    options: RenameOptions | None = None,
    monitor: Monitor | None = None,
) -> tuple[Report, tuple[GroupMove, ...]]:
    """Plan (and with ``apply``, execute) direct renames under ``root``."""
    options = options or RenameOptions()
    monitor = monitor or Monitor()
    report = Report()
    manifest = Manifest.load(root.resolve()) if options.use_manifest else None
    moves: list[GroupMove] = []
    matched: set[Path] = set()
    with ExifTool(workers=options.workers) as tool:
        for tree in config.trees:
            scan_root = (root / tree.path).resolve()
            if paths:
                scoped = [p.resolve() for p in paths if p.resolve().is_relative_to(scan_root)]
                for candidate in scoped:
                    if not candidate.is_dir():
                        raise ValueError(f"expected a directory, got: {candidate}")
                matched.update(scoped)
            else:
                scoped = [scan_root] if scan_root.is_dir() else []
            for target_root in scoped:
                moves.extend(
                    _plan_tree(tool, tree, target_root, config, options, report, manifest, monitor)
                )
    if paths:
        unmatched = [p for p in paths if p.resolve() not in matched]
        if unmatched:
            raise ValueError(
                "path(s) outside every configured tree: " + ", ".join(str(p) for p in unmatched)
            )
    if manifest is not None:
        manifest.save()

    if not options.apply:
        for move in moves:
            for rename in move.renames:
                report.add(
                    Finding(
                        Bucket.RENAME_PENDING,
                        rename.old,
                        f"would become {rename.new.name}",
                        data={"new_name": rename.new.name},
                    )
                )
        return report, tuple(moves)

    if moves:
        problems = validate_plan(tuple(moves), root)
        if problems:
            raise ValueError("plan failed validation:\n" + "\n".join(problems))
        journal = Journal.create(
            root, tuple(moves), directory=options.journal_dir, command="rename"
        )
        print(f"journal: {journal.path}", file=sys.stderr)
        result = apply_plan(journal, monitor=monitor)
        for key, error in result.failed:
            report.add(Finding(Bucket.APPLY_FAILED, Path(key), f"rename failed: {error}"))
        applied = set(result.applied)
        for move in moves:
            if move.key in applied:
                for rename in move.renames:
                    report.add(
                        Finding(
                            Bucket.RENAMED,
                            rename.old,
                            f"now {rename.new.name}",
                            data={"new_name": rename.new.name},
                        )
                    )
    return report, tuple(moves)


def _plan_tree(
    tool: ExifTool,
    tree: Tree,
    scan_root: Path,
    config: Config,
    options: RenameOptions,
    report: Report,
    manifest: Manifest | None,
    monitor: Monitor,
) -> list[GroupMove]:
    files = list(scan_tree(scan_root, config.grammar, config.excludes, on_error=scan_alarm(report)))
    monitor.step("scan", len(files), 0)
    report.scanned += len(files)
    groups = group_by_prefix(files)
    report.groups += len(groups)
    dam_managed = config.dam is not None and tree.path in config.dam.trees

    if tree.media == "photo":
        master_extensions = config.photo_master_extensions
    else:
        master_extensions = config.video_extensions

    # case-broken names join their group's move when that group is being
    # re-prefixed — fixing them under the old prefix in the same run
    # would split a sidecar from its master
    case_candidates = _case_fix_candidates(files, config, dam_managed, master_extensions)
    case_by_prefix: dict[str, list[tuple[Path, ParsedName]]] = defaultdict(list)
    for path, parsed in case_candidates:
        case_by_prefix[parsed.prefix].append((path, parsed))
    handled_case: set[Path] = set()
    moves: list[GroupMove] = []

    chain = config.date_chain_photo if tree.media == "photo" else config.date_chain_video
    masters = {
        group.prefix: master
        for group in groups
        if (master := group.master(master_extensions)) is not None
    }
    paths = sorted(master.path for master in masters.values())
    monitor.step("dates", 0, len(paths))
    dates = resolve_dates(paths, chain, config.tzinfo, tool, manifest=manifest, full=options.full)
    monitor.step("dates", len(paths), len(paths))
    digests, digest_errors = naming_digests(
        paths,
        config.pattern,
        tool,
        manifest=manifest,
        workers=options.workers,
        full=options.full,
        monitor=monitor,
    )

    for group in groups:
        master = masters.get(group.prefix)
        if master is None:
            continue  # verify reports orphan/ambiguous groups
        path = master.path
        if path in digest_errors:
            report.add(Finding(Bucket.HASH_ERROR, path, digest_errors[path]))
            continue
        resolved = dates.get(path)
        if resolved is None:
            report.add(Finding(Bucket.METADATA_UNREADABLE, path))
            continue
        if path not in digests:
            continue
        if not isinstance(resolved, ResolvedDate):
            report.add(Finding(Bucket.UNRESOLVED_DATE, path, resolved.reason))
            continue
        derived = config.pattern.build_prefix(resolved.value, digests[path])
        if derived == group.prefix:
            report.ok += 1
            continue
        extra = case_by_prefix.get(group.prefix, ())
        move = _group_move(group, master, derived, dam_managed, extra)
        if move is not None:
            handled_case.update(case_path for case_path, _ in extra)
            moves.append(move)

    # case fixes whose group keeps its prefix (or has none) stand alone
    for case_path, parsed in case_candidates:
        if case_path in handled_case:
            continue
        moves.append(
            GroupMove(
                key=f"case:{case_path}",
                renames=(Rename(old=case_path, new=case_path.with_name(parsed.rebuild())),),
            )
        )

    return moves


def _group_move(
    group: Group,
    master: ScannedFile,
    derived: str,
    dam_managed: bool,
    case_members: Sequence[tuple[Path, ParsedName]] = (),
) -> GroupMove | None:
    """The renames one group calls for; ``None`` when nothing is ours.

    ``case_members`` are the group's case-broken files: they move to the
    derived prefix in the same atomic group, extension case fixed.
    """
    renames: list[Rename] = []
    for member in group.members:
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
    for case_path, parsed in case_members:
        renames.append(Rename(old=case_path, new=case_path.with_name(parsed.rebuild(derived))))
    if not renames:
        return None
    return GroupMove(key=f"{group.prefix}->{derived}", renames=tuple(renames))


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


def _case_fix_candidates(
    files: list[ScannedFile],
    config: Config,
    dam_managed: bool,
    master_extensions: frozenset[str],
) -> list[tuple[Path, ParsedName]]:
    """Malformed names that become canonical by lowercasing the extensions."""
    candidates: list[tuple[Path, ParsedName]] = []
    for file in files:
        if file.status != FileStatus.MALFORMED:
            continue
        stem, dot, extensions = file.path.name.partition(".")
        candidate = f"{stem}{dot}{extensions.lower()}"
        if candidate == file.path.name:
            continue
        parsed = config.grammar.parse(candidate)
        if parsed is None:
            continue
        if dam_managed and (
            (parsed.suffix == "" and parsed.raw_ext is None and parsed.ext in master_extensions)
            or (parsed.suffix == "" and parsed.raw_ext is None and parsed.ext == "xmp")
        ):
            # a case-broken master or its plain sidecar is the DAM's file;
            # renaming it behind the DAM's back breaks the catalog link
            continue
        candidates.append((file.path, parsed))
    return candidates
