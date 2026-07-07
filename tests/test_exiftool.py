"""Tests for the ExifTool driver."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from stampla.exiftool import ExifTool, ExifToolError, find_exiftool


class KeepOpenBytesIO(io.BytesIO):
    """A BytesIO whose buffer stays readable after close()."""

    def close(self) -> None:
        pass


class FakeProcess:
    """Stands in for the exiftool subprocess in protocol tests."""

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdin = KeepOpenBytesIO()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        pass


def fake_tool(monkeypatch: pytest.MonkeyPatch, stdout: bytes, stderr: bytes = b"") -> ExifTool:
    process = FakeProcess(stdout, stderr)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    tool = ExifTool(executable="exiftool-under-test")
    tool.start()
    return tool


class TestProtocol:
    def test_execute_sends_args_and_reads_until_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = fake_tool(monkeypatch, b"13.55\n{ready1}\n")
        assert tool.execute("-ver") == "13.55\n"
        sent = tool._process.stdin.getvalue()  # type: ignore[union-attr]
        assert sent == b"-ver\n-execute1\n"

    def test_sequential_commands_use_distinct_markers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = fake_tool(monkeypatch, b"one\n{ready1}\ntwo\n{ready2}\n")
        assert tool.execute("a") == "one\n"
        assert tool.execute("b") == "two\n"

    def test_execute_json_parses_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b'[{"SourceFile":"a.nef","Nikon:CreateDate":"2026:01:01 00:00:00"}]\n{ready1}\n'
        tool = fake_tool(monkeypatch, payload)
        assert tool.execute_json("a.nef") == [
            {"SourceFile": "a.nef", "Nikon:CreateDate": "2026:01:01 00:00:00"}
        ]

    def test_execute_json_empty_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tool = fake_tool(monkeypatch, b"{ready1}\n")
        assert tool.execute_json("missing.nef") == []

    def test_execute_json_rejects_garbage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tool = fake_tool(monkeypatch, b"not json\n{ready1}\n")
        with pytest.raises(ExifToolError, match="unparsable"):
            tool.execute_json("a.nef")

    def test_process_death_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tool = fake_tool(monkeypatch, b"")
        with pytest.raises(ExifToolError, match="exited unexpectedly"):
            tool.execute("-ver")

    def test_execute_before_start_fails(self) -> None:
        with pytest.raises(ExifToolError, match="not running"):
            ExifTool().execute("-ver")

    def test_read_metadata_keys_by_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = (
            b'[{"SourceFile":"d/a.nef","Nikon:CreateDate":"x"},'
            b'{"SourceFile":"d/b.nef","Nikon:CreateDate":"y"}]\n{ready1}\n'
        )
        tool = fake_tool(monkeypatch, payload)
        result = tool.read_metadata([Path("d/a.nef"), Path("d/b.nef")], ["CreateDate"])
        assert result == {
            Path("d/a.nef"): {"Nikon:CreateDate": "x"},
            Path("d/b.nef"): {"Nikon:CreateDate": "y"},
        }


class TestFindExiftool:
    def test_missing_binary_gives_install_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExifToolError, match="install"):
            find_exiftool()


requires_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


@pytest.fixture(scope="module")
def tool() -> Iterator[ExifTool]:
    with ExifTool() as running:
        yield running


@requires_exiftool
class TestIntegration:
    def test_version(self, tool: ExifTool) -> None:
        assert re.fullmatch(r"\d+\.\d+", tool.version())

    def test_write_then_read_roundtrip(self, tool: ExifTool, tmp_path: Path) -> None:
        target = tmp_path / "made.xmp"
        tool.execute("-o", str(target), "-XMP-dc:Title=hello")
        assert target.exists()
        result = tool.read_metadata([target], ["XMP-dc:Title"])
        assert result[target]["XMP-dc:Title"] == "hello"

    def test_non_ascii_filename(self, tool: ExifTool, tmp_path: Path) -> None:
        target = tmp_path / "zdjęcia ąśź.xmp"
        tool.execute("-o", str(target), "-XMP-dc:Title=łąka")
        result = tool.read_metadata([target], ["XMP-dc:Title"])
        assert result[target]["XMP-dc:Title"] == "łąka"

    def test_unreadable_file_is_absent_from_results(self, tool: ExifTool, tmp_path: Path) -> None:
        missing = tmp_path / "not-there.nef"
        assert tool.read_metadata([missing], ["CreateDate"]) == {}

    def test_many_files_in_one_call(self, tool: ExifTool, tmp_path: Path) -> None:
        paths = []
        for index in range(3):
            target = tmp_path / f"file{index}.xmp"
            tool.execute("-o", str(target), f"-XMP-dc:Title=t{index}")
            paths.append(target)
        result = tool.read_metadata(paths, ["XMP-dc:Title"])
        assert [result[p]["XMP-dc:Title"] for p in paths] == ["t0", "t1", "t2"]

    def test_group_qualified_keys(self, tool: ExifTool, tmp_path: Path) -> None:
        target = tmp_path / "grouped.xmp"
        tool.execute("-o", str(target), "-XMP-photoshop:TransmissionReference=abc")
        result = tool.read_metadata([target], ["TransmissionReference"])
        assert result[target] == {"XMP-photoshop:TransmissionReference": "abc"}
