"""Tests for the organize command."""

from __future__ import annotations

import io
import json
import os
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from stampla.cli import main
from tests.test_importer import make_card_photo
from tests.test_verify import TINY_JPEG

requires_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

CONFIG = """
root = {root!r}

[[trees]]
path = "Photos"
media = "photo"

[extensions]
raw = ["jpg"]
"""


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text(CONFIG.format(root=str(tmp_path)))
    return tmp_path


def run_organize(archive: Path, messy: Path) -> tuple[int, dict[str, object]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["organize", str(messy), "--config", str(archive / "config.toml"), "--json"])
    return code, json.loads(buffer.getvalue())


def findings_of(payload: dict[str, object]) -> list[dict[str, object]]:
    findings = payload["findings"]
    assert isinstance(findings, list)
    return findings


@requires_exiftool
class TestOrganize:
    def test_proposals_without_touching_anything(self, archive: Path, tmp_path: Path) -> None:
        messy = tmp_path / "messy"
        make_card_photo(messy, "IMG_0001", "2019:05:04 10:00:00")
        code, payload = run_organize(archive, messy)
        assert code == 0
        proposals = payload["plan"]
        assert isinstance(proposals, list)
        assert len(proposals) == 1
        target = Path(str(proposals[0]["changes"][0][1]))
        assert target.parts[-4:-1] == ("Photos", "2019", "2019-05")
        assert not (archive / "Photos").exists()  # nothing moved

    def test_mtime_fallback_is_proposed_but_flagged(self, archive: Path, tmp_path: Path) -> None:
        messy = tmp_path / "messy"
        messy.mkdir()
        undated = messy / "IMG_0002.JPG"
        undated.write_bytes(TINY_JPEG)
        stamp = 1_557_000_000  # 2019-05-04 UTC-ish
        os.utime(undated, (stamp, stamp))

        code, payload = run_organize(archive, messy)
        assert code == 1  # mtime dating needs human confirmation
        findings = findings_of(payload)
        assert findings[0]["bucket"] == "mtime-dated"
        proposals = payload["plan"]
        assert isinstance(proposals, list)
        assert len(proposals) == 1  # still proposed, not dropped

    def test_duplicate_content_is_clustered(self, archive: Path, tmp_path: Path) -> None:
        messy = tmp_path / "messy"
        first = make_card_photo(messy, "IMG_0003", "2019:06:01 09:00:00")
        copy = messy / "copy of IMG_0003.JPG"
        copy.write_bytes(first.read_bytes())

        code, payload = run_organize(archive, messy)
        assert code == 1
        buckets = [f["bucket"] for f in findings_of(payload)]
        assert buckets.count("collision") == 2

    def test_already_archived_content_is_reported(self, archive: Path, tmp_path: Path) -> None:
        messy = tmp_path / "messy"
        make_card_photo(messy, "IMG_0004", "2019:07:01 09:00:00")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            assert (
                main(
                    [
                        "import",
                        str(messy),
                        "--config",
                        str(archive / "config.toml"),
                        "--apply",
                    ]
                )
                == 0
            )

        code, payload = run_organize(archive, messy)
        assert code == 0
        findings = findings_of(payload)
        assert findings[0]["bucket"] == "already-imported"

    def test_organize_has_no_apply_flag(self, archive: Path, tmp_path: Path) -> None:
        messy = tmp_path / "messy"
        messy.mkdir()
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "organize",
                    str(messy),
                    "--config",
                    str(archive / "config.toml"),
                    "--apply",
                ]
            )
        assert excinfo.value.code == 2  # argparse rejects the flag
