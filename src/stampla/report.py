"""Findings, buckets and report rendering.

Everything verify (and later, organize) discovers lands in one of a fixed
set of buckets, so reports stay comparable between runs. A finding is
never a guess: each carries the evidence in its detail line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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


#: Findings that describe a safe, fully accounted-for state rather than a
#: problem. They never fail a command's exit code.
SAFE_BUCKETS = frozenset(
    {Bucket.ALREADY_IMPORTED, Bucket.IGNORED, Bucket.TOKEN_PENDING, Bucket.TOKEN_WRITTEN}
)


#: Rendering order: alarms first, expected drift and inventory last.
_BUCKET_ORDER = (
    Bucket.CORRUPTION,
    Bucket.HASH_ERROR,
    Bucket.METADATA_UNREADABLE,
    Bucket.DATE_MISMATCH,
    Bucket.UNRESOLVED_DATE,
    Bucket.COLLISION,
    Bucket.AMBIGUOUS_MASTER,
    Bucket.ORPHAN_FAMILY,
    Bucket.NEEDS_SIDECAR,
    Bucket.OTHER_PATTERN,
    Bucket.MALFORMED,
    Bucket.EDIT_DRIFT,
    Bucket.UNNAMED,
    Bucket.TOKEN_PENDING,
    Bucket.TOKEN_WRITTEN,
    Bucket.ALREADY_IMPORTED,
    Bucket.IGNORED,
)


@dataclass(frozen=True)
class Finding:
    bucket: Bucket
    path: Path
    detail: str = ""
    related: tuple[Path, ...] = ()


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    ok: int = 0
    scanned: int = 0
    families: int = 0

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def merge(self, other: Report) -> None:
        self.findings.extend(other.findings)
        self.ok += other.ok
        self.scanned += other.scanned
        self.families += other.families

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
                    "path": str(finding.path),
                    "detail": finding.detail,
                    "related": [str(path) for path in finding.related],
                }
                for finding in self.findings
            ],
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
        for bucket in _BUCKET_ORDER:
            group = by_bucket.get(bucket)
            if not group:
                continue
            lines.append(f"\n{bucket.value} ({len(group)}):")
            for finding in group:
                detail = f"  {finding.detail}" if finding.detail else ""
                lines.append(f"  {finding.path}{detail}")
        return "\n".join(lines)
