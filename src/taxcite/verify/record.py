"""A diligence record for a verification run (roadmap 2.4).

IRS Office of Professional Responsibility Alert 2026-19 ties the use of AI to the
duties a practitioner already has under Circular 230. Section 10.22 requires the
practitioner to review every AI-generated document before it reaches a client or the
Service, and the OPR names verification of *citations* among the things that review
must cover.

A verification report on a terminal satisfies the duty and proves nothing about it
afterwards. This writes the same run down in a form that can sit in an engagement
file: what was checked, against which published sources, when, by which version of
which tool, and what it found. The document is identified by a hash rather than
reproduced, so the record can be kept without copying privileged text into it.

What a record establishes and what it does not is stated in the record itself, because
a compliance artifact that overstates itself is worse than none.
"""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from taxcite.models import SEVERITY_ORDER, Severity, VerificationReport, severity_for
from taxcite.verify.report import overall_severity

#: The guidance this record is designed to evidence compliance with.
AUTHORITY_NOTE: Final = (
    "Circular 230 \u00a7 10.22 (due diligence), as applied to AI-assisted work by IRS "
    "Office of Professional Responsibility Alert 2026-19."
)

SCOPE_NOTE: Final = (
    "This record evidences that the citations and quotations in the identified "
    "document were checked mechanically against the official published sources named "
    "below, and what that check found. It does not evidence that the document's legal "
    "analysis is correct, that its conclusions are sound, or that any authority cited "
    "supports the proposition it is cited for. Those are matters of professional "
    "judgment that no tool performs and that this record does not address."
)


def document_hash(text: str) -> str:
    """Return a stable SHA-256 of the document, so it can be identified but not copied."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class DiligenceRecord:
    """One verification run, written down."""

    document: str
    document_hash: str
    document_bytes: int
    checked_at: datetime
    taxcite_version: str
    source_versions: dict[str, str]
    tax_year: int | None
    as_of: str | None
    offline: bool
    counts: dict[str, int]
    findings: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "ok"
    environment: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render the record as a JSON-serialisable mapping."""
        return {
            "record_version": 1,
            "authority": AUTHORITY_NOTE,
            "scope": SCOPE_NOTE,
            "document": self.document,
            "document_hash": self.document_hash,
            "document_bytes": self.document_bytes,
            "checked_at": self.checked_at.isoformat(),
            "taxcite_version": self.taxcite_version,
            "source_versions": self.source_versions,
            "tax_year": self.tax_year,
            "as_of": self.as_of,
            "offline": self.offline,
            "outcome": self.outcome,
            "counts": self.counts,
            "findings": self.findings,
            "environment": self.environment,
        }


def build(
    report: VerificationReport,
    *,
    document: str,
    text: str,
    offline: bool,
) -> DiligenceRecord:
    """Turn a verification report into a diligence record."""
    findings: list[dict[str, Any]] = []
    for result in report.results:
        if result.worst_severity is Severity.OK:
            continue
        findings.append(
            {
                "citation": result.citation.display,
                "canonical_id": result.citation.canonical_id,
                "status": result.status.value,
                "severity": result.severity.value,
                "message": result.message,
                "suggestion": result.suggestion,
                "source_url": result.source_url,
                "quotes": [
                    {
                        "status": quote.status.value,
                        "severity": severity_for(quote.status).value,
                        "score": quote.score,
                        "matched_provision_id": quote.matched_provision_id,
                    }
                    for quote in result.quotes
                    if severity_for(quote.status) is not Severity.OK
                ],
            }
        )
    for quote in report.orphan_quotes:
        if severity_for(quote.status) is Severity.OK:
            continue
        findings.append(
            {
                "citation": None,
                "status": quote.status.value,
                "severity": severity_for(quote.status).value,
                "message": "quotation with no citation attached",
                "matched_provision_id": quote.matched_provision_id,
            }
        )

    versions = dict(report.source_versions)
    return DiligenceRecord(
        document=document,
        document_hash=document_hash(text),
        document_bytes=len(text.encode("utf-8")),
        checked_at=report.generated_at,
        taxcite_version=report.taxcite_version,
        source_versions={
            key: value for key, value in versions.items() if key not in {"tax_year", "as_of"}
        },
        tax_year=int(versions["tax_year"]) if "tax_year" in versions else None,
        as_of=versions.get("as_of"),
        offline=offline,
        counts=dict(report.summary),
        findings=findings,
        outcome=overall_severity(report).value,
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(terse=True),
        },
    )


def append(path: Path, record: DiligenceRecord) -> None:
    """Append a record to a JSON Lines log.

    Append-only on purpose: a diligence log that can be edited in place evidences
    nothing. One line per run, newest last.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict(), sort_keys=False) + "\n")


def read_log(path: Path) -> list[dict[str, Any]]:
    """Read a diligence log back."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def to_markdown(record: DiligenceRecord) -> str:
    """Render a record for a human, or for printing into a file."""
    sources = (
        ", ".join(f"{key}: {value}" for key, value in sorted(record.source_versions.items()))
        or "none recorded"
    )
    out = [
        "# Citation diligence record",
        "",
        f"- **Document:** `{record.document}`",
        f"- **Identified by:** `{record.document_hash}` ({record.document_bytes} bytes)",
        f"- **Checked:** {record.checked_at.isoformat()}",
        f"- **Tool:** TaxCite {record.taxcite_version}",
        f"- **Sources:** {sources}",
    ]
    if record.tax_year is not None:
        out.append(f"- **Tax year checked against:** {record.tax_year}")
    if record.as_of:
        out.append(f"- **Law as of:** {record.as_of}")
    out += [
        f"- **Network access during the check:** {'none' if record.offline else 'permitted'}",
        f"- **Outcome:** {record.outcome}",
        f"- **Findings:** {len(record.findings)}",
        "",
    ]
    if record.findings:
        out += ["| Citation | Status | Detail |", "|---|---|---|"]
        for finding in record.findings:
            citation = finding.get("citation") or "(unattributed quotation)"
            detail = finding.get("suggestion") or finding.get("message") or ""
            out.append(
                f"| {_escape(str(citation))} | `{finding['status']}` | {_escape(str(detail))} |"
            )
        out.append("")
    out += [
        "## What this record does and does not establish",
        "",
        SCOPE_NOTE,
        "",
        f"Prepared with reference to {AUTHORITY_NOTE}",
        "",
    ]
    return "\n".join(out)


def _escape(text: str) -> str:
    """Escape a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def worst_of(records: list[DiligenceRecord]) -> Severity:
    """Return the most serious outcome across several records."""
    worst = Severity.OK
    for record in records:
        severity = Severity(record.outcome)
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[worst]:
            worst = severity
    return worst


def now() -> datetime:
    """Return the current UTC time, for records built outside a report."""
    return datetime.now(UTC)


__all__ = [
    "AUTHORITY_NOTE",
    "SCOPE_NOTE",
    "DiligenceRecord",
    "append",
    "build",
    "document_hash",
    "now",
    "read_log",
    "to_markdown",
    "worst_of",
]
