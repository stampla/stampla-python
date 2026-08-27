"""Tests for the naming digest service."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from stampla.digests import naming_digests
from stampla.exiftool import ExifTool
from stampla.manifest import Manifest
from stampla.pattern import NamingPattern
from tests.test_verify import TINY_JPEG

requires_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

HYBRID = NamingPattern(name="md5-hybrid", image_hash=frozenset({"jpg", "jpeg"}))


@pytest.fixture(scope="module")
def tool() -> Iterator[ExifTool]:
    with ExifTool() as running:
        yield running


def image_hash_of(path: Path) -> str:
    return subprocess.run(
        ["exiftool", "-api", "imagehashtype=MD5", "-s3", "-ImageDataHash", str(path)],
        capture_output=True,
        text=True,
    ).stdout.strip()


@requires_exiftool
class TestNamingDigests:
    def test_partition_by_source(self, tool: ExifTool, tmp_path: Path) -> None:
        photo = tmp_path / "a.jpg"
        photo.write_bytes(TINY_JPEG)
        blob = tmp_path / "b.nef"
        blob.write_bytes(b"raw bytes")

        digests, errors = naming_digests([photo, blob], HYBRID, tool)
        assert errors == {}
        assert digests[photo] == image_hash_of(photo)
        assert digests[blob] == hashlib.md5(b"raw bytes").hexdigest()
        assert digests[photo] != hashlib.md5(TINY_JPEG).hexdigest()

    def test_metadata_write_keeps_image_digest(self, tool: ExifTool, tmp_path: Path) -> None:
        photo = tmp_path / "a.jpg"
        photo.write_bytes(TINY_JPEG)
        before, _ = naming_digests([photo], HYBRID, tool)
        subprocess.run(
            ["exiftool", "-q", "-overwrite_original", "-XMP-dc:Subject+=kw", str(photo)],
            check=True,
        )
        after, _ = naming_digests([photo], HYBRID, tool)
        assert before[photo] == after[photo]

    def test_manifest_caches_image_digests(self, tool: ExifTool, tmp_path: Path) -> None:
        root = tmp_path / "archive"
        root.mkdir()
        photo = root / "a.jpg"
        photo.write_bytes(TINY_JPEG)
        manifest = Manifest.load(root)

        first, _ = naming_digests([photo], HYBRID, tool, manifest=manifest)
        manifest.save()
        assert "md5-image" in manifest.path.read_text()

        second, _ = naming_digests([photo], HYBRID, tool, manifest=Manifest.load(root))
        assert first == second

    def test_unhashable_image_format_is_an_error(self, tool: ExifTool, tmp_path: Path) -> None:
        pattern = NamingPattern(name="odd", image_hash=frozenset({"xyz"}))
        weird = tmp_path / "t.xyz"
        weird.write_text("not an image")
        digests, errors = naming_digests([weird], pattern, tool)
        assert digests == {}
        assert "no image-data hash" in errors[weird]
