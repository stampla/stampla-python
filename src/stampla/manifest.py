"""Per-machine hash manifest: skip re-hashing what hasn't changed.

The manifest is a tab-separated file under the archive root, one per
machine (``.stampla/manifest-<machine>.tsv``), so machines that sync
the archive never write to each other's file. Exclude the directory from
sync tools; each machine keeps its own view of "when did I last verify
this file here".

A stored digest is trusted only when both size and mtime still match —
any mtime change means re-hash, never silent trust. Rows are
tab-separated with no quoting; a path containing a tab or newline is
rejected as malformed instead of escaped. Readers tolerate rows with
extra or missing trailing columns so the format can grow.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_COLUMNS = ("path", "size", "mtime_ns", "algo", "digest", "checked_at")


class ManifestError(ValueError):
    """A path that cannot be represented in the manifest."""


@dataclass
class ManifestEntry:
    size: int
    mtime_ns: int
    algo: str
    digest: str
    checked_at: str


def machine_name() -> str:
    """A filesystem-safe name for this machine.

    A clean hostname passes through unchanged. When sanitizing had to
    drop information (foreign characters, excessive length), a short
    hash of the raw name is appended so that two machines can never
    silently converge on the same manifest file.
    """
    raw = platform.node().split(".")[0] or "machine"
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", raw)[:48]
    if safe != raw:
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe.strip('-') or 'machine'}-{digest}"
    return safe


class Manifest:
    """Digest cache for one archive root on one machine."""

    def __init__(self, root: Path, directory: str = ".stampla") -> None:
        self.root = root
        self.path = root / directory / f"manifest-{machine_name()}.tsv"
        self._entries: dict[str, ManifestEntry] = {}
        self._dirty = False

    def __len__(self) -> int:
        return len(self._entries)

    @classmethod
    def load(cls, root: Path, directory: str = ".stampla") -> Manifest:
        manifest = cls(root, directory)
        if not manifest.path.is_file():
            return manifest
        with manifest.path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.rstrip("\n")
                if not line or line.startswith("path\t"):
                    continue
                fields = line.split("\t")
                if len(fields) < 5:
                    continue  # short rows from older versions are skipped
                try:
                    entry = ManifestEntry(
                        size=int(fields[1]),
                        mtime_ns=int(fields[2]),
                        algo=fields[3],
                        digest=fields[4],
                        checked_at=fields[5] if len(fields) > 5 else "",
                    )
                except ValueError:
                    continue
                manifest._entries[fields[0]] = entry
        return manifest

    def lookup(self, path: Path, algorithm: str) -> str | None:
        """The stored digest, if size and mtime still vouch for it."""
        entry = self._entries.get(self._key(path))
        if entry is None or entry.algo != algorithm:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size != entry.size or stat.st_mtime_ns != entry.mtime_ns:
            return None
        return entry.digest

    def record(self, path: Path, algorithm: str, digest: str) -> None:
        """Store a freshly computed digest with the file's current stat."""
        stat = path.stat()
        self._entries[self._key(path)] = ManifestEntry(
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            algo=algorithm,
            digest=digest,
            checked_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._dirty = True

    def save(self) -> None:
        """Write atomically; no-op if nothing changed."""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        scratch = self.path.with_suffix(".tsv.tmp")
        with scratch.open("w", encoding="utf-8") as stream:
            stream.write("\t".join(_COLUMNS) + "\n")
            for key in sorted(self._entries):
                entry = self._entries[key]
                stream.write(
                    f"{key}\t{entry.size}\t{entry.mtime_ns}\t{entry.algo}"
                    f"\t{entry.digest}\t{entry.checked_at}\n"
                )
        os.replace(scratch, self.path)
        self._dirty = False

    def _key(self, path: Path) -> str:
        relative = path.relative_to(self.root).as_posix()
        if "\t" in relative or "\n" in relative:
            raise ManifestError(f"path contains tab or newline: {relative!r}")
        return relative
