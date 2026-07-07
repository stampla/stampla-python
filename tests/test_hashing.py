"""Tests for content hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stampla.hashing import compute_digests, default_workers, hash_files

HELLO_MD5 = "5eb63bbbe01eeed093cb22bb8f5acdc3"
HELLO_SHA256 = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


@pytest.fixture
def hello_file(tmp_path: Path) -> Path:
    path = tmp_path / "hello.bin"
    path.write_bytes(b"hello world")
    return path


class TestComputeDigests:
    def test_known_md5(self, hello_file: Path) -> None:
        assert compute_digests(hello_file, ["md5"]) == {"md5": HELLO_MD5}

    def test_several_digests_single_pass(self, hello_file: Path) -> None:
        result = compute_digests(hello_file, ["md5", "sha256"])
        assert result == {"md5": HELLO_MD5, "sha256": HELLO_SHA256}

    def test_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.bin"
        empty.touch()
        assert compute_digests(empty, ["md5"]) == {"md5": hashlib.md5(b"").hexdigest()}

    def test_chunked_reading_matches_whole_file(self, tmp_path: Path) -> None:
        path = tmp_path / "big.bin"
        payload = bytes(range(256)) * 1000
        path.write_bytes(payload)
        chunked = compute_digests(path, ["sha256"], chunk_size=1024)
        assert chunked == {"sha256": hashlib.sha256(payload).hexdigest()}

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OSError, match=r"big\.bin|No such file|cannot find"):
            compute_digests(tmp_path / "big.bin", ["md5"])


class TestHashFiles:
    def test_parallel_hashing(self, tmp_path: Path) -> None:
        paths = []
        for index in range(5):
            path = tmp_path / f"file{index}.bin"
            path.write_bytes(f"content {index}".encode())
            paths.append(path)
        digests, errors = hash_files(paths, ["md5"], workers=2)
        assert errors == {}
        assert digests == {
            path: {"md5": hashlib.md5(path.read_bytes()).hexdigest()} for path in paths
        }

    def test_single_worker_path(self, hello_file: Path) -> None:
        digests, errors = hash_files([hello_file], ["md5"], workers=1)
        assert digests == {hello_file: {"md5": HELLO_MD5}}
        assert errors == {}

    def test_unreadable_file_is_an_error_entry(self, tmp_path: Path, hello_file: Path) -> None:
        missing = tmp_path / "gone.bin"
        digests, errors = hash_files([hello_file, missing], ["md5"], workers=1)
        assert hello_file in digests
        assert missing in errors
        assert "gone.bin" in errors[missing]

    def test_empty_input(self) -> None:
        assert hash_files([], ["md5"]) == ({}, {})

    def test_workers_never_exceed_files(self, hello_file: Path) -> None:
        digests, _ = hash_files([hello_file], ["md5"], workers=32)
        assert digests[hello_file]["md5"] == HELLO_MD5


def test_default_workers_is_positive() -> None:
    assert default_workers() >= 1
