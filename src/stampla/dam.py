"""DAM token injection: let the DAM rename the masters it manages.

A DAM (e.g. Adobe Lightroom Classic) must rename its own masters or its
catalog loses track of them — but it cannot compute content hashes. The
bridge is a metadata token: stampla writes each master's computed
name into a field the DAM exposes as a filename template token (for
Lightroom Classic, IPTC "Job Identifier" — `TransmissionReference`),
and the DAM renames from that token.

Where the token goes follows where the DAM keeps metadata:

- a RAW master with an ``.xmp`` sidecar → the sidecar,
- an embedded-metadata master (JPEG, DNG, TIFF, PSD) → the file itself
  (its content hash goes stale at that moment, which is expected for
  formats that are edited in place),
- a RAW master **without** a sidecar → reported as ``needs-sidecar``.
  stampla never fabricates a sidecar: the DAM reading a minimal,
  script-made file could wipe catalog-side metadata. Save metadata from
  the DAM first, then rerun.

The DAM workflow after injection (Lightroom Classic):

1. Metadata → Read Metadata from Files on the affected folders,
2. Library → Rename Photos with the single ``{Job Identifier}`` token.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from stampla.config import Config, Tree
from stampla.dates import ResolvedDate, resolve_date
from stampla.exiftool import ExifTool
from stampla.family import group_by_prefix
from stampla.hashing import hash_files
from stampla.manifest import Manifest, ManifestError
from stampla.report import Bucket, Finding, Report
from stampla.scan import scan_tree

#: formats whose metadata lives inside the file; the token goes there
EMBEDDED_TOKEN_EXTENSIONS = frozenset({"jpg", "jpeg", "dng", "tif", "tiff", "psd"})


@dataclass(frozen=True)
class InjectOptions:
    apply: bool = False
    workers: int | None = None


def run_inject(
    config: Config, root: Path, paths: tuple[Path, ...] = (), options: InjectOptions | None = None
) -> Report:
    """Write computed names into the DAM token of stale-named masters."""
    options = options or InjectOptions()
    if config.dam is None or not config.dam.trees:
        raise ValueError("no [dam] trees configured; nothing to inject into")
    report = Report()
    manifest = Manifest.load(root.resolve())
    with ExifTool() as tool:
        for tree in config.trees:
            if tree.path not in config.dam.trees:
                continue
            scan_root = (root / tree.path).resolve()
            if paths:
                scoped = [p.resolve() for p in paths if p.resolve().is_relative_to(scan_root)]
                if not scoped:
                    continue
            else:
                scoped = [scan_root]
            for target_root in scoped:
                _inject_tree(tool, tree, target_root, config, options, report, manifest)
    manifest.save()
    return report


def _inject_tree(
    tool: ExifTool,
    tree: Tree,
    scan_root: Path,
    config: Config,
    options: InjectOptions,
    report: Report,
    manifest: Manifest,
) -> None:
    files = list(scan_tree(scan_root, config.grammar, config.excludes))
    report.scanned += len(files)
    families = group_by_prefix(files)
    report.families += len(families)

    master_extensions = config.raw_extensions if tree.media == "photo" else config.video_extensions
    chain = config.date_chain_photo if tree.media == "photo" else config.date_chain_video

    masters = [
        master.path
        for family in families
        if (master := family.master(master_extensions)) is not None
    ]
    assert config.dam is not None
    token_tag = config.dam.token_tag.partition(":")[2] or config.dam.token_tag
    tags = sorted({entry.partition(":")[2] or entry for entry in chain} | {token_tag})
    metadata = tool.read_metadata(masters, tags) if masters else {}
    algorithm = config.pattern.digest
    digests: dict[Path, str] = {}
    for path in masters:
        with suppress(ManifestError):
            cached = manifest.lookup(path, algorithm)
            if cached is not None:
                digests[path] = cached
    to_hash = [path for path in masters if path not in digests]
    raw_digests, _ = hash_files(to_hash, [algorithm], workers=options.workers)
    for path, result in raw_digests.items():
        digests[path] = result[algorithm]
        with suppress(ManifestError):
            manifest.record(path, algorithm, result[algorithm])

    for family in families:
        master = family.master(master_extensions)
        if master is None:
            continue  # orphan/ambiguous families are verify's business
        path = master.path
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

        extension = master.parsed.ext if master.parsed else ""
        if extension in EMBEDDED_TOKEN_EXTENSIONS and _is_converged(
            family.prefix, derived, metadata[path], token_tag, config.pattern.datetime_length
        ):
            # Writing the token changed the content, so the name can never
            # equal the current hash for embedded formats. Name == stored
            # token marks the flow as complete; re-injecting would chase
            # its own tail forever.
            report.ok += 1
            continue

        target, note = _token_target(path, extension)
        if target is None:
            report.add(
                Finding(
                    Bucket.NEEDS_SIDECAR,
                    path,
                    "no sidecar to carry the token; save metadata from the DAM"
                    " first and rerun (or rename directly if the DAM does not"
                    " manage this file)",
                )
            )
            continue

        assert config.dam is not None
        if not options.apply:
            report.add(
                Finding(
                    Bucket.TOKEN_PENDING, path, f"would write {derived} into {target.name}{note}"
                )
            )
            continue
        tool.execute("-overwrite_original", f"-{config.dam.token_tag}={derived}", str(target))
        written = tool.read_metadata([target], [config.dam.token_tag.partition(":")[2]])
        stored = next(iter(written.get(target, {}).values()), None)
        if stored != derived:
            report.add(
                Finding(
                    Bucket.HASH_ERROR,
                    target,
                    f"token write not confirmed: read back {stored!r}",
                )
            )
            continue
        report.add(
            Finding(Bucket.TOKEN_WRITTEN, path, f"{derived} written into {target.name}{note}")
        )


def _is_converged(
    prefix: str,
    derived: str,
    tags: dict[str, object],
    token_tag: str,
    datetime_length: int,
) -> bool:
    """Whether an embedded-format master already completed the token flow.

    True when the stored token equals the current name prefix (the DAM
    renamed to what was injected) and the capture date still agrees —
    the remaining hash difference is the token write itself.
    """
    stored = next(
        (value for key, value in tags.items() if key.endswith(f":{token_tag}")),
        None,
    )
    return stored == prefix and derived[:datetime_length] == prefix[:datetime_length]


def _token_target(master: Path, extension: str) -> tuple[Path | None, str]:
    """Where the token belongs for this master, plus a display note."""
    if extension in EMBEDDED_TOKEN_EXTENSIONS:
        return master, " (embedded; its content hash goes stale now)"
    sidecar = master.with_suffix(".xmp")
    if sidecar.is_file():
        return sidecar, ""
    return None, ""
