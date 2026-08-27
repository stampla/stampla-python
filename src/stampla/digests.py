"""Naming digests: the right hash for each master, per the pattern.

The pattern decides per extension whether a master's naming digest
covers the whole file or the image data only (see
:class:`stampla.pattern.NamingPattern`). This module computes either
kind, backed by the per-machine manifest so unchanged files are never
re-read: whole-file digests via parallel local hashing, image-data
digests via ExifTool. Manifest entries for image digests use the
``<algorithm>-image`` key, so the two kinds can never be confused.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from stampla.exiftool import ExifTool
from stampla.hashing import hash_files
from stampla.manifest import Manifest, ManifestError
from stampla.pattern import NamingPattern
from stampla.progress import Monitor


def naming_digests(
    paths: Sequence[Path],
    pattern: NamingPattern,
    tool: ExifTool,
    manifest: Manifest | None = None,
    workers: int | None = None,
    full: bool = False,
    monitor: Monitor | None = None,
) -> tuple[dict[Path, str], dict[Path, str]]:
    """Compute each master's naming digest under ``pattern``.

    Returns ``(digests, errors)`` — full hexdigests keyed by path, and
    per-path error messages for files that could not be hashed.
    """
    monitor = monitor or Monitor()
    file_sourced = [p for p in paths if pattern.digest_source_for(_ext(p)) == "file"]
    image_sourced = [p for p in paths if pattern.digest_source_for(_ext(p)) == "image"]

    digests: dict[Path, str] = {}
    errors: dict[Path, str] = {}

    to_hash = _through_manifest(file_sourced, pattern.digest, manifest, full, digests)
    if to_hash:
        stats = _pre_stats(to_hash)
        fresh, hash_errors = hash_files(to_hash, [pattern.digest], workers=workers, monitor=monitor)
        errors.update(hash_errors)
        for path, result in fresh.items():
            digests[path] = result[pattern.digest]
            _record(manifest, path, pattern.digest, result[pattern.digest], stats.get(path))

    image_key = f"{pattern.digest}-image"
    to_hash = _through_manifest(image_sourced, image_key, manifest, full, digests)
    if to_hash:
        # image hashes come back as one ExifTool batch: coarse events
        stats = _pre_stats(to_hash)
        monitor.step("hash", 0, len(to_hash))
        fresh_hashes = tool.read_image_hashes(to_hash, pattern.digest)
        monitor.step("hash", len(to_hash), len(to_hash))
        for path in to_hash:
            value = fresh_hashes.get(path)
            if value is None:
                errors[path] = (
                    "no image-data hash from ExifTool (unsupported format,"
                    " or the file could not be read)"
                )
                continue
            digests[path] = value
            _record(manifest, path, image_key, value, stats.get(path))

    return digests, errors


def digest_under(
    path: Path,
    pattern: NamingPattern,
    tool: ExifTool,
    manifest: Manifest | None = None,
) -> str | None:
    """One file's naming digest under an arbitrary pattern, or ``None``."""
    digests, _ = naming_digests([path], pattern, tool, manifest)
    return digests.get(path)


def _ext(path: Path) -> str:
    return path.suffix.lstrip(".").lower()


def _through_manifest(
    paths: list[Path],
    key: str,
    manifest: Manifest | None,
    full: bool,
    digests: dict[Path, str],
) -> list[Path]:
    """Fill cached digests; return the paths that still need hashing."""
    if manifest is None or full:
        return paths
    misses: list[Path] = []
    for path in paths:
        cached = None
        with suppress(ManifestError):
            cached = manifest.lookup(path, key)
        if cached is not None:
            digests[path] = cached
        else:
            misses.append(path)
    return misses


def _record(
    manifest: Manifest | None,
    path: Path,
    key: str,
    digest: str,
    stat: os.stat_result | None,
) -> None:
    # no pre-computation stat means no safe cache entry: recording a
    # post-computation stat would vouch for content the hash never saw
    if manifest is not None and stat is not None:
        with suppress(ManifestError, OSError):
            manifest.record(path, key, digest, stat=stat)


def _pre_stats(paths: Sequence[Path]) -> dict[Path, os.stat_result]:
    """Each file's stat *before* it is read — see :meth:`Manifest.record`."""
    stats: dict[Path, os.stat_result] = {}
    for path in paths:
        with suppress(OSError):
            stats[path] = path.stat()
    return stats
