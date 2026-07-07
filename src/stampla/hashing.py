"""Content hashing: streamed, parallel, several digests per read.

Hashing is CPU-bound and is the throughput bottleneck on fast storage, so
files are hashed in parallel worker processes. When more than one digest
of a file is needed (verifying an old hash while computing a new one
during a pattern migration), all of them are fed from a single read pass —
the file is never read twice.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_CHUNK_SIZE = 8 * 1024 * 1024


def compute_digests(
    path: Path, algorithms: Sequence[str], chunk_size: int = _CHUNK_SIZE
) -> dict[str, str]:
    """Hexdigests of one file for every algorithm, in a single read pass."""
    hashers = [hashlib.new(algorithm) for algorithm in algorithms]
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            for hasher in hashers:
                hasher.update(chunk)
    return {
        algorithm: hasher.hexdigest() for algorithm, hasher in zip(algorithms, hashers, strict=True)
    }


def default_workers() -> int:
    """Leave one core for the rest of the pipeline."""
    # os.process_cpu_count() respects CPU affinity but needs Python 3.13
    count = getattr(os, "process_cpu_count", os.cpu_count)()
    return max(1, (count or 2) - 1)


def hash_files(
    paths: Sequence[Path],
    algorithms: Sequence[str] = ("md5",),
    workers: int | None = None,
) -> tuple[dict[Path, dict[str, str]], dict[Path, str]]:
    """Hash many files in parallel.

    Returns ``(digests, errors)``: per-path digest mappings for files that
    could be read, and per-path error messages for those that could not.
    Unreadable files are the caller's judgement call, not an exception.

    Worker processes are started with spawn semantics on some platforms,
    so a script calling this must be importable — keep the call under
    ``if __name__ == "__main__":`` as with any multiprocessing code.
    """
    digests: dict[Path, dict[str, str]] = {}
    errors: dict[Path, str] = {}
    if not paths:
        return digests, errors
    worker_count = min(workers or default_workers(), len(paths))
    if worker_count == 1:
        for path in paths:
            _collect(path, algorithms, digests, errors)
        return digests, errors
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        results = executor.map(_hash_one, ((path, tuple(algorithms)) for path in paths))
        for path, result in zip(paths, results, strict=True):
            if isinstance(result, dict):
                digests[path] = result
            else:
                errors[path] = result
    return digests, errors


def _collect(
    path: Path,
    algorithms: Sequence[str],
    digests: dict[Path, dict[str, str]],
    errors: dict[Path, str],
) -> None:
    result = _hash_one((path, tuple(algorithms)))
    if isinstance(result, dict):
        digests[path] = result
    else:
        errors[path] = result


def _hash_one(job: tuple[Path, tuple[str, ...]]) -> dict[str, str] | str:
    path, algorithms = job
    try:
        return compute_digests(path, algorithms)
    except OSError as exc:
        return str(exc)
