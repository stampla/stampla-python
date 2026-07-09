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
        assert payload["format"] == 1
        assert payload["root"] == str(archive.resolve())
        by_name = buckets_of(payload)
        assert by_name[renamed.name] == "date-mismatch"
        assert by_name[corrupted.name] == "corruption"
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert not Path(str(findings[0]["path"])).is_absolute()  # root-relative
        by_path = {Path(str(f["path"])).name: f for f in findings}
        mismatch_data = by_path[renamed.name]["data"]
        assert mismatch_data["name_datetime"] == "20260106_110000"
        assert mismatch_data["metadata_datetime"] == "20260106_100000"
        assert "DateTimeOriginal" in str(mismatch_data["source"])
        corruption_data = by_path[corrupted.name]["data"]
        assert corruption_data["name_digest"] == "deadbeef"
        assert str(by_path[corrupted.name]["detail"]).endswith(
            str(corruption_data["content_digest"])
        )
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

    def test_manifest_is_created_and_reused(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-06"
        master = make_master(month, "2026:06:01 10:00:00")
        assert run_cli(archive)[0] == 0
        manifests = list((archive / ".stampla").glob("manifest-*.tsv"))
        assert len(manifests) == 1
        assert master.name in manifests[0].read_text()

        # Doctor the content while faking the original size and mtime: the
        # manifest cannot see this (documented trust boundary), --full can.
        stat = master.stat()
        payload = bytearray(master.read_bytes())
        payload[-1] ^= 0xFF
        master.write_bytes(payload)
        import os

        os.utime(master, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert run_cli(archive)[0] == 0  # cached digest still trusted
        code, payload_json = run_cli(archive, "--full")
        assert code == 1
        assert buckets_of(payload_json)[master.name] == "corruption"

    def test_touched_file_is_rehashed(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-07"
        master = make_master(month, "2026:07:01 10:00:00")
        assert run_cli(archive)[0] == 0
        content = bytearray(master.read_bytes())
        content[-1] ^= 0xFF
        master.write_bytes(content)  # mtime changes naturally
        code, payload = run_cli(archive)
        assert code == 1
        assert buckets_of(payload)[master.name] == "corruption"

    def test_no_manifest_flag(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-08"
        make_master(month, "2026:08:01 10:00:00")
        assert run_cli(archive, "--no-manifest")[0] == 0
        assert not (archive / ".stampla").exists()

    def test_file_path_argument_is_an_error(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-12"
        master = make_master(month, "2026:12:05 12:30:00")
        code = main(["verify", str(master), "--config", str(archive / "config.toml")])
        assert code == 2

    def test_part_leftover_is_explained(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-11"
        make_master(month, "2026:11:05 12:30:00")
        (month / "somecopy.jpg.1234.part").write_bytes(b"torn")
        code, payload = run_cli(archive)
        assert code == 1
        findings = payload["findings"]
        assert isinstance(findings, list)
        leftover = [f for f in findings if str(f["path"]).endswith(".part")]
        assert "interrupted copy" in str(leftover[0]["detail"])

    def test_nothing_to_verify_is_an_error(self, archive: Path) -> None:
        code = main(["verify", "--config", str(archive / "config.toml")])
        assert code == 2

    def test_json_stream_emits_progress_then_result(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-09"
        make_master(month, "2026:09:05 12:30:00")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["verify", "--config", str(archive / "config.toml"), "--json-stream"])
        assert code == 0
        lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line]
        assert all("event" in line for line in lines)
        assert any(line["event"] == "progress" for line in lines)
        result = lines[-1]
        assert result["event"] == "result"
        assert result["format"] == 1
        assert result["summary"]["ok"] == 1


def image_hash_of(path: Path) -> str:
    return subprocess.run(
        ["exiftool", "-api", "imagehashtype=MD5", "-s3", "-ImageDataHash", str(path)],
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_image_master(directory: Path, capture: str, seasoning: bytes = b"") -> Path:
    """A canonical master named by its image-data hash."""
    directory.mkdir(parents=True, exist_ok=True)
    scratch = directory / "scratch.jpg"
    scratch.write_bytes(TINY_JPEG + seasoning)
    exiftool(f"-EXIF:DateTimeOriginal={capture}", str(scratch))
    compact = capture.replace(":", "").replace(" ", "_")
    named = directory / f"{compact}_{image_hash_of(scratch)[:8]}.jpg"
    scratch.rename(named)
    return named


HYBRID_CONFIG = (
    CONFIG_TEMPLATE
    + """
[pattern]
name = "md5-hybrid"
image_hash = ["jpg", "jpeg"]

[[pattern.additional]]
name = "md5-file"
"""
)


@requires_exiftool
class TestHybridPattern:
    @pytest.fixture
    def hybrid_archive(self, tmp_path: Path) -> Path:
        (tmp_path / "config.toml").write_text(HYBRID_CONFIG.format(root=str(tmp_path)))
        return tmp_path

    def test_metadata_edits_do_not_drift_names(self, hybrid_archive: Path) -> None:
        month = hybrid_archive / "Photos" / "2026" / "2026-01"
        master = make_image_master(month, "2026:01:05 12:30:00")
        exiftool("-XMP-dc:Subject+=holiday", "-XMP-xmp:Rating=5", str(master))

        code, payload = run_cli(hybrid_archive)
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0, payload
        assert summary["ok"] == 1

    def test_image_data_change_is_corruption(self, hybrid_archive: Path) -> None:
        month = hybrid_archive / "Photos" / "2026" / "2026-02"
        master = make_image_master(month, "2026:02:05 12:30:00")
        payload_bytes = bytearray(master.read_bytes())
        # flip one bit inside the entropy-coded data, just before EOI,
        # avoiding bytes that could form or break a JPEG marker
        index = len(payload_bytes) - 4
        while payload_bytes[index] in (0xFF, 0xFE, 0x00) or payload_bytes[index - 1] == 0xFF:
            index -= 1
        payload_bytes[index] ^= 0x01
        master.write_bytes(payload_bytes)

        code, payload = run_cli(hybrid_archive)
        assert code == 1
        assert buckets_of(payload)[master.name] == "corruption"

    def test_path_scoping_limits_verification(self, hybrid_archive: Path) -> None:
        january = hybrid_archive / "Photos" / "2026" / "2026-01"
        february = hybrid_archive / "Photos" / "2026" / "2026-02"
        make_image_master(january, "2026:01:05 12:30:00")
        make_image_master(february, "2026:02:05 12:30:00")
        code, payload = run_cli(hybrid_archive, str(january))
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0
        assert summary["scanned"] == 1  # february untouched

    def test_ambiguous_master_with_skip_hash_says_so(self, hybrid_archive: Path) -> None:
        config = hybrid_archive / "config.toml"
        config.write_text(config.read_text().replace('raw = ["jpg"]', 'raw = ["jpg", "nef"]'))
        month = hybrid_archive / "Photos" / "2026" / "2026-09"
        master = make_image_master(month, "2026:09:05 12:30:00")
        twin = month / (master.stem + ".nef")  # second master candidate, same prefix
        twin.write_bytes(b"conversion")
        code, payload = run_cli(hybrid_archive, "--skip-hash")
        assert code == 1
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "ambiguous-master"
        assert "--skip-hash" in str(findings[0]["detail"])

    def test_junk_master_is_reported_undatable(self, hybrid_archive: Path) -> None:
        config = hybrid_archive / "config.toml"
        config.write_text(config.read_text().replace('raw = ["jpg"]', 'raw = ["jpg", "nef"]'))
        month = hybrid_archive / "Photos" / "2026" / "2026-10"
        month.mkdir(parents=True)
        # a "nef" of pure junk: exiftool enumerates it but finds no dates
        junk = month / "20261005_123000_deadbeef.nef"
        junk.write_bytes(b"\x00\x01\x02not media")
        code, payload = run_cli(hybrid_archive)
        assert code == 1
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "unresolved-date"

    def test_file_hash_named_master_is_other_pattern(self, hybrid_archive: Path) -> None:
        # named under the additional whole-file pattern: pending migration,
        # neither drift nor corruption
        month = hybrid_archive / "Photos" / "2026" / "2026-03"
        master = make_master(month, "2026:03:05 12:30:00")

        code, payload = run_cli(hybrid_archive)
        assert code == 1
        by_name = buckets_of(payload)
        assert by_name[master.name] == "other-pattern"

    def test_missing_root_is_an_error(self, tmp_path: Path) -> None:
        config = tmp_path / "no-root.toml"
        config.write_text('[[trees]]\npath = "Photos"\nmedia = "photo"\n')
        code = main(["verify", "--config", str(config)])
        assert code == 2
