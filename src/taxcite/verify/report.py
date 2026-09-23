"""Rendering a verification report (SPEC 10).

Three output formats share one report object: Markdown for humans and for the MCP
client, JSON for machines, and GitHub workflow annotations for CI.
"""

from __future__ import annotations

import json
from typing import Final

from taxcite.models import (
    SEVERITY_ORDER,
    CitationResult,
    QuoteCheck,
    Severity,
    SourceType,
    Status,
    VerificationReport,
    severity_for,
)

#: How each severity is rendered in a Markdown table.
_MARKER: Final[dict[Severity, str]] = {
    Severity.OK: "ok",
    Severity.INFO: "info",
    Severity.WARNING: "warning",
    Severity.ERROR: "ERROR",
}

DISCLAIMER: Final = (
    "TaxCite verifies that cited provisions exist and that quoted language matches "
    "the official source. It does not judge whether a legal conclusion is correct, "
    "and it is not tax advice."
)

#: Printed once when a report contains a court decision. Existence, case name and
#: quotation are checked; treatment is not, and cannot be from a free source. Stating
#: it once where it will be read beats repeating it on every row until it is skipped.
CASE_NOTE: Final = (
    "Court decisions are checked for existence, case name and quoted language. "
    "TaxCite does **not** check whether a decision has been reversed, vacated, or "
    "overruled: no free source publishes treatment signals, so that remains a job for "
    "a citator."
)


class LineIndex:
    """Converts character offsets in a document into 1-based line and column numbers."""

    def __init__(self, text: str) -> None:
        """Index the newline positions of ``text``."""
        self._starts = [0]
        for position, char in enumerate(text):
            if char == "\n":
                self._starts.append(position + 1)

    def locate(self, offset: int) -> tuple[int, int]:
        """Return the 1-based line and column of ``offset``."""
        low, high = 0, len(self._starts) - 1
        while low < high:
            middle = (low + high + 1) // 2
            if self._starts[middle] <= offset:
                low = middle
            else:
                high = middle - 1
        return low + 1, offset - self._starts[low] + 1


def to_json(report: VerificationReport) -> str:
    """Render the report as indented JSON."""
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=False)


def overall_severity(report: VerificationReport) -> Severity:
    """Return the most serious severity anywhere in the report."""
    worst = Severity.OK
    for result in report.results:
        if SEVERITY_ORDER[result.worst_severity] > SEVERITY_ORDER[worst]:
            worst = result.worst_severity
    for quote in report.orphan_quotes:
        severity = severity_for(quote.status)
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[worst]:
            worst = severity
    return worst


def to_markdown(report: VerificationReport, *, text: str | None = None) -> str:
    """Render the report as Markdown: a header, a table, and per-finding details."""
    lines = LineIndex(text) if text is not None else None
    worst = overall_severity(report)
    out: list[str] = ["# TaxCite verification report", ""]

    verdict = {
        Severity.OK: "All citations and quotations check out.",
        Severity.INFO: "No problems found; some citations could not be verified.",
        Severity.WARNING: "Some citations need attention.",
        Severity.ERROR: "Some citations or quotations are wrong.",
    }[worst]
    out += [f"**{verdict}**", ""]
    out += [_counts_line(report), ""]
    if report.source_versions:
        rendered = ", ".join(f"{k}: {v}" for k, v in sorted(report.source_versions.items()))
        out += [f"Sources — {rendered}. TaxCite {report.taxcite_version}.", ""]
    else:
        out += [f"TaxCite {report.taxcite_version}.", ""]

    if not report.results and not report.orphan_quotes:
        out += ["No tax citations found.", "", f"> {DISCLAIMER}", ""]
        return "\n".join(out)

    out += ["| # | Citation | Line | Status | Note |", "|---:|---|---:|---|---|"]
    for position, result in enumerate(report.results, start=1):
        line = lines.locate(result.citation.span[0])[0] if lines else ""
        out.append(
            f"| {position} | {_escape(result.citation.display)} | {line} "
            f"| {_status_cell(result)} | {_escape(_note(result))} |"
        )
    out.append("")

    details = [
        (position, result)
        for position, result in enumerate(report.results, start=1)
        if result.worst_severity is not Severity.OK
    ]
    if details or report.orphan_quotes:
        out += ["## Findings", ""]
    for position, result in details:
        out += _detail(position, result, lines)
    for quote in report.orphan_quotes:
        out += _orphan_detail(quote, lines)

    if any(result.citation.source is SourceType.CASE for result in report.results):
        out += [f"> {CASE_NOTE}", ""]
    out += [f"> {DISCLAIMER}", ""]
    return "\n".join(out)


def _counts_line(report: VerificationReport) -> str:
    """Render the one-line count summary."""
    summary = report.summary
    parts = [f"{summary.get('citations', 0)} citations"]
    if summary.get("quotes"):
        parts.append(f"{summary['quotes']} quotations")
    for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        count = summary.get(severity.value, 0)
        if count:
            parts.append(f"{count} {severity.value}")
    return " · ".join(parts)


def _status_cell(result: CitationResult) -> str:
    """Render the status column, folding in the worst quote status."""
    statuses = [result.status.value]
    statuses += [quote.status.value for quote in result.quotes]
    marker = _MARKER[result.worst_severity]
    return f"{marker}: {', '.join(statuses)}"


