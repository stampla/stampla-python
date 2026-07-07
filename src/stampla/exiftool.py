"""Driver for ExifTool, the only external tool stampla relies on.

ExifTool is run as a single persistent process (``-stay_open``) so that
querying thousands of files does not pay its startup cost per file.
Metadata is always requested with ``-a -G1``: several tags with the same
name routinely coexist in one file (a maker-notes capture time in local
wall-clock next to a QuickTime one in UTC), and only the group name tells
them apart.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

_INSTALL_HINTS = {
    "darwin": "brew install exiftool",
    "linux": "sudo apt install libimage-exiftool-perl (or your distribution's package)",
    "win32": "choco install exiftool, or download from https://exiftool.org/",
}

_QUERY_CHUNK_SIZE = 500


class ExifToolError(RuntimeError):
    """ExifTool is missing, died, or returned something unusable."""


def find_exiftool() -> str:
    """Locate the exiftool executable or fail with an install hint."""
    executable = shutil.which("exiftool")
    if executable is None:
        hint = _INSTALL_HINTS.get(sys.platform, "see https://exiftool.org/")
        raise ExifToolError(f"exiftool not found on PATH; install it with: {hint}")
    return executable


class ExifTool:
    """A persistent ExifTool process.

    Use as a context manager, or call :meth:`start` and :meth:`stop`.
    """

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=100)
        self._stderr_thread: threading.Thread | None = None
        self._sequence = 0

    def __enter__(self) -> ExifTool:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._process is not None:
            return
        executable = self._executable or find_exiftool()
        self._process = subprocess.Popen(
            [executable, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        try:
            if process.stdin is not None:
                process.stdin.write(b"-stay_open\nFalse\n")
                process.stdin.flush()
                process.stdin.close()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()

    def version(self) -> str:
        """The ExifTool version, e.g. ``13.55``."""
        return self.execute("-ver").strip()

    def execute(self, *args: str) -> str:
        """Run one ExifTool command inside the persistent process."""
        process = self._require_process()
        assert process.stdin is not None
        assert process.stdout is not None
        self._sequence += 1
        marker = f"{{ready{self._sequence}}}"
        payload = "".join(f"{arg}\n" for arg in args) + f"-execute{self._sequence}\n"
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

    def execute_json(self, *args: str) -> list[dict[str, Any]]:
        """Run a command with ``-j`` output and parse it."""
        output = self.execute("-j", *args)
        if not output.strip():
            return []
        try:
            result: list[dict[str, Any]] = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ExifToolError(f"unparsable exiftool output: {output[:200]!r}") from exc
        return result

    def read_metadata(
        self,
        paths: Sequence[Path],
        tags: Iterable[str],
        extra_args: Sequence[str] = (),
    ) -> dict[Path, dict[str, Any]]:
        """Read the given tags from many files, keyed by group-qualified name.

        Files ExifTool cannot read are simply absent from the result; the
        caller decides whether that is an error.
        """
        tag_args = [f"-{tag}" for tag in tags]
        charset_args = ["-charset", "filename=UTF8"] if sys.platform == "win32" else []
        results: dict[Path, dict[str, Any]] = {}
        for start in range(0, len(paths), _QUERY_CHUNK_SIZE):
            chunk = paths[start : start + _QUERY_CHUNK_SIZE]
            entries = self.execute_json(
                "-a",
                "-G1",
                *charset_args,
                *extra_args,
                *tag_args,
                *(str(path) for path in chunk),
            )
            for entry in entries:
                source = entry.pop("SourceFile", None)
                if source is not None:
                    results[Path(source)] = entry
        return results

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None:
            raise ExifToolError("exiftool process is not running; call start() first")
        return self._process

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            self._stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip())

    def _recent_stderr(self) -> str:
        return "\n".join(self._stderr_lines) or "(no stderr output)"
