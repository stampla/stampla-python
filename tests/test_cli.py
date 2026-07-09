"""Tests for the command-line interface."""

from __future__ import annotations

from pathlib import Path

import pytest

import stampla.cli as cli_module
from stampla import __version__
from stampla.cli import main


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "stampla" in capsys.readouterr().out


def test_interrupt_exits_130_and_points_at_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "config.toml").write_text(f"root = {str(tmp_path)!r}\n")

    def interrupted(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "run_verify", interrupted)
    assert main(["verify", "--config", str(tmp_path / "config.toml")]) == 130
    assert "resume" in capsys.readouterr().err
