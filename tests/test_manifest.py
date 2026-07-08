"""Tests for the per-machine hash manifest."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stampla.manifest import Manifest, ManifestError, machine_name


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "Photos").mkdir()
    return tmp_path


def make_file(root: Path, name: str, content: bytes = b"data") -> Path:
    path = root / "Photos" / name
    path.write_bytes(content)
    return path


class TestRoundTrip:
    def test_record_save_load_lookup(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        manifest.save()

        reloaded = Manifest.load(root)
        assert len(reloaded) == 1
        assert reloaded.lookup(path, "md5") == "abc123"

    def test_manifest_file_is_per_machine(self, root: Path) -> None:
        manifest = Manifest(root)
        assert manifest.path.name == f"manifest-{machine_name()}.tsv"
        assert manifest.path.parent == root / ".stampla"

    def test_save_without_changes_writes_nothing(self, root: Path) -> None:
        manifest = Manifest.load(root)
        manifest.save()
        assert not manifest.path.exists()


class TestTrustBoundary:
    def test_mtime_change_invalidates(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000_000))
        assert manifest.lookup(path, "md5") is None

    def test_size_change_invalidates(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        stat = path.stat()
        path.write_bytes(b"different content")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same mtime, new size
        assert manifest.lookup(path, "md5") is None

    def test_different_algorithm_misses(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        assert manifest.lookup(path, "sha256") is None

    def test_missing_file_misses(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        path.unlink()
        assert manifest.lookup(path, "md5") is None


class TestFormat:
    def test_non_ascii_paths_round_trip(self, root: Path) -> None:
        (root / "Zdjęcia").mkdir()
        path = root / "Zdjęcia" / "zdjęcie ąśź.nef"
        path.write_bytes(b"x")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        manifest.save()
        assert Manifest.load(root).lookup(path, "md5") == "abc123"

    def test_short_rows_are_tolerated(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        manifest.save()
        # simulate an older version's file: drop the last column
        lines = manifest.path.read_text().splitlines()
        lines[1] = "\t".join(lines[1].split("\t")[:5])
        manifest.path.write_text("\n".join(lines) + "\n")
        assert Manifest.load(root).lookup(path, "md5") == "abc123"

    def test_extra_future_columns_are_tolerated(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        manifest.save()
        lines = manifest.path.read_text().splitlines()
        lines[1] += "\tfuture-column-value"
        manifest.path.write_text("\n".join(lines) + "\n")
        assert Manifest.load(root).lookup(path, "md5") == "abc123"

    def test_garbage_rows_are_skipped(self, root: Path) -> None:
        manifest = Manifest(root)
        manifest.path.parent.mkdir(parents=True)
        manifest.path.write_text(
            "path\tsize\tmtime_ns\talgo\tdigest\tchecked_at\n"
            "broken row without tabs\n"
            "x.nef\tnot-a-number\t0\tmd5\tabc\t\n"
        )
        assert len(Manifest.load(root)) == 0

    def test_out_of_root_path_is_a_manifest_error(self, root: Path) -> None:
        narrow_root = root / "Photos"
        outside = root / "elsewhere.nef"
        outside.write_bytes(b"x")
        manifest = Manifest.load(narrow_root)
        with pytest.raises(ManifestError, match="outside"):
            manifest.lookup(outside, "md5")

    def test_old_trusted_entries_are_counted_stale(self, root: Path) -> None:
        path = make_file(root, "a.nef")
        manifest = Manifest.load(root)
        manifest.record(path, "md5", "abc123")
        manifest.save()
        aged = manifest.path.read_text().replace(
            manifest.path.read_text().splitlines()[1].split("\t")[5],
            "2020-01-01T00:00:00Z",
        )
        manifest.path.write_text(aged)

        reloaded = Manifest.load(root)
        assert reloaded.lookup(path, "md5") == "abc123"  # still trusted
        assert reloaded.stale_trusted == 1  # but counted

    def test_tab_in_path_is_rejected(self, root: Path) -> None:
        # No file is created: Windows cannot even represent this name,
        # and rejection must not depend on the file existing.
        weird = root / "Photos" / "a\tb.nef"
        manifest = Manifest.load(root)
        with pytest.raises(ManifestError, match="tab"):
            manifest.lookup(weird, "md5")
        with pytest.raises(ManifestError, match="tab"):
            manifest.record(weird, "md5", "abc123")

    def test_machine_name_is_filesystem_safe(self) -> None:
        assert machine_name()
        assert "/" not in machine_name()
        assert " " not in machine_name()


class TestMachineName:
    def name_for(self, monkeypatch: pytest.MonkeyPatch, node: str) -> str:
        import platform

        monkeypatch.setattr(platform, "node", lambda: node)
        return machine_name()

    def test_clean_hostname_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self.name_for(monkeypatch, "cider") == "cider"
        assert self.name_for(monkeypatch, "DESKTOP-AB12CD") == "DESKTOP-AB12CD"

    def test_fqdn_uses_first_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self.name_for(monkeypatch, "studio.example.com") == "studio"

    def test_empty_hostname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self.name_for(monkeypatch, "") == "machine"

    def test_lossy_names_get_disambiguating_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = self.name_for(monkeypatch, "ネットワーク")
        second = self.name_for(monkeypatch, "сервер")
        assert first != second
        assert first.startswith("machine-")
        assert second.startswith("machine-")

    def test_spaces_are_sanitized_with_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        name = self.name_for(monkeypatch, "my host")
        assert name.startswith("my-host-")
        assert " " not in name

    def test_long_hostnames_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        name = self.name_for(monkeypatch, "x" * 300)
        assert len(name) <= 60
        assert name != self.name_for(monkeypatch, "x" * 299)

    def test_result_is_deterministic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self.name_for(monkeypatch, "zażółć") == self.name_for(monkeypatch, "zażółć")
