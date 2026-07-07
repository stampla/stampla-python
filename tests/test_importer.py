"""Tests for the import command."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from stampla.cli import main
from tests.test_verify import TINY_JPEG

requires_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

CONFIG_TEMPLATE = """
root = {root!r}

[[trees]]
path = "Photos"
media = "photo"

[extensions]
raw = ["jpg"]

[[sidecar_dirs]]
subdir = "NKSC_PARAM"
strip = ".nksc"
"""


def make_card_photo(card: Path, base: str, capture: str, seasoning: bytes = b"") -> Path:
    card.mkdir(parents=True, exist_ok=True)
    photo = card / f"{base}.JPG"
    photo.write_bytes(TINY_JPEG + seasoning)
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", f"-EXIF:DateTimeOriginal={capture}", str(photo)],
        check=True,
    )
    return photo


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text(CONFIG_TEMPLATE.format(root=str(tmp_path)))
    return tmp_path


def run_import(archive: Path, card: Path, *extra: str) -> tuple[int, dict[str, object]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(
            ["import", str(card), "--config", str(archive / "config.toml"), "--json", *extra]
        )
    return code, json.loads(buffer.getvalue())


@requires_exiftool
class TestImportEndToEnd:
    def test_dry_run_touches_nothing(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_1234", "2026:07:01 10:00:00")
        code, payload = run_import(archive, card)
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0
        assert summary["ok"] == 1
        assert not (archive / "Photos").exists()

    def test_apply_copies_family_under_canonical_names(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        photo = make_card_photo(card, "DSC_1234", "2026:07:01 10:00:00")
        (card / "DSC_1234.xmp").write_text("<x:xmpmeta/>")
        (card / "DSC_1234.JPG.xmp").write_text("<x:xmpmeta/>")
        nksc = card / "NKSC_PARAM"
        nksc.mkdir()
        (nksc / "DSC_1234.JPG.nksc").write_text("nx")
        (card / "DSC_1234-Edit.TIF").write_bytes(b"edited")

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload

        digest = hashlib.md5(photo.read_bytes()).hexdigest()[:8]
        month = archive / "Photos" / "2026" / "2026-07"
        prefix = f"20260701_100000_{digest}"
        assert (month / f"{prefix}.jpg").exists()
        assert (month / f"{prefix}.xmp").exists()
        assert (month / f"{prefix}.jpg.xmp").exists()
        assert (month / "NKSC_PARAM" / f"{prefix}.jpg.nksc").exists()
        assert (month / f"{prefix}-Edit.tif").exists()
        # sources untouched
        assert photo.exists()
        assert (card / "DSC_1234.xmp").exists()

    def test_reimport_is_the_clearance_check(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_1234", "2026:07:01 10:00:00")
        (card / "DSC_1234.xmp").write_text("<x:xmpmeta/>")
        assert run_import(archive, card, "--apply")[0] == 0

        # everything already in the archive with identical content: exit 0
        code, payload = run_import(archive, card, "--apply")
        assert code == 0
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "already-imported"

    def test_differing_archive_copy_is_a_collision(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_1234", "2026:07:01 10:00:00")
        sidecar = card / "DSC_1234.xmp"
        sidecar.write_text("<x:xmpmeta/>")
        assert run_import(archive, card, "--apply")[0] == 0

        # the archive sidecar evolves (edits) — the card version now differs
        month = archive / "Photos" / "2026" / "2026-07"
        archived_sidecar = next(month.glob("*.xmp"))
        archived_sidecar.write_text("<x:xmpmeta edited/>")

        code, payload = run_import(archive, card)
        assert code == 1
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "collision"
        assert "differs" in str(findings[0]["detail"])

    def test_partially_imported_family_is_a_collision(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_1234", "2026:07:01 10:00:00")
        (card / "DSC_1234.xmp").write_text("<x:xmpmeta/>")
        assert run_import(archive, card, "--apply")[0] == 0

        month = archive / "Photos" / "2026" / "2026-07"
        next(month.glob("*.xmp")).unlink()

        code, payload = run_import(archive, card)
        assert code == 1
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "collision"
        assert "missing" in str(findings[0]["detail"])

    def test_hidden_paths_are_reported_not_silently_skipped(
        self, archive: Path, tmp_path: Path
    ) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_1234", "2026:07:01 10:00:00")
        trash = card / ".Trashes" / "501"
        trash.mkdir(parents=True)
        (trash / "leftover.jpg").write_bytes(TINY_JPEG)

        code, payload = run_import(archive, card, "--apply")
        assert code == 0  # hidden junk is visible but not blocking
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "ignored"
        assert "leftover.jpg" in str(findings[0]["path"])

    def test_undated_file_is_skipped_not_imported(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        card.mkdir()
        (card / "DSC_0001.JPG").write_bytes(TINY_JPEG)
        make_card_photo(card, "DSC_0002", "2026:07:02 11:00:00")

        code, payload = run_import(archive, card, "--apply")
        assert code == 1
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "unresolved-date"
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("*.jpg"))) == 1

    def test_orphan_sidecar_group_is_reported(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        card.mkdir()
        (card / "DSC_0003.xmp").write_text("<x:xmpmeta/>")
        code, payload = run_import(archive, card)
        assert code == 1
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert findings[0]["bucket"] == "orphan-family"

    def test_imported_archive_verifies_clean(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_1234", "2026:07:01 10:00:00")
        make_card_photo(card, "DSC_1235", "2026:07:03 12:00:00", b"x")
        assert run_import(archive, card, "--apply")[0] == 0

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["verify", "--config", str(archive / "config.toml"), "--json"])
        payload = json.loads(buffer.getvalue())
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0, payload
        assert summary["ok"] == 2

    def test_missing_card_is_an_error(self, archive: Path, tmp_path: Path) -> None:
        assert (
            main(["import", str(tmp_path / "no-card"), "--config", str(archive / "config.toml")])
            == 2
        )
