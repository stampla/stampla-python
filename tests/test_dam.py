"""Tests for DAM token injection."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from stampla.cli import main
from tests.test_verify import TINY_JPEG, make_master

requires_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

CONFIG_TEMPLATE = """
root = {root!r}

[[trees]]
path = "Photos"
media = "photo"

[extensions]
raw = ["jpg", "nef"]
mutable = ["jpg"]

[dam]
trees = ["Photos"]
"""


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text(CONFIG_TEMPLATE.format(root=str(tmp_path)))
    return tmp_path


def run_inject(archive: Path, *extra: str) -> tuple[int, dict[str, object]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["inject", "--config", str(archive / "config.toml"), "--json", *extra])
    return code, json.loads(buffer.getvalue())


def findings_of(payload: dict[str, object]) -> list[dict[str, object]]:
    findings = payload["findings"]
    assert isinstance(findings, list)
    return findings


def read_token(path: Path) -> str | None:
    out = subprocess.run(
        ["exiftool", "-s3", "-XMP-photoshop:TransmissionReference", str(path)],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out or None


def dump_tags(path: Path) -> set[str]:
    out = subprocess.run(
        ["exiftool", "-a", "-G1", "-s", str(path)], capture_output=True, text=True
    ).stdout
    return {
        line.strip()
        for line in out.splitlines()
        if not line.startswith(("[ExifTool]", "[File]", "[System]"))
    }


def make_drifted_master(month: Path, capture: str) -> Path:
    """A canonical master whose name hash no longer matches its content."""
    master = make_master(month, capture)
    stale = month / (master.name.rsplit("_", 1)[0] + "_0ddc0ffe.jpg")
    master.rename(stale)
    return stale


@requires_exiftool
class TestInject:
    def test_clean_archive_needs_no_tokens(self, archive: Path) -> None:
        make_master(archive / "Photos" / "2026" / "2026-01", "2026:01:05 12:30:00")
        code, payload = run_inject(archive)
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0
        assert summary["ok"] == 1
        assert findings_of(payload) == []

    def test_dry_run_shows_pending_token_without_writing(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-02"
        stale = make_drifted_master(month, "2026:02:01 10:00:00")
        code, payload = run_inject(archive)
        assert code == 0
        findings = findings_of(payload)
        assert findings[0]["bucket"] == "token-pending"
        data = findings[0]["data"]
        assert isinstance(data, dict)
        assert data["embedded"] is True
        assert str(data["token"]) in str(findings[0]["detail"])
        assert read_token(stale) is None

    def test_apply_writes_token_into_embedded_master(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-03"
        stale = make_drifted_master(month, "2026:03:01 10:00:00")
        code, payload = run_inject(archive, "--apply")
        assert code == 0, payload
        findings = findings_of(payload)
        assert findings[0]["bucket"] == "token-written"
        token = read_token(stale)
        assert token is not None
        assert token.startswith("20260301_100000_")
        assert token != stale.name.rsplit(".", 1)[0]

    def test_apply_writes_token_into_sidecar_for_raw(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-04"
        month.mkdir(parents=True)
        # a "nef" master (jpeg payload; codec irrelevant) with LrC sidecar
        scratch = make_master(month, "2026:04:01 09:00:00")
        raw = month / (scratch.name.rsplit("_", 1)[0] + "_0ddc0ffe.nef")
        scratch.rename(raw)
        sidecar = raw.with_suffix(".xmp")
        subprocess.run(["exiftool", "-q", "-o", str(sidecar), "-XMP-dc:Title=keepme"], check=True)

        before = dump_tags(sidecar)
        code, payload = run_inject(archive, "--apply")
        assert code == 0, payload
        assert findings_of(payload)[0]["bucket"] == "token-written"

        token = read_token(sidecar)
        assert token is not None
        assert token.startswith("20260401_090000_")
        assert read_token(raw) is None  # the RAW itself is untouched

        # the pilot's guarantee, automated: only the token tag and the
        # toolkit signature may differ
        after = dump_tags(sidecar)
        changed = {line.split(":")[-1].split("  ")[0].strip() for line in before ^ after}
        touched_tags = {line.split()[1] for line in (before ^ after) if len(line.split()) > 1}
        assert touched_tags <= {"TransmissionReference", "XMPToolkit"}, changed

    def test_raw_without_sidecar_needs_sidecar(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-05"
        month.mkdir(parents=True)
        scratch = make_master(month, "2026:05:01 09:00:00")
        raw = month / (scratch.name.rsplit("_", 1)[0] + "_0ddc0ffe.nef")
        scratch.rename(raw)

        code, payload = run_inject(archive, "--apply")
        assert code == 1
        assert findings_of(payload)[0]["bucket"] == "needs-sidecar"
        assert read_token(raw) is None

    def test_no_dam_config_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(
            f'root = {str(tmp_path)!r}\n[[trees]]\npath = "Photos"\nmedia = "photo"\n'
        )
        (tmp_path / "Photos").mkdir()
        assert main(["inject", "--config", str(tmp_path / "config.toml")]) == 2

    def test_embedded_token_flow_converges(self, archive: Path) -> None:
        # Writing the token into an embedded-format master changes its
        # content, so its name can never match its current hash. The flow
        # must still converge: after the DAM rename, inject goes quiet.
        month = archive / "Photos" / "2026" / "2026-07"
        stale = make_drifted_master(month, "2026:07:01 10:00:00")

        code, payload = run_inject(archive, "--apply")
        assert code == 0
        token = read_token(stale)
        assert token is not None

        # simulate the DAM renaming the master to the injected token
        renamed = stale.with_name(f"{token}.jpg")
        stale.rename(renamed)

        # inject must now consider this master done — not re-inject,
        # which would loop forever
        code, payload = run_inject(archive, "--apply")
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0
        assert findings_of(payload) == []
        assert summary["ok"] == 1
        assert read_token(renamed) == token  # unchanged

    def test_embedded_convergence_still_catches_date_changes(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-08"
        stale = make_drifted_master(month, "2026:08:01 10:00:00")
        assert run_inject(archive, "--apply")[0] == 0
        token = read_token(stale)
        assert token is not None
        renamed = stale.with_name(f"{token}.jpg")
        stale.rename(renamed)

        # a capture-time correction changes the date part: convergence
        # must not mask it
        subprocess.run(
            [
                "exiftool",
                "-q",
                "-overwrite_original",
                "-EXIF:DateTimeOriginal=2026:08:02 11:00:00",
                str(renamed),
            ],
            check=True,
        )
        code, payload = run_inject(archive)
        assert code == 0
        findings = findings_of(payload)
        assert findings[0]["bucket"] == "token-pending"
        assert "20260802_110000_" in str(findings[0]["detail"])

    def test_undated_stale_master_is_reported(self, archive: Path) -> None:
        month = archive / "Photos" / "2026" / "2026-06"
        month.mkdir(parents=True)
        undated = month / "20260601_080000_cafecafe.jpg"
        undated.write_bytes(TINY_JPEG)
        code, payload = run_inject(archive)
        assert code == 1
        assert findings_of(payload)[0]["bucket"] == "unresolved-date"
