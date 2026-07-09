"""Findings, buckets and report rendering.

Everything verify (and later, organize) discovers lands in one of a fixed
set of buckets, so reports stay comparable between runs. A finding is
never a guess: each carries the evidence in its detail line, and where
that evidence contains values a consumer would otherwise have to parse
back out of the prose (expected vs. actual timestamps, digests, the tag
that dated a file), the same values ride along machine-readably in
``data``.

Every bucket has a severity, so a consumer never has to re-derive what a
bucket *means*:

- ``alarm`` — data at risk or a change that failed; act now,
- ``attention`` — something to review or fix,
- ``expected`` — legitimate drift; worth knowing, still a finding,
- ``safe`` — a fully accounted-for state; never fails a command.

Text output orders alarms first, and the exit-code rule is encoded once
here: :data:`SAFE_BUCKETS` (the findings that never fail a command) is
exactly the buckets whose severity is ``safe``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Severity(Enum):
    ALARM = "alarm"
    ATTENTION = "attention"
    EXPECTED = "expected"
    SAFE = "safe"


class Bucket(Enum):
    DATE_MISMATCH = "date-mismatch"
    EDIT_DRIFT = "edit-drift"
    CORRUPTION = "corruption"
    UNRESOLVED_DATE = "unresolved-date"
    COLLISION = "collision"
    MALFORMED = "malformed"
    UNNAMED = "unnamed"
    ORPHAN_FAMILY = "orphan-family"
    AMBIGUOUS_MASTER = "ambiguous-master"
    METADATA_UNREADABLE = "metadata-unreadable"
    HASH_ERROR = "hash-error"
    ALREADY_IMPORTED = "already-imported"
    IGNORED = "ignored"
    TOKEN_PENDING = "token-pending"
    TOKEN_WRITTEN = "token-written"
    NEEDS_SIDECAR = "needs-sidecar"
    OTHER_PATTERN = "other-pattern"
    RENAME_PENDING = "rename-pending"
    RENAMED = "renamed"
    MTIME_DATED = "mtime-dated"
    NAME_DATED = "name-dated"
    APPLY_FAILED = "apply-failed"

    @property
    def severity(self) -> Severity:
        return _SEVERITIES[self]


_SEVERITIES = {
    Bucket.CORRUPTION: Severity.ALARM,
    Bucket.APPLY_FAILED: Severity.ALARM,
    Bucket.HASH_ERROR: Severity.ALARM,
    Bucket.METADATA_UNREADABLE: Severity.ATTENTION,
    Bucket.DATE_MISMATCH: Severity.ATTENTION,
    Bucket.UNRESOLVED_DATE: Severity.ATTENTION,
    Bucket.COLLISION: Severity.ATTENTION,
    Bucket.AMBIGUOUS_MASTER: Severity.ATTENTION,
    Bucket.ORPHAN_FAMILY: Severity.ATTENTION,
    Bucket.NEEDS_SIDECAR: Severity.ATTENTION,
    Bucket.OTHER_PATTERN: Severity.ATTENTION,
    Bucket.MTIME_DATED: Severity.ATTENTION,
    Bucket.MALFORMED: Severity.ATTENTION,
    Bucket.UNNAMED: Severity.ATTENTION,
    Bucket.EDIT_DRIFT: Severity.EXPECTED,
    Bucket.NAME_DATED: Severity.SAFE,
    Bucket.TOKEN_PENDING: Severity.SAFE,
    Bucket.TOKEN_WRITTEN: Severity.SAFE,
    Bucket.RENAME_PENDING: Severity.SAFE,
    Bucket.RENAMED: Severity.SAFE,
    Bucket.ALREADY_IMPORTED: Severity.SAFE,
    Bucket.IGNORED: Severity.SAFE,
}

#: Findings that describe a safe, fully accounted-for state rather than a
#: problem. They never fail a command's exit code.
SAFE_BUCKETS = frozenset(bucket for bucket in Bucket if bucket.severity is Severity.SAFE)


#: Rendering order: alarms first, expected drift and inventory last.
BUCKET_ORDER = (
    Bucket.CORRUPTION,
    Bucket.APPLY_FAILED,
    Bucket.HASH_ERROR,
    Bucket.METADATA_UNREADABLE,
    Bucket.DATE_MISMATCH,
    Bucket.UNRESOLVED_DATE,
    Bucket.COLLISION,
    Bucket.AMBIGUOUS_MASTER,
    Bucket.ORPHAN_FAMILY,
    Bucket.NEEDS_SIDECAR,
    Bucket.OTHER_PATTERN,
    Bucket.MTIME_DATED,
    Bucket.NAME_DATED,
    Bucket.MALFORMED,
    Bucket.EDIT_DRIFT,
    Bucket.UNNAMED,
    Bucket.TOKEN_PENDING,
    Bucket.TOKEN_WRITTEN,
    Bucket.RENAME_PENDING,
    Bucket.RENAMED,
    Bucket.ALREADY_IMPORTED,
    Bucket.IGNORED,
)


@dataclass(frozen=True)
class Finding:
    bucket: Bucket
    path: Path
    detail: str = ""
    related: tuple[Path, ...] = ()
    #: the detail's values, machine-readable; only set where the prose
    #: carries data worth consuming (never information of its own)
    data: Mapping[str, object] | None = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    ok: int = 0
    scanned: int = 0
    families: int = 0
    #: informational lines that never affect the exit code
    hints: list[str] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def merge(self, other: Report) -> None:
        self.findings.extend(other.findings)
        self.ok += other.ok
        self.scanned += other.scanned
        self.families += other.families
        self.hints.extend(other.hints)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def has_problems(self) -> bool:
        """Whether any finding describes something other than a safe state."""
        return any(finding.bucket not in SAFE_BUCKETS for finding in self.findings)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for finding in self.findings:
            result[finding.bucket.value] = result.get(finding.bucket.value, 0) + 1
        return result

    def to_json(self) -> str:
        payload = {
            "summary": {
                "scanned": self.scanned,
                "families": self.families,
                "ok": self.ok,
                **self.counts(),
            },
            "findings": [
                {
                    "bucket": finding.bucket.value,
                    "severity": finding.bucket.severity.value,
                    "path": str(finding.path),
                    "detail": finding.detail,
                    "related": [str(path) for path in finding.related],
                    **({"data": dict(finding.data)} if finding.data else {}),
                }
                for finding in self.findings
            ],
            "hints": list(self.hints),
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def render_text(self) -> str:
        lines = [
            f"scanned {self.scanned} files in {self.families} families: "
            f"{self.ok} ok, {len(self.findings)} findings"
        ]
        by_bucket: dict[Bucket, list[Finding]] = {}
        for finding in self.findings:
            by_bucket.setdefault(finding.bucket, []).append(finding)
        for bucket in BUCKET_ORDER:
            group = by_bucket.get(bucket)
            if not group:
                continue
            lines.append(f"\n{bucket.value} ({len(group)}):")
            for finding in group:
                detail = f"  {finding.detail}" if finding.detail else ""
                lines.append(f"  {finding.path}{detail}")
        for hint in self.hints:
            lines.append(f"\nhint: {hint}")
        return "\n".join(lines)
