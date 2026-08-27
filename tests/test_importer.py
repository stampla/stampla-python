"""Tests for the import command."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from stampla.cli import main
from stampla.journal import Journal
from stampla.progress import Monitor
from tests.test_verify import TINY_JPEG


def findings_of(payload: dict[str, object]) -> list[dict[str, object]]:
    findings = payload["findings"]
    assert isinstance(findings, list)
    return findings


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


def run_import(
    archive: Path, card: Path, *extra: str, paths: tuple[Path, ...] = ()
) -> tuple[int, dict[str, object]]:
    # selection paths go right after the card: argparse on Python 3.11
    # cannot match a positional block that follows the options
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(
            [
                "import",
                str(card),
                *[str(path) for path in paths],
                "--config",
                str(archive / "config.toml"),
                "--json",
                *extra,
            ]
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
        assert payload["verdict"] is None  # a dry run never judges the card
        assert not (archive / "Photos").exists()

    def test_apply_copies_group_under_canonical_names(self, archive: Path, tmp_path: Path) -> None:
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
        assert payload["verdict"] == {
            "safe_to_format": True,
            "imported": 0,
            "already_imported": 1,
            "ignored": 0,
        }

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

    def test_selection_imports_only_chosen_directories(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card / "batch-a", "DSC_0001", "2026:07:01 10:00:00")
        make_card_photo(card / "batch-b", "DSC_0002", "2026:07:02 10:00:00", b"b")

        code, payload = run_import(archive, card, "--apply", paths=(card / "batch-a",))
        assert code == 0, payload
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("20260701_*.jpg"))) == 1
        assert list(month.glob("20260702_*.jpg")) == []
        # the unselected batch is out of scope entirely: not copied, not reported
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert summary["scanned"] == 1
        # and a selective run never clears the card for formatting
        assert payload["verdict"] is None

    def test_selection_outside_the_card_is_an_error(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_0001", "2026:07:01 10:00:00")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                ["import", str(card), str(elsewhere), "--config", str(archive / "config.toml")]
            )
        assert code == 2

    def test_selection_must_be_a_directory(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        photo = make_card_photo(card, "DSC_0001", "2026:07:01 10:00:00")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["import", str(card), str(photo), "--config", str(archive / "config.toml")])
        assert code == 2

    def test_applied_import_with_problems_withholds_the_verdict(
        self, archive: Path, tmp_path: Path
    ) -> None:
        card = tmp_path / "card"
        card.mkdir()
        (card / "NODATE.JPG").write_bytes(TINY_JPEG)  # no capture time anywhere

        code, payload = run_import(archive, card, "--apply")
        assert code == 1
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["safe_to_format"] is False
        assert verdict["imported"] == 0

    def test_partially_imported_group_is_a_collision(self, archive: Path, tmp_path: Path) -> None:
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
        assert findings[0]["bucket"] == "orphan-group"

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


@requires_exiftool
class TestImportPolicies:
    def configure(self, tmp_path: Path, extra: str) -> Path:
        (tmp_path / "config.toml").write_text(
            CONFIG_TEMPLATE.format(root=str(tmp_path)).replace('raw = ["jpg"]', 'raw = ["nef"]')
            + extra
        )
        return tmp_path

    def make_raw_jpeg_pair(self, card: Path, base: str, capture: str) -> tuple[Path, Path]:
        # the "NEF" is a JPEG that gets its EXIF written while still a
        # .jpg and its extension changed after — reads are content-sniffed,
        # so dates still resolve; only grouping and policy are under test
        jpg = make_card_photo(card, base, capture)
        scratch = make_card_photo(card, f"XRAW{base}", capture, b"raw-payload")
        raw = card / f"{base}.NEF"
        scratch.rename(raw)
        return raw, jpg

    def test_jpeg_twin_skipped_when_policy_enabled(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path, "[import]\nskip_jpeg_twins = true\n")
        card = tmp_path / "card"
        self.make_raw_jpeg_pair(card, "DSC_0001", "2026:07:05 10:00:00")

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert [f["bucket"] for f in findings] == ["ignored"]
        assert findings[0]["data"] == {"reason": "jpeg-twin"}
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("*.nef"))) == 1
        assert list(month.glob("*.jpg")) == []

    def test_jpeg_twin_imported_by_default(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path, "")
        card = tmp_path / "card"
        self.make_raw_jpeg_pair(card, "DSC_0001", "2026:07:05 10:00:00")

        code, _ = run_import(archive, card, "--apply")
        assert code == 0
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("*.nef"))) == 1
        assert len(list(month.glob("*.jpg"))) == 1

    def test_standalone_jpeg_still_imports_under_twin_policy(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path, "[import]\nskip_jpeg_twins = true\n")
        card = tmp_path / "card"
        make_card_photo(card, "DSC_0002", "2026:07:05 11:00:00")  # jpg only, no raw

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("*.jpg"))) == 1

    def test_ignore_globs_are_reported_not_imported(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path, '[import]\nignore = ["NIKON001.DSC", "NC_FLLST.DAT"]\n')
        card = tmp_path / "card"
        self.make_raw_jpeg_pair(card, "DSC_0001", "2026:07:05 10:00:00")
        (card / "NIKON001.DSC").write_bytes(b"\0" * 16)
        dcim = card / "DCIM" / "100NZ502"
        dcim.mkdir(parents=True)
        (dcim / "NC_FLLST.DAT").write_bytes(b"\0" * 16)

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        findings = payload["findings"]
        assert isinstance(findings, list)
        ignored = [Path(str(f["path"])).name for f in findings if f["bucket"] == "ignored"]
        assert "NIKON001.DSC" in ignored
        assert "NC_FLLST.DAT" in ignored

    def test_enhanced_nr_dng_travels_with_its_raw(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path, "")
        card = tmp_path / "card"
        self.make_raw_jpeg_pair(card, "DSC_0009", "2026:07:06 10:00:00")
        (card / "DSC_0009-Enhanced-NR.dng").write_bytes(b"denoised")

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        month = archive / "Photos" / "2026" / "2026-07"
        nef = next(month.glob("*.nef"))
        prefix = nef.name.rsplit(".", 1)[0]
        assert (month / f"{prefix}-Enhanced-NR.dng").exists()

    def test_standalone_heic_is_its_own_master(self, tmp_path: Path) -> None:
        # phone shot: JPEG payload named .heic — master selection and
        # grouping are name-based, content codec is irrelevant here
        archive = self.configure(tmp_path, "")
        card = tmp_path / "card"
        scratch = make_card_photo(card, "IMG_5001", "2026:07:08 09:00:00")
        heic = card / "IMG_5001.HEIC"
        scratch.rename(heic)

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("*.heic"))) == 1

    def test_standalone_dng_is_its_own_master(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path, "")
        card = tmp_path / "card"
        scratch = make_card_photo(card, "SCAN0001", "2026:07:07 09:00:00")
        dng = card / "SCAN0001.dng"
        scratch.rename(dng)

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("*.dng"))) == 1

    def test_ignore_all_jpegs_case_insensitively(self, tmp_path: Path) -> None:
        # in-camera RAW processing produces new-numbered JPEGs that are
        # not twins; a "*.jpg" ignore skips every JPEG regardless of case
        archive = self.configure(tmp_path, '[import]\nignore = ["*.jpg"]\n')
        card = tmp_path / "card"
        self.make_raw_jpeg_pair(card, "DSC_0001", "2026:07:05 10:00:00")

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        findings = payload["findings"]
        assert isinstance(findings, list)
        assert [f["bucket"] for f in findings] == ["ignored"]
        assert str(findings[0]["path"]).endswith("DSC_0001.JPG")
        month = archive / "Photos" / "2026" / "2026-07"
        assert len(list(month.glob("*.nef"))) == 1
        assert list(month.glob("*.jpg")) == []


SHOOT_CONFIG = """
root = {root!r}

