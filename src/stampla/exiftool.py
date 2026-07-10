"""Driver for ExifTool, the only external tool stampla relies on.

ExifTool is run as persistent processes (``-stay_open``) so that
querying thousands of files does not pay its startup cost per file.
One process is single-threaded Perl, so bulk reads shard the file list
across a small pool of them; writes stay serialized through the first.
Metadata is always requested with ``-a`` and tag-group-qualified names:
several tags with the same name routinely coexist in one file (a
maker-notes capture time in local wall-clock next to a QuickTime one in
UTC), and only the tag-group name tells them apart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_INSTALL_HINTS = {
    "darwin": "brew install exiftool",
    "linux": "sudo apt install libimage-exiftool-perl (or your distribution's package)",
    "win32": "choco install exiftool, or download from https://exiftool.org/",
}

_QUERY_CHUNK_SIZE = 500

#: enough to keep a fast disk busy without a wall of Perl processes
_DEFAULT_POOL_SIZE = 8


class ExifToolError(RuntimeError):
    """ExifTool is missing, died, or returned something unusable."""


def find_exiftool() -> str:
    """Locate the exiftool executable or fail with an install hint."""
    executable = shutil.which("exiftool")
    if executable is None:
        hint = _INSTALL_HINTS.get(sys.platform, "see https://exiftool.org/")
        raise ExifToolError(f"exiftool not found on PATH; install it with: {hint}")
    return executable


class _Worker:
    """One persistent ExifTool process."""

    def __init__(self, executable: str) -> None:
        self.process = subprocess.Popen(
            [executable, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.stderr_lines: deque[str] = deque(maxlen=100)
        self.sequence = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def stop(self) -> None:
        process = self.process
        try:
            if process.stdin is not None:
                process.stdin.write(b"-stay_open\nFalse\n")
                process.stdin.flush()
                process.stdin.close()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()

    def execute(self, *args: str) -> str:
        with self.lock:
            process = self.process
            assert process.stdin is not None
            assert process.stdout is not None
            self.sequence += 1
            marker = f"{{ready{self.sequence}}}"
            payload = "".join(f"{arg}\n" for arg in args) + f"-execute{self.sequence}\n"
            try:
                process.stdin.write(payload.encode("utf-8"))
                process.stdin.flush()
            except OSError as exc:
                raise ExifToolError(f"exiftool process is gone: {self._recent_stderr()}") from exc

            lines: list[str] = []
            while True:
                raw = process.stdout.readline()
                if not raw:
                    raise ExifToolError(f"exiftool exited unexpectedly: {self._recent_stderr()}")
                line = raw.decode("utf-8", errors="replace")
                if line.strip() == marker:
                    return "".join(lines)
                lines.append(line)

    def _drain_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for raw in self.process.stderr:
            self.stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip())

    def _recent_stderr(self) -> str:
        return "\n".join(self.stderr_lines) or "(no stderr output)"


class ExifTool:
    """A pool of persistent ExifTool processes behind one interface.

    Bulk reads (:meth:`read_metadata`, :meth:`read_image_hashes`) shard
    their file lists across the pool; single commands and writes go
    through one process. Use as a context manager, or call
    :meth:`start` and :meth:`stop`.
    """

    def __init__(self, executable: str | None = None, workers: int | None = None) -> None:
        self._executable = executable
        cores = getattr(os, "process_cpu_count", os.cpu_count)() or 4
        self._size = max(1, workers if workers is not None else min(_DEFAULT_POOL_SIZE, cores))
        self._workers: list[_Worker] = []

    def __enter__(self) -> ExifTool:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._workers:
            return
        executable = self._executable or find_exiftool()
        self._workers = [_Worker(executable) for _ in range(self._size)]

    def stop(self) -> None:
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.stop()

    def version(self) -> str:
        """The ExifTool version, e.g. ``13.55``."""
        return self.execute("-ver").strip()

    def execute(self, *args: str) -> str:
        """Run one ExifTool command; commands are serialized, in order."""
        return self._require_workers()[0].execute(*args)

    def execute_json(self, *args: str) -> list[dict[str, Any]]:
        """Run a command with ``-j`` output and parse it."""
        return _parse_json(self.execute("-j", *args))

    def read_metadata(
        self,
        paths: Sequence[Path],
        tags: Iterable[str],
        extra_args: Sequence[str] = (),
        group_level: int = 0,
    ) -> dict[Path, dict[str, Any]]:
        """Read the given tags from many files, keyed by tag-group-qualified name.

        ExifTool's family-0 tag groups give the coarse names date resolution ranks by
        (``EXIF``, ``XMP``, ``MakerNotes``, ``QuickTime``); pass a different
        level for finer tag-group names.

        Files ExifTool cannot read are simply absent from the result; the
        caller decides whether that is an error.
        """
        tag_args = [f"-{tag}" for tag in tags]
        query = ["-a", f"-G{group_level}", *_charset_args(), *extra_args, *tag_args]

        def collect(entries: list[dict[str, Any]], results: dict[Path, dict[str, Any]]) -> None:
            for entry in entries:
                source = entry.pop("SourceFile", None)
                if source is not None:
                    results[Path(source)] = entry

        return self._sharded_query(paths, query, collect)

    def read_image_hashes(self, paths: Sequence[Path], algorithm: str = "md5") -> dict[Path, str]:
        """Digest of each file's image data only, metadata excluded.

        Files whose format has no image-data stream ExifTool can hash are
        simply absent from the result.
        """
        type_names = {"md5": "MD5", "sha256": "SHA256", "sha512": "SHA512"}
        if algorithm not in type_names:
            raise ExifToolError(f"image hashing does not support {algorithm!r}")
        query = [
            "-api",
            f"imagehashtype={type_names[algorithm]}",
            *_charset_args(),
            "-ImageDataHash",
        ]

        def collect(entries: list[dict[str, Any]], results: dict[Path, str]) -> None:
            for entry in entries:
                value = entry.get("ImageDataHash")
                source = entry.get("SourceFile")
                if isinstance(value, str) and value and source is not None:
                    results[Path(source)] = value.lower()

        return self._sharded_query(paths, query, collect)

    def _sharded_query(
        self,
        paths: Sequence[Path],
        query: Sequence[str],
        collect: Any,
    ) -> dict[Path, Any]:
        """Fan a chunked file query out across the pool and merge results."""
        workers = self._require_workers()
        if not paths:
            return {}
        # small batches shrink the chunk so every worker gets a share
        chunk_size = max(1, min(_QUERY_CHUNK_SIZE, -(-len(paths) // len(workers))))
        chunks = [paths[start : start + chunk_size] for start in range(0, len(paths), chunk_size)]
        results: dict[Path, Any] = {}
        if not chunks:
            return results

        def run(index: int, chunk: Sequence[Path]) -> list[dict[str, Any]]:
            worker = workers[index % len(workers)]
            return _parse_json(worker.execute("-j", *query, *(str(path) for path in chunk)))

        if len(chunks) == 1:
            collect(run(0, chunks[0]), results)
            return results
        with ThreadPoolExecutor(max_workers=len(workers)) as pool:
            for entries in pool.map(run, range(len(chunks)), chunks):
                collect(entries, results)
        return results

    def _require_workers(self) -> list[_Worker]:
        if not self._workers:
            raise ExifToolError("exiftool process is not running; call start() first")
        return self._workers


def _charset_args() -> list[str]:
    return ["-charset", "filename=UTF8"] if sys.platform == "win32" else []


def _parse_json(output: str) -> list[dict[str, Any]]:
    if not output.strip():
        return []
    try:
        result: list[dict[str, Any]] = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ExifToolError(f"unparsable exiftool output: {output[:200]!r}") from exc
    return result
