"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

import stampla.journal as journal_module


@pytest.fixture(autouse=True)
def isolated_journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's journals out of the user's real journal directory."""
    monkeypatch.setattr(journal_module, "default_journal_dir", lambda: tmp_path / "journals")
