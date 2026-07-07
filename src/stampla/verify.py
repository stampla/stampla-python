"""The verify command: recompute every name and report what disagrees.

For each family, the master's capture time and content hash are
recomputed and the resulting prefix compared with the one on disk.
The classification distinguishes what a mismatch *means*:

- a date difference is a naming error (or a deliberate re-date),
- a hash difference on a format that is edited in place is expected drift,
- a hash difference on an immutable format is a corruption alarm.

Families whose master is structurally ambiguous (a RAW plus a conversion
named after it) are settled by evidence: the candidate whose content hash
matches the prefix is the master, the others are treated as derivatives.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from stampla.config import Config, Tree
from stampla.dates import UnresolvedDate, resolve_date
from stampla.exiftool import ExifTool
from stampla.family import Family, group_by_prefix
from stampla.hashing import hash_files
from stampla.pattern import NamingPattern
from stampla.report import Bucket, Finding, Report
from stampla.scan import FileStatus, ScannedFile, scan_tree


@dataclass(frozen=True)
class VerifyOptions:
    skip_hash: bool = False
    workers: int | None = None


def run_verify(
    config: Config,
    root: Path,
    paths: Sequence[Path] = (),
    options: VerifyOptions | None = None,
) -> Report:
    """Verify the configured trees (or the given subpaths) under ``root``."""
    options = options or VerifyOptions()
    report = Report()
    with ExifTool() as tool:
        for tree, scan_root in _targets(config, root, paths):
            report.merge(_verify_tree(tool, tree, scan_root, config, options))
    return report


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
                targets.append((tree, resolved))
    if not targets:
        raise ValueError(
            "nothing to verify: no configured tree matches "
            + (", ".join(str(p) for p in paths) if paths else str(root))
        )
    return targets


def _verify_tree(
    tool: ExifTool, tree: Tree, scan_root: Path, config: Config, options: VerifyOptions
) -> Report:
    report = Report()
    files = list(scan_tree(scan_root, config.grammar, config.excludes))
    report.scanned = len(files)

    for file in files:
        if file.status == FileStatus.MALFORMED:
            report.add(Finding(Bucket.MALFORMED, file.path, "name breaks the grammar"))
        elif file.status == FileStatus.UNNAMED:
            report.add(Finding(Bucket.UNNAMED, file.path))

    families = group_by_prefix(files)
    report.families = len(families)

    if tree.media == "photo":
        master_extensions = config.raw_extensions
        chain = config.date_chain_photo
    else:
        master_extensions = config.video_extensions
        chain = config.date_chain_video

    candidates = [
        candidate.path
        for family in families
        for candidate in family.master_candidates(master_extensions)
    ]
    tags = sorted({entry.partition(":")[2] or entry for entry in chain})
    metadata = tool.read_metadata(candidates, tags) if candidates else {}
    digests: dict[Path, str] = {}
    hash_errors: dict[Path, str] = {}
    if not options.skip_hash and candidates:
        raw_digests, hash_errors = hash_files(
            candidates, [config.pattern.digest], workers=options.workers
        )
        digests = {path: result[config.pattern.digest] for path, result in raw_digests.items()}

    derived_owners: dict[str, list[Path]] = defaultdict(list)
    for family in families:
        derived = _classify_family(
            report,
            family,
            master_extensions,
            config.mutable_extensions,
            config.pattern,
            chain,
            metadata,
            digests,
            hash_errors,
            options.skip_hash,
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
                    )
                )
    return report


def _classify_family(
    report: Report,
    family: Family,
    master_extensions: frozenset[str],
    mutable_extensions: frozenset[str],
    pattern: NamingPattern,
    chain: Sequence[str],
    metadata: Mapping[Path, Mapping[str, object]],
    digests: Mapping[Path, str],
    hash_errors: Mapping[Path, str],
    skip_hash: bool,
) -> tuple[str, Path] | None:
    """Classify one family; returns (derived prefix, master path) if derivable."""
    candidates = family.master_candidates(master_extensions)
    if not candidates:
        members = tuple(member.path for member in family.members)
        report.add(
            Finding(
                Bucket.ORPHAN_FAMILY,
                members[0],
                f"{len(members)} file(s) share prefix {family.prefix} but none is a master",
                related=members[1:],
            )
        )
        return None

    if len(candidates) > 1:
        master = _master_by_hash(family, candidates, pattern, digests)
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
    tags = metadata.get(path)
    if tags is None:
        report.add(Finding(Bucket.METADATA_UNREADABLE, path))
        return None
    resolved = resolve_date(tags, chain)
    if isinstance(resolved, UnresolvedDate):
        report.add(Finding(Bucket.UNRESOLVED_DATE, path, resolved.reason))
        return None

    actual_prefix = family.prefix
    if pattern.datetime_of(actual_prefix) != resolved.value:
        derived_datetime = resolved.value.strftime(pattern.datetime_format)
        report.add(
            Finding(
                Bucket.DATE_MISMATCH,
                path,
                f"name says {actual_prefix[: pattern.datetime_length]},"
                f" metadata says {derived_datetime} ({resolved.source})",
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

    ext = master.parsed.ext if master.parsed else path.suffix.lstrip(".").lower()
    bucket = Bucket.EDIT_DRIFT if ext in mutable_extensions else Bucket.CORRUPTION
    report.add(
        Finding(
            bucket,
            path,
            f"name says {pattern.digest_of(actual_prefix)},"
            f" content is {digest[: pattern.digest_length]}",
        )
    )
    return (derived_prefix, path)


def _master_by_hash(
    family: Family,
    candidates: tuple[ScannedFile, ...],
    pattern: NamingPattern,
    digests: Mapping[Path, str],
) -> ScannedFile | None:
    """The candidate whose content hash matches the family prefix, if unique."""
    expected = pattern.digest_of(family.prefix)
    matching = [
        candidate
        for candidate in candidates
        if digests.get(candidate.path, "")[: pattern.digest_length] == expected
    ]
    return matching[0] if len(matching) == 1 else None
