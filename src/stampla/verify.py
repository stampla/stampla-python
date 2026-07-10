"""The verify command: recompute every name and report what disagrees.

For each group, the master's capture time and content hash are
recomputed and the resulting prefix compared with the one on disk.
The classification distinguishes what a mismatch *means*:

- a date difference is a naming error (or a deliberate re-date),
- a hash difference on a format that is edited in place is expected drift,
- a hash difference on an immutable format is a corruption alarm.

Groups whose master is structurally ambiguous (a RAW plus a conversion
named after it) are settled by evidence: the candidate whose content hash
matches the prefix is the master, the others are treated as derivatives.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from stampla.config import Config, Tree
from stampla.dates import ResolvedDate, UnresolvedDate, resolve_dates
from stampla.digests import digest_under, naming_digests
from stampla.exiftool import ExifTool
from stampla.group import Group, group_by_prefix
from stampla.manifest import Manifest
from stampla.pattern import NamingPattern
from stampla.progress import Monitor
from stampla.report import Bucket, Finding, Report
from stampla.scan import FileStatus, ScannedFile, scan_tree


@dataclass(frozen=True)
class VerifyOptions:
    skip_hash: bool = False
    workers: int | None = None
    full: bool = False
    use_manifest: bool = True


def run_verify(
    config: Config,
    root: Path,
    paths: Sequence[Path] = (),
    options: VerifyOptions | None = None,
    monitor: Monitor | None = None,
) -> Report:
    """Verify the configured trees (or the given subpaths) under ``root``."""
    options = options or VerifyOptions()
    monitor = monitor or Monitor()
    # --skip-hash still loads the manifest: it caches dates, not just digests
    manifest = Manifest.load(root.resolve()) if options.use_manifest else None
    report = Report()
    with ExifTool(workers=options.workers) as tool:
        for tree, scan_root in _targets(config, root, paths):
            report.merge(_verify_tree(tool, tree, scan_root, config, options, manifest, monitor))
    if manifest is not None:
        if manifest.stale_trusted:
            report.hints.append(
                f"{manifest.stale_trusted} cached manifest entr(y/ies) were trusted despite being"
                " older than 180 days; run with --full for a deep check"
            )
        manifest.save()
    return report


def _scan_with_monitor(scan_root: Path, config: Config, monitor: Monitor) -> list[ScannedFile]:
    """Scan a tree, reporting motion (the total is unknown until done)."""
    files: list[ScannedFile] = []
    for file in scan_tree(scan_root, config.grammar, config.excludes):
        files.append(file)
        if len(files) % 512 == 0:
            monitor.step("scan", len(files), 0, file.path)
    monitor.step("scan", len(files), 0)
    return files


def _targets(config: Config, root: Path, paths: Sequence[Path]) -> list[tuple[Tree, Path]]:
    targets: list[tuple[Tree, Path]] = []
    for tree in config.trees:
        tree_root = (root / tree.path).resolve()
        if not paths:
            if tree_root.is_dir():
                targets.append((tree, tree_root))
            continue
        for path in paths:
            resolved = path.resolve()
            if resolved.is_relative_to(tree_root):
                if not resolved.is_dir():
                    raise ValueError(f"expected a directory, got: {path}")
                targets.append((tree, resolved))
    if not targets:
        raise ValueError(
            "nothing to verify: no configured tree matches "
            + (", ".join(str(p) for p in paths) if paths else str(root))
        )
    return targets


def _verify_tree(
    tool: ExifTool,
    tree: Tree,
    scan_root: Path,
    config: Config,
    options: VerifyOptions,
    manifest: Manifest | None = None,
    monitor: Monitor | None = None,
) -> Report:
    monitor = monitor or Monitor()
    report = Report()
    files = _scan_with_monitor(scan_root, config, monitor)
    report.scanned = len(files)

    for file in files:
        if file.status == FileStatus.MALFORMED:
            report.add(Finding(Bucket.MALFORMED, file.path, "name breaks the grammar"))
        elif file.status == FileStatus.UNNAMED:
            detail = ""
            if file.path.name.endswith(".part"):
                detail = (
                    "leftover scratch file from an interrupted copy;"
                    " check the journal, then delete it"
                )
            report.add(Finding(Bucket.UNNAMED, file.path, detail))

    groups = group_by_prefix(files)
    report.groups = len(groups)

    if tree.media == "photo":
        master_extensions = config.photo_master_extensions
        chain = config.date_chain_photo
    else:
        master_extensions = config.video_extensions
        chain = config.date_chain_video

    candidates = [
        candidate.path
        for group in groups
        for candidate in group.master_candidates(master_extensions)
    ]
    # date resolution is one ExifTool batch: coarse events around it
    monitor.step("dates", 0, len(candidates))
    dates = resolve_dates(
        candidates, chain, config.tzinfo, tool, manifest=manifest, full=options.full
    )
    monitor.step("dates", len(candidates), len(candidates))
    digests: dict[Path, str] = {}
    hash_errors: dict[Path, str] = {}
    if not options.skip_hash and candidates:
        digests, hash_errors = naming_digests(
            candidates,
            config.pattern,
            tool,
            manifest=manifest,
            workers=options.workers,
            full=options.full,
            monitor=monitor,
        )

    derived_owners: dict[str, list[Path]] = defaultdict(list)
    for group in groups:
        derived = _classify_group(
            report,
            group,
            master_extensions,
            config,
            chain,
            dates,
            digests,
            hash_errors,
            options.skip_hash,
            tool,
            manifest,
        )
        if derived is not None:
            derived_owners[derived[0]].append(derived[1])

    for derived_prefix, owners in sorted(derived_owners.items()):
        if len(owners) > 1:
            for owner in owners:
                report.add(
                    Finding(
                        Bucket.COLLISION,
                        owner,
                        f"derives {derived_prefix}, same as "
                        + ", ".join(str(o) for o in owners if o != owner),
                        related=tuple(o for o in owners if o != owner),
                        data={"prefix": derived_prefix},
                    )
                )
    return report


def _classify_group(
    report: Report,
    group: Group,
    master_extensions: frozenset[str],
    config: Config,
    chain: Sequence[str],
    dates: Mapping[Path, ResolvedDate | UnresolvedDate | None],
    digests: Mapping[Path, str],
    hash_errors: Mapping[Path, str],
    skip_hash: bool,
    tool: ExifTool,
    manifest: Manifest | None,
) -> tuple[str, Path] | None:
    """Classify one group; returns (derived prefix, master path) if derivable."""
    candidates = group.master_candidates(master_extensions)
    if not candidates:
        members = tuple(member.path for member in group.members)
        report.add(
            Finding(
                Bucket.ORPHAN_GROUP,
                members[0],
                f"{len(members)} file(s) share prefix {group.prefix} but none is a master",
                related=members[1:],
            )
        )
        return None

    pattern = config.pattern
    if len(candidates) > 1:
        master = _master_by_hash(group, candidates, pattern, digests)
        if master is None:
            names = ", ".join(candidate.path.name for candidate in candidates)
            detail = f"{len(candidates)} master candidates ({names})" + (
                "; run without --skip-hash to settle by content" if skip_hash else ""
            )
            report.add(Finding(Bucket.AMBIGUOUS_MASTER, candidates[0].path, detail))
            return None
    else:
        master = candidates[0]

    path = master.path
    if path in hash_errors:
        report.add(Finding(Bucket.HASH_ERROR, path, hash_errors[path]))
        return None
    resolved = dates.get(path)
    if resolved is None:
        report.add(Finding(Bucket.METADATA_UNREADABLE, path))
        return None
    if isinstance(resolved, UnresolvedDate):
        report.add(Finding(Bucket.UNRESOLVED_DATE, path, resolved.reason))
        return None

    actual_prefix = group.prefix
    named_pattern = master.parsed.pattern if master.parsed else pattern
    if named_pattern.datetime_of(actual_prefix) != resolved.value:
        name_datetime = actual_prefix[: named_pattern.datetime_length]
        derived_datetime = resolved.value.strftime(named_pattern.datetime_format)
        report.add(
            Finding(
                Bucket.DATE_MISMATCH,
                path,
                f"name says {name_datetime}, metadata says {derived_datetime} ({resolved.source})",
                data={
                    "name_datetime": name_datetime,
                    "metadata_datetime": derived_datetime,
                    "source": resolved.source,
                },
            )
        )
        return None
    if skip_hash:
        report.ok += 1
        return None

    digest = digests.get(path)
    if digest is None:
        report.add(Finding(Bucket.HASH_ERROR, path, "no digest computed"))
        return None
    derived_prefix = pattern.build_prefix(resolved.value, digest)
    if derived_prefix == actual_prefix:
        report.ok += 1
        return (derived_prefix, path)

    # A same-looking name may have been produced by an additional
    # recognized pattern: re-derive under each before judging content.
    for alternative in config.additional_patterns:
        if not alternative.matches_prefix(actual_prefix):
            continue
        alt_digest = digest_under(path, alternative, tool, manifest)
        if alt_digest is None:
            continue
        if alternative.build_prefix(resolved.value, alt_digest) == actual_prefix:
            report.add(
                Finding(
                    Bucket.OTHER_PATTERN,
                    path,
                    f"intact under pattern {alternative.name!r};"
                    f" pending migration to {pattern.name!r}",
                    data={"pattern": alternative.name, "target_pattern": pattern.name},
                )
            )
            return None

    ext = master.parsed.ext if master.parsed else path.suffix.lstrip(".").lower()
    data: dict[str, object] = {"derived_prefix": derived_prefix}
    if pattern.digest_source_for(ext) == "image":
        bucket = Bucket.CORRUPTION
        meaning = "image data differs from the name"
    else:
        mutable = ext in config.mutable_extensions
        bucket = Bucket.EDIT_DRIFT if mutable else Bucket.CORRUPTION
        name_digest = pattern.digest_of(actual_prefix)
        content_digest = digest[: pattern.digest_length]
        meaning = f"name says {name_digest}, content is {content_digest}"
        data.update(name_digest=name_digest, content_digest=content_digest)
    report.add(Finding(bucket, path, meaning, data=data))
    return (derived_prefix, path)


def _master_by_hash(
    group: Group,
    candidates: tuple[ScannedFile, ...],
    pattern: NamingPattern,
    digests: Mapping[Path, str],
) -> ScannedFile | None:
    """The candidate whose content hash matches the group prefix, if unique."""
    expected = pattern.digest_of(group.prefix)
    matching = [
        candidate
        for candidate in candidates
        if digests.get(candidate.path, "")[: pattern.digest_length] == expected
    ]
    return matching[0] if len(matching) == 1 else None