[[trees]]
path = "Events"
media = "photo"
layout = "{{yyyy}}/{{shoot}}"

[extensions]
raw = ["jpg"]
"""


@requires_exiftool
class TestShootToken:
    def configure(self, tmp_path: Path, extra: str = "") -> Path:
        (tmp_path / "config.toml").write_text(SHOOT_CONFIG.format(root=str(tmp_path)) + extra)
        return tmp_path

    def run_shoot(self, archive: Path, card: Path, *extra: str) -> tuple[int, dict[str, object]]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                ["import", str(card), "--config", str(archive / "config.toml"), "--json", *extra]
            )
        return code, json.loads(buffer.getvalue())

    def test_shoot_fills_the_layout_token(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path)
        card = tmp_path / "card"
        make_card_photo(card, "IMG_5001", "2026:05:16 15:20:41")

        code, payload = self.run_shoot(archive, card, "--shoot", "Anna Peter Wedding", "--apply")
        assert code == 0, payload
        shoot_dir = archive / "Events" / "2026" / "Anna_Peter_Wedding"
        assert list(shoot_dir.glob("20260516_152041_*.jpg"))

    def test_shoot_layout_refuses_to_import_without_a_shoot(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path)
        card = tmp_path / "card"
        make_card_photo(card, "IMG_5001", "2026:05:16 15:20:41")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["import", str(card), "--config", str(archive / "config.toml"), "--apply"])
        assert code == 2  # never guesses a shoot

    def test_hostile_shoot_names_are_rejected(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path)
        card = tmp_path / "card"
        make_card_photo(card, "IMG_5001", "2026:05:16 15:20:41")
        for hostile in ("../escape", "a/b", "", "x" * 65):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "import",
                        str(card),
                        "--config",
                        str(archive / "config.toml"),
                        "--shoot",
                        hostile,
                    ]
                )
            assert code == 2, hostile

    def test_shoot_dir_archive_verifies_clean(self, tmp_path: Path) -> None:
        archive = self.configure(tmp_path)
        card = tmp_path / "card"
        make_card_photo(card, "IMG_5001", "2026:05:16 15:20:41")
        (card / "IMG_5001.xmp").write_text("<x:xmpmeta/>")
        code, payload = self.run_shoot(archive, card, "--shoot", "Anna Peter Wedding", "--apply")
        assert code == 0, payload

        # the shoot lives in the directory: names and verification untouched
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            verify_code = main(["verify", "--config", str(tmp_path / "config.toml"), "--json"])
        verify_payload = json.loads(buffer.getvalue())
        assert verify_code == 0, verify_payload
        assert verify_payload["summary"]["ok"] == 1


@requires_exiftool
class TestScanSafety:
    def test_unreadable_directory_withholds_formatting(self, archive: Path, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("permission bits do not translate")
        card = tmp_path / "card"
        make_card_photo(card, "DSC_0001", "2019:06:01 09:00:00")
        hidden = card / "DCIM_B"
        hidden.mkdir()
        (hidden / "DSC_0002.JPG").write_bytes(TINY_JPEG)
        hidden.chmod(0)
        try:
            code, payload = run_import(archive, card, "--apply")
        finally:
            hidden.chmod(0o755)

        assert code == 1
        buckets = {f["bucket"] for f in findings_of(payload)}
        assert "scan-error" in buckets
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["safe_to_format"] is False
        [finding] = [f for f in findings_of(payload) if f["bucket"] == "scan-error"]
        assert "do not format" in str(finding["detail"])

    def test_newline_filename_is_an_alarm_not_a_guess(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_0001", "2019:06:01 09:00:00")
        weird = card / "DSC\n-AllDates=2000:01:01.JPG"
        weird.write_bytes(TINY_JPEG)

        code, payload = run_import(archive, card, "--apply")
        assert code == 1
        [finding] = [f for f in findings_of(payload) if f["bucket"] == "scan-error"]
        assert "newline" in str(finding["detail"])
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["safe_to_format"] is False
        # the clean photo still imported; only the hostile name was refused
        assert verdict["imported"] == 1

    def test_duplicate_content_on_card_imports_once(self, archive: Path, tmp_path: Path) -> None:
        card = tmp_path / "card"
        first = make_card_photo(card / "DCIM_A", "DSC_0001", "2019:06:01 09:00:00")
        second = card / "DCIM_B" / "DSC_0400.JPG"
        second.parent.mkdir(parents=True)
        second.write_bytes(first.read_bytes())

        code, payload = run_import(archive, card, "--apply")
        assert code == 0, payload
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["safe_to_format"] is True
        assert verdict["imported"] == 1
        [dup] = [f for f in findings_of(payload) if f["bucket"] == "ignored"]
        dup_data = dup["data"]
        assert isinstance(dup_data, dict)
        assert dup_data["reason"] == "card-duplicate"
        month = archive / "Photos" / "2019" / "2019-06"
        assert len(list(month.glob("*.jpg"))) == 1

    def test_post_apply_verification_failure_withholds_formatting(
        self, archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        card = tmp_path / "card"
        make_card_photo(card, "DSC_0001", "2019:06:01 09:00:00")

        import stampla.importer as importer_module

        real_apply = importer_module.apply_plan  # type: ignore[attr-defined]

        def truncating_apply(journal: Journal, **kwargs: Monitor | None) -> object:
            result = real_apply(journal, **kwargs)
            month = archive / "Photos" / "2019" / "2019-06"
            for copied in month.glob("*.jpg"):
                copied.write_bytes(b"rotted after the copy landed")
            return result

        monkeypatch.setattr(importer_module, "apply_plan", truncating_apply)
        code, payload = run_import(archive, card, "--apply")
        assert code == 1
        [finding] = [f for f in findings_of(payload) if f["bucket"] == "corruption"]
        assert "do not format the card" in str(finding["detail"])
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["safe_to_format"] is False
