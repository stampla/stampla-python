"""Tests for the relocate command.

Relocate never reads metadata — the correct shelf for a group is derived
from its name — so these build canonically named files directly on
``tmp_path`` and drive the command through the public CLI (and, where the
error and undo paths need it, through ``run_relocate`` directly).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from stampla.cli import main
from stampla.config import Config, DamConfig, Tree
from stampla.journal import Journal
from stampla.relocate import RelocateOptions, run_relocate

CONFIG_TEMPLATE = """
root = {root!r}

[[trees]]
path = "Photos"
media = "photo"
{extra}
"""


def write_config(tmp_path: Path, extra: str = "") -> None:
    (tmp_path / "config.toml").write_text(CONFIG_TEMPLATE.format(root=str(tmp_path), extra=extra))


def make_group(directory: Path, prefix: str) -> tuple[Path, list[Path]]:
    """A canonically named master with a plain sidecar, both under ``prefix``."""
    directory.mkdir(parents=True, exist_ok=True)
    master = directory / f"{prefix}.jpg"
    master.write_text("master")
    sidecar = directory / f"{prefix}.xmp"
    sidecar.write_text("<x:xmpmeta/>")
    return master, [sidecar]


def run_relocate_cli(archive: Path, *extra: str) -> tuple[int, dict[str, object]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["relocate", "--config", str(archive / "config.toml"), "--json", *extra])
    return code, json.loads(buffer.getvalue())


def buckets_of(payload: dict[str, object]) -> list[str]:
    findings = payload["findings"]
    assert isinstance(findings, list)
    return [str(f["bucket"]) for f in findings]


def config_of(tmp_path: Path, dam: DamConfig | None = None, layout: str | None = None) -> Config:
    tree = Tree(path="Photos", media="photo", **({"layout": layout} if layout else {}))
    return Config(trees=(tree,), root=str(tmp_path), dam=dam)


class TestRelocate:
    def test_misplaced_group_moves_whole_on_apply(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        # name says January, but the group sits under 2026-02
        wrong = tmp_path / "Photos" / "2026" / "2026-02"
        master, sidecars = make_group(wrong, "20260105_123000_deadbeef")

        code, payload = run_relocate_cli(tmp_path, "--apply")
        # the misplacement is still recorded (attention), plus the move
        assert code == 1, payload
        assert buckets_of(payload).count("relocated") == 1
        assert buckets_of(payload).count("misplaced") == 1
        right = tmp_path / "Photos" / "2026" / "2026-01"
        assert (right / master.name).exists()
        assert (right / sidecars[0].name).exists()
        assert not master.exists()

        # a re-run has nothing left to find
        code, payload = run_relocate_cli(tmp_path, "--apply")
        assert code == 0
        assert payload["findings"] == []

    def test_dry_run_reports_and_moves_nothing(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        wrong = tmp_path / "Photos" / "2026" / "2026-02"
        master, _ = make_group(wrong, "20260105_123000_deadbeef")

        code, payload = run_relocate_cli(tmp_path)
        assert code == 1  # misplaced is an attention finding
        buckets = buckets_of(payload)
        assert "misplaced" in buckets
        assert "relocate-pending" in buckets
        assert master.exists()  # nothing moved

    def test_correctly_placed_group_is_ok(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        right = tmp_path / "Photos" / "2026" / "2026-01"
        make_group(right, "20260105_123000_deadbeef")

        code, payload = run_relocate_cli(tmp_path, "--apply")
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0
        assert summary["ok"] == 1
        assert payload["findings"] == []

    def test_dam_tree_gets_a_checklist_never_a_move(self, tmp_path: Path) -> None:
        write_config(tmp_path, '\n[dam]\ntrees = ["Photos"]\n')
        wrong = tmp_path / "Photos" / "2026" / "2026-02"
        master, _ = make_group(wrong, "20260105_123000_deadbeef")

        # an archive-wide run: misplacement reported, nothing moved
        code, payload = run_relocate_cli(tmp_path, "--apply")
        assert code == 1  # misplaced is attention, not safe
        assert "misplaced" in buckets_of(payload)
        assert "relocated" not in buckets_of(payload)
        assert master.exists()
        hints = payload["hints"]
        assert isinstance(hints, list)
        assert any("Folders panel" in hint for hint in hints)

    def test_targeting_a_dam_tree_with_apply_is_an_error(self, tmp_path: Path) -> None:
        config = config_of(tmp_path, dam=DamConfig(trees=("Photos",)))
        wrong = tmp_path / "Photos" / "2026" / "2026-02"
        make_group(wrong, "20260105_123000_deadbeef")
        with pytest.raises(ValueError, match="DAM-managed"):
            run_relocate(
                config,
                tmp_path,
                (tmp_path / "Photos",),
                options=RelocateOptions(apply=True),
            )

    def test_shoot_layout_wrong_year_is_reported_not_moved(self, tmp_path: Path) -> None:
        write_config(tmp_path, '\nlayout = "{yyyy}/{shoot}"\n')
        # name says 2026, but the group is filed under 2025/<shoot>
        wrong = tmp_path / "Photos" / "2025" / "harvest-fair"
        master, _ = make_group(wrong, "20260105_123000_deadbeef")

        code, payload = run_relocate_cli(tmp_path, "--apply")
        assert code == 1
        assert "misplaced" in buckets_of(payload)
        assert "relocated" not in buckets_of(payload)
        assert master.exists()
        hints = payload["hints"]
        assert isinstance(hints, list)
        assert any("shoot" in hint for hint in hints)

    def test_shoot_layout_right_year_any_shoot_dir_is_ok(self, tmp_path: Path) -> None:
        write_config(tmp_path, '\nlayout = "{yyyy}/{shoot}"\n')
        # right year, any shoot directory name — the shoot segment is a wildcard
        right = tmp_path / "Photos" / "2026" / "whatever-the-shoot-was"
        make_group(right, "20260105_123000_deadbeef")

        code, payload = run_relocate_cli(tmp_path, "--apply")
        summary = payload["summary"]
        assert isinstance(summary, dict)
        assert code == 0
        assert summary["ok"] == 1
        assert payload["findings"] == []

    def test_relocate_is_undoable(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journals"
        config = config_of(tmp_path)
        wrong = tmp_path / "Photos" / "2026" / "2026-02"
        master, sidecars = make_group(wrong, "20260105_123000_deadbeef")

        run_relocate(
            config,
            tmp_path,
            options=RelocateOptions(apply=True, journal_dir=journal_dir),
        )
        right = tmp_path / "Photos" / "2026" / "2026-01"
        assert (right / master.name).exists()
        assert not master.exists()

        journals = sorted(journal_dir.glob("journal-*.json"))
        assert len(journals) == 1
        journal = Journal.load(journals[-1])
        assert journal.command == "relocate"
        from stampla.apply import undo_journal

        result = undo_journal(journal)
        assert result.ok
        assert master.exists()  # back where it started
        assert sidecars[0].exists()
        assert not (right / master.name).exists()


class TestVerifyReportsMisplaced:
    def test_verify_flags_a_wrongly_shelved_named_file(self, tmp_path: Path) -> None:
        # verify's placement check reads names only, so no exiftool is
        # needed for the misplaced finding itself; a file with unreadable
        # metadata is still placed by its name.
        write_config(tmp_path)
        wrong = tmp_path / "Photos" / "2026" / "2026-02"
        make_group(wrong, "20260105_123000_deadbeef")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                ["verify", "--config", str(tmp_path / "config.toml"), "--json", "--skip-hash"]
            )
        payload = json.loads(buffer.getvalue())
        assert code == 1
        buckets = [str(f["bucket"]) for f in payload["findings"]]
        assert "misplaced" in buckets
