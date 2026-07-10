"""Tests for report rendering."""

from __future__ import annotations

import json
from pathlib import Path

from stampla.report import BUCKET_ORDER, SAFE_BUCKETS, Bucket, Finding, Report, Severity


def sample_report() -> Report:
    report = Report(ok=2, scanned=5, groups=3)
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
        assert "scanned 5 files in 3 groups: 2 ok, 3 findings" in text

    def test_alarms_come_before_inventory(self) -> None:
        text = sample_report().render_text()
        assert text.index("corruption") < text.index("unnamed")

    def test_details_are_shown(self) -> None:
        assert "name says aaaa" in sample_report().render_text()

    def test_empty_report(self) -> None:
        report = Report(ok=1, scanned=1, groups=1)
        assert "1 ok, 0 findings" in report.render_text()
        assert not report.has_findings


class TestSeverity:
    def test_every_bucket_is_classified(self) -> None:
        for bucket in Bucket:
            assert isinstance(bucket.severity, Severity)

    def test_safe_buckets_are_exactly_the_safe_severity(self) -> None:
        assert frozenset(b for b in Bucket if b.severity is Severity.SAFE) == SAFE_BUCKETS

    def test_expected_drift_still_counts_as_a_problem(self) -> None:
        report = Report()
        report.add(Finding(Bucket.EDIT_DRIFT, Path("a/x.dng")))
        assert report.has_problems

    def test_rendering_order_covers_every_bucket_once(self) -> None:
        assert sorted(BUCKET_ORDER, key=lambda b: b.value) == sorted(Bucket, key=lambda b: b.value)
        assert len(BUCKET_ORDER) == len(set(BUCKET_ORDER))


class TestJson:
    def test_round_trips(self) -> None:
        payload = json.loads(sample_report().to_json())
        assert payload["summary"]["ok"] == 2
        assert payload["summary"]["buckets"]["corruption"] == 1
        # paths are rendered in the platform's native form
        assert payload["findings"][2]["related"] == [str(Path("a/z.nef"))]

    def test_paths_become_relative_to_the_root(self) -> None:
        root = Path("/archive").resolve()
        report = Report()
        report.add(Finding(Bucket.CORRUPTION, root / "Photos" / "x.nef"))
        report.add(Finding(Bucket.UNNAMED, Path("/elsewhere/card/y.jpg")))
        findings = json.loads(report.to_json(root))["findings"]
        assert findings[0]["path"] == str(Path("Photos") / "x.nef")
        assert findings[1]["path"] == str(Path("/elsewhere/card/y.jpg"))

    def test_findings_carry_their_severity(self) -> None:
        payload = json.loads(sample_report().to_json())
        assert payload["findings"][0]["severity"] == "alarm"
        assert payload["findings"][1]["severity"] == "attention"

    def test_data_appears_only_when_set(self) -> None:
        report = Report()
        report.add(Finding(Bucket.DATE_MISMATCH, Path("a/x.nef"), data={"source": "EXIF"}))
        report.add(Finding(Bucket.UNNAMED, Path("a/y.nef")))
        findings = json.loads(report.to_json())["findings"]
        assert findings[0]["data"] == {"source": "EXIF"}
        assert "data" not in findings[1]


class TestMerge:
    def test_merge_accumulates(self) -> None:
        first = sample_report()
        second = Report(ok=10, scanned=20, groups=15)
        first.merge(second)
        assert first.ok == 12
        assert first.scanned == 25
        assert len(first.findings) == 3
