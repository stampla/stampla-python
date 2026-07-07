"""Tests for the rename command."""

from __future__ import annotations

import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from stampla.cli import main
from tests.test_verify import make_master

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
mutable = ["jpg"]

[[sidecar_dirs]]
subdir = "NKSC_PARAM"
strip = ".nksc"
{extra}
"""


def write_config(tmp_path: Path, extra: str = "") -> None:
    (tmp_path / "config.toml").write_text(CONFIG_TEMPLATE.format(root=str(tmp_path), extra=extra))


def run_rename(archive: Path, *extra: str) -> tuple[int, dict[str, object]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["rename", "--config", str(archive / "config.toml"), "--json", *extra])
    return code, json.loads(buffer.getvalue())


def findings_of(payload: dict[str, object]) -> list[dict[str, object]]:
    findings = payload["findings"]
    assert isinstance(findings, list)
    return findings


def make_drifted_family(month: Path, capture: str) -> tuple[Path, list[Path]]:
    """A stale-named master with DAM-unaware members and a plain sidecar."""
    master = make_master(month, capture)
    stale_prefix = master.name.rsplit("_", 1)[0] + "_0ddc0ffe"
    stale = month / f"{stale_prefix}.jpg"
    master.rename(stale)
    members = []
    for name in (f"{stale_prefix}.xmp", f"{stale_prefix}.jpg.xmp", f"{stale_prefix}-Edit.tif"):
        member = month / name
        member.write_text("member")
        members.append(member)
    nksc = month / "NKSC_PARAM"
    nksc.mkdir(exist_ok=True)
    cross = nksc / f"{stale_prefix}.jpg.nksc"
    cross.write_text("nx")
    members.append(cross)
    return stale, members


@requires_exiftool
class TestRenameEndToEnd:
    def test_whole_family_renamed_outside_dam(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        month = tmp_path / "Photos" / "2026" / "2026-01"
        stale, _ = make_drifted_family(month, "2026:01:05 12:30:00")

        code, payload = run_rename(tmp_path, "--apply")
        assert code == 0, payload
        renamed = [f for f in findings_of(payload) if f["bucket"] == "renamed"]
        assert len(renamed) == 5  # master + 4 members

        assert not stale.exists()
        survivors = sorted(p.name for p in month.rglob("*") if p.is_file())
        prefixes = {name.split(".")[0].split("-")[0] for name in survivors}
        assert len(prefixes) == 1  # everyone moved to the same new prefix
        assert not prefixes.pop().endswith("0ddc0ffe")

    def test_dam_tree_keeps_master_and_plain_sidecar(self, tmp_path: Path) -> None:
        write_config(tmp_path, '\n[dam]\ntrees = ["Photos"]\n')
        month = tmp_path / "Photos" / "2026" / "2026-02"
        stale, _ = make_drifted_family(month, "2026:02:05 12:30:00")

        code, payload = run_rename(tmp_path, "--apply")
        assert code == 0, payload
        assert stale.exists()  # master is the DAM's to rename
        assert (month / f"{stale.stem}.xmp").exists()  # so is its plain sidecar
        # DAM-unaware members moved to the derived prefix
        renamed = [f for f in findings_of(payload) if f["bucket"] == "renamed"]
        assert len(renamed) == 3  # .jpg.xmp, -Edit.tif, NKSC
        assert not (month / f"{stale.stem}.jpg.xmp").exists()
        assert not (month / "NKSC_PARAM" / f"{stale.stem}.jpg.nksc").exists()

    def test_case_fix_for_uppercase_extension(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        month = tmp_path / "Photos" / "2026" / "2026-03"
        master = make_master(month, "2026:03:05 12:30:00")
        weird = month / f"{master.stem}.FP2"
        weird.write_text("profile")

        code, payload = run_rename(tmp_path, "--apply")
        assert code == 0, payload
        # check the directory listing: on case-insensitive filesystems
        # Path(".FP2").exists() would also match the renamed ".fp2"
        names = {p.name for p in month.iterdir()}
        assert f"{master.stem}.fp2" in names
        assert f"{master.stem}.FP2" not in names

    def test_dry_run_is_default_and_touches_nothing(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        month = tmp_path / "Photos" / "2026" / "2026-04"
        stale, _ = make_drifted_family(month, "2026:04:05 12:30:00")

        code, payload = run_rename(tmp_path)
        assert code == 0
        pending = [f for f in findings_of(payload) if f["bucket"] == "rename-pending"]
        assert len(pending) == 5
        assert stale.exists()

    def test_clean_archive_plans_nothing(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        make_master(tmp_path / "Photos" / "2026" / "2026-05", "2026:05:05 12:30:00")
        code, payload = run_rename(tmp_path, "--apply")
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0
        assert summary["ok"] == 1
        assert findings_of(payload) == []

    def test_rename_is_undoable(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journals"
        write_config(tmp_path)
        month = tmp_path / "Photos" / "2026" / "2026-06"
        stale, _ = make_drifted_family(month, "2026:06:05 12:30:00")

        import stampla.journal as journal_module

        original = journal_module.default_journal_dir
        journal_module.default_journal_dir = lambda: journal_dir
        try:
            assert run_rename(tmp_path, "--apply")[0] == 0
            assert not stale.exists()
            journals = sorted(journal_dir.glob("journal-*.json"))
            assert main(["undo", str(journals[-1])]) == 0
            assert stale.exists()
        finally:
            journal_module.default_journal_dir = original

    def test_renamed_archive_verifies_clean(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        month = tmp_path / "Photos" / "2026" / "2026-07"
        make_drifted_family(month, "2026:07:05 12:30:00")
        assert run_rename(tmp_path, "--apply")[0] == 0

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["verify", "--config", str(tmp_path / "config.toml"), "--json"])
        payload = json.loads(buffer.getvalue())
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0, payload
        assert summary["ok"] == 1