def _note(result: CitationResult) -> str:
    """Render the note column."""
    note = result.message
    if result.suggestion:
        note = f"{note} — {result.suggestion}"
    return note


def _detail(position: int, result: CitationResult, lines: LineIndex | None) -> list[str]:
    """Render the details block for one non-OK citation."""
    location = _location(result.citation.span[0], lines)
    out = [f"### {position}. {result.citation.display}{location}", ""]
    out.append(f"- **Status:** `{result.status.value}` ({result.severity.value})")
    out.append(f"- **Detail:** {result.message}")
    if result.provision_heading:
        out.append(f"- **Heading:** {result.provision_heading}")
    if result.suggestion:
        out.append(f"- **Suggestion:** {result.suggestion}")
    if result.source_url:
        out.append(f"- **Source:** {result.source_url}")
    if result.citation.warnings:
        out.append(f"- **Warnings:** {'; '.join(result.citation.warnings)}")
    out.append("")
    for quote in result.quotes:
        if severity_for(quote.status) is Severity.OK:
            continue
        out += _quote_block(quote)
    return out


def _orphan_detail(quote: QuoteCheck, lines: LineIndex | None) -> list[str]:
    """Render the details block for a quotation with no citation attached."""
    location = _location(quote.span[0], lines)
    out = [f"### Unattributed quotation{location}", ""]
    out += _quote_block(quote)
    return out


def _quote_block(quote: QuoteCheck) -> list[str]:
    """Render one quote check."""
    out = [
        f"- **Quote:** \u201c{_escape(_shorten(quote.quote))}\u201d",
        f"- **Quote status:** `{quote.status.value}` (score {quote.score:.2f})",
    ]
    if quote.matched_provision_id:
        out.append(f"- **Matched:** `{quote.matched_provision_id}`")
    if quote.matched_text:
        out.append(f"- **Source text:** \u201c{_escape(_shorten(quote.matched_text))}\u201d")
    if quote.diff:
        out.append(f"- **Difference:** {quote.diff}")
    out.append("")
    return out


def _location(offset: int, lines: LineIndex | None) -> str:
    """Render " (line N)" when line numbers are available."""
    if lines is None:
        return ""
    return f" (line {lines.locate(offset)[0]})"


def _shorten(text: str, limit: int = 300) -> str:
    """Trim long quotations for display."""
    return text if len(text) <= limit else f"{text[:limit].rstrip()}\u2026"


def _escape(text: str) -> str:
    """Escape the characters that would break a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def to_github(report: VerificationReport, *, text: str, filename: str) -> str:
    """Render the report as GitHub workflow annotations.

    Only warnings and errors are annotated; ``ok`` and ``info`` findings would drown
    the diff view in noise.
    """
    lines = LineIndex(text)
    out: list[str] = []
    for result in report.results:
        severity = result.worst_severity
        if severity in {Severity.OK, Severity.INFO}:
            continue
        line, column = lines.locate(result.citation.span[0])
        end_column = column + (result.citation.span[1] - result.citation.span[0])
        level = "error" if severity is Severity.ERROR else "warning"
        message = _note(result)
        for quote in result.quotes:
            if severity_for(quote.status) in {Severity.OK, Severity.INFO}:
                continue
            excerpt = _shorten(quote.quote, 120)
            message = f"{message} | quote {quote.status.value}: \u201c{excerpt}\u201d"
        out.append(
            f"::{level} file={filename},line={line},col={column},endColumn={end_column},"
            f"title={result.citation.display}::{_annotation(message)}"
        )
    for quote in report.orphan_quotes:
        if severity_for(quote.status) in {Severity.OK, Severity.INFO}:
            continue
        line, column = lines.locate(quote.span[0])
        level = "error" if severity_for(quote.status) is Severity.ERROR else "warning"
        excerpt = _shorten(quote.quote, 120)
        detail = _annotation(f"{quote.status.value}: \u201c{excerpt}\u201d")
        out.append(
            f"::{level} file={filename},line={line},col={column},"
            f"title=Unattributed quotation::{detail}"
        )
    return "\n".join(out)


def _annotation(message: str) -> str:
    """Escape a message for a GitHub workflow command."""
    return (
        message.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace("::", ":\u200b:")
    )


def render(
    report: VerificationReport, fmt: str, *, text: str | None = None, filename: str = ""
) -> str:
    """Render a report in the named format.

    Raises:
        ValueError: if ``fmt`` is not one of ``md``, ``json``, or ``github``.
    """
    match fmt:
        case "md" | "markdown":
            return to_markdown(report, text=text)
        case "json":
            return to_json(report)
        case "github":
            return to_github(report, text=text or "", filename=filename)
        case _:
            raise ValueError(f"unknown report format: {fmt!r}")


def status_counts(report: VerificationReport) -> dict[Status, int]:
    """Return the number of findings for each status."""
    counts: dict[Status, int] = {}
    for result in report.results:
        counts[result.status] = counts.get(result.status, 0) + 1
        for quote in result.quotes:
            counts[quote.status] = counts.get(quote.status, 0) + 1
    for quote in report.orphan_quotes:
        counts[quote.status] = counts.get(quote.status, 0) + 1
    return counts
