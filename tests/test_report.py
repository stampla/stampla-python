"""Tests for report rendering."""

from __future__ import annotations

import json
from pathlib import Path

from stampla.report import Bucket, Finding, Report


def sample_report() -> Report:
    report = Report(ok=2, scanned=5, families=3)
    report.add(Finding(Bucket.CORRUPTION, Path("a/x.nef"), "name says aaaa, content is bbbb"))
    report.add(Finding(Bucket.UNNAMED, Path("a/DSC_1.NEF")))
    report.add(
        Finding(
            Bucket.COLLISION,
            Path("a/y.nef"),
            "derives p",
            related=(Path("a/z.nef"),),
        )
    )
    return report


class TestRenderText:
    def test_summary_line(self) -> None:
        text = sample_report().render_text()
        assert "scanned 5 files in 3 families: 2 ok, 3 findings" in text

    def test_alarms_come_before_inventory(self) -> None:
        text = sample_report().render_text()
        assert text.index("corruption") < text.index("unnamed")

    def test_details_are_shown(self) -> None:
        assert "name says aaaa" in sample_report().render_text()

    def test_empty_report(self) -> None:
        report = Report(ok=1, scanned=1, families=1)
        assert "1 ok, 0 findings" in report.render_text()
        assert not report.has_findings


class TestJson:
    def test_round_trips(self) -> None:
        payload = json.loads(sample_report().to_json())
        assert payload["summary"]["ok"] == 2
        assert payload["summary"]["corruption"] == 1
        assert payload["findings"][2]["related"] == ["a/z.nef"]


class TestMerge:
    def test_merge_accumulates(self) -> None:
        first = sample_report()
        second = Report(ok=10, scanned=20, families=15)
        first.merge(second)
        assert first.ok == 12
        assert first.scanned == 25
        assert len(first.findings) == 3
