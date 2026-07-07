"""Tests for the verify command.

End-to-end tests build a miniature archive from a real 1x1 JPEG with
EXIF written by ExifTool, then run verify through the public CLI.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from stampla.cli import main

requires_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

# A minimal valid 1x1 JPEG.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHR"
    "ofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QA"
    "FAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN"
    "//2Q=="
)

CONFIG_TEMPLATE = """
root = {root!r}

[[trees]]
path = "Photos"
media = "photo"

[extensions]
raw = ["jpg"]
mutable = []
"""


def exiftool(*args: str) -> None:
    subprocess.run(["exiftool", "-q", "-overwrite_original", *args], check=True)


def make_master(directory: Path, capture: str, seasoning: bytes = b"") -> Path:
    """Create a JPEG with the given capture time, named canonically."""
    directory.mkdir(parents=True, exist_ok=True)
    scratch = directory / "scratch.jpg"
    scratch.write_bytes(TINY_JPEG + seasoning)
    exiftool(f"-EXIF:DateTimeOriginal={capture}", str(scratch))
    digest = hashlib.md5(scratch.read_bytes()).hexdigest()
    compact = capture.replace(":", "").replace(" ", "_")
    named = directory / f"{compact}_{digest[:8]}.jpg"
    scratch.rename(named)
    return named


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TEMPLATE.format(root=str(tmp_path)))
    return tmp_path


def run_cli(archive: Path, *extra: str) -> tuple[int, dict[str, object]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["verify", "--config", str(archive / "config.toml"), "--json", *extra])
    return code, json.loads(buffer.getvalue())


def buckets_of(payload: dict[str, object]) -> dict[str, str]:
    findings = payload["findings"]
    assert isinstance(findings, list)
    return {Path(str(f["path"])).name: str(f["bucket"]) for f in findings}


@requires_exiftool
class TestVerifyEndToEnd:
    def test_clean_archive_exits_zero(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-01"
        master = make_master(month, "2026:01:05 12:30:00")
        (month / f"{master.stem}.xmp").write_text("<x:xmpmeta/>")
        code, payload = run_cli(archive)
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0, payload
        assert summary["ok"] == 1
        assert summary["scanned"] == 2

    def test_buckets(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-01"
        # ok
        make_master(month, "2026:01:05 12:30:00")
        # date-mismatch: name one hour off
        wrong_date = make_master(month, "2026:01:06 10:00:00", b"a")
        renamed = month / ("20260106_110000_" + wrong_date.name.split("_")[2])
        wrong_date.rename(renamed)
        # corruption: hash in name is wrong, jpg configured immutable
        wrong_hash = make_master(month, "2026:01:07 09:00:00", b"b")
        corrupted = month / ("20260107_090000_deadbeef.jpg")
        wrong_hash.rename(corrupted)
        # unresolved: no date metadata at all
        undated = month / "20260108_080000_cafecafe.jpg"
        undated.write_bytes(TINY_JPEG)
        # unnamed and malformed
        (month / "DSC_1234.jpg").write_bytes(TINY_JPEG)
        (month / "20260109_070000_0badc0de(1).jpg").write_bytes(TINY_JPEG)
        # orphan sidecar
        (month / "20260110_060000_feedface.xmp").write_text("<x:xmpmeta/>")

        code, payload = run_cli(archive)
        assert code == 1
        by_name = buckets_of(payload)
        assert by_name[renamed.name] == "date-mismatch"
        assert by_name[corrupted.name] == "corruption"
        assert by_name[undated.name] == "unresolved-date"
        assert by_name["DSC_1234.jpg"] == "unnamed"
        assert by_name["20260109_070000_0badc0de(1).jpg"] == "malformed"
        assert by_name["20260110_060000_feedface.xmp"] == "orphan-family"
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert summary["ok"] == 1

    def test_collision_between_identical_masters(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-02"
        first = make_master(month, "2026:02:01 10:00:00")
        clone = month / ("20260201_100000_00000000.jpg")
        clone.write_bytes(first.read_bytes())
        code, payload = run_cli(archive)
        assert code == 1
        assert sorted(buckets_of(payload).values()).count("collision") == 2

    def test_edit_drift_with_mutable_extension(self, archive: Path, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            CONFIG_TEMPLATE.format(root=str(tmp_path)).replace("mutable = []", 'mutable = ["jpg"]')
        )
        month = archive / "Photos" / "2026" / "2026-03"
        drifted = make_master(month, "2026:03:01 08:00:00")
        renamed = month / "20260301_080000_0ddba11e.jpg"
        drifted.rename(renamed)
        code, payload = run_cli(archive)
        assert code == 1
        assert buckets_of(payload)[renamed.name] == "edit-drift"

    def test_skip_hash_catches_dates_not_content(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-04"
        wrong_hash = make_master(month, "2026:04:01 08:00:00")
        renamed = month / "20260401_080000_0badf00d.jpg"
        wrong_hash.rename(renamed)
        wrong_date = make_master(month, "2026:04:02 09:00:00", b"c")
        redated = month / ("20260402_100000_" + wrong_date.name.split("_")[2])
        wrong_date.rename(redated)

        code, payload = run_cli(archive, "--skip-hash")
        assert code == 1
        by_name = buckets_of(payload)
        assert by_name[redated.name] == "date-mismatch"
        assert renamed.name not in by_name  # content not checked

    def test_ambiguous_master_settled_by_hash(self, archive: Path, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            CONFIG_TEMPLATE.format(root=str(tmp_path)).replace(
                'raw = ["jpg"]', 'raw = ["jpg", "dng"]'
            )
        )
        month = archive / "Photos" / "2026" / "2026-05"
        master = make_master(month, "2026:05:01 07:00:00")
        conversion = month / (master.stem + ".dng")
        conversion.write_bytes(master.read_bytes() + b"converted")
        code, payload = run_cli(archive)
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0, payload
        assert summary["ok"] == 1

    def test_nothing_to_verify_is_an_error(self, archive: Path) -> None:
        code = main(["verify", "--config", str(archive / "config.toml")])
        assert code == 2

    def test_missing_root_is_an_error(self, tmp_path: Path) -> None:
        config = tmp_path / "no-root.toml"
        config.write_text('[[trees]]\npath = "Photos"\nmedia = "photo"\n')
        code = main(["verify", "--config", str(config)])
        assert code == 2
