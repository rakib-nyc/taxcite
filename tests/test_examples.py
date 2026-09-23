"""The shipped example memo and its report are checked against the real behaviour."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taxcite.models import Severity, Status
from taxcite.verify import verify_text
from taxcite.verify.report import to_markdown

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SAMPLE_MEMO = EXAMPLES / "sample_memo.md"
SAMPLE_REPORT = EXAMPLES / "sample_report.md"
SNAPSHOT = Path(__file__).parent / "fixtures" / "snapshots" / "sample_report.md"

#: Every error deliberately planted in examples/sample_memo.md.
PLANTED = {
    "I.R.C. § 263(a)(9)": Status.PINPOINT_NOT_FOUND,
    "I.R.C. § 162A": Status.NOT_FOUND,
    "I.R.C. § 162(z)": Status.PINPOINT_NOT_FOUND,
    "I.R.C. § 4": Status.REPEALED,
}

#: Every quotation error planted in the sample memo, by the citation it hangs off.
PLANTED_QUOTES = {
    "I.R.C. § 162(b)": Status.QUOTE_WRONG_PINPOINT,
    "I.R.C. § 263(a)": Status.QUOTE_MISATTRIBUTED,
    "I.R.C. § 61(a)": Status.QUOTE_NOT_FOUND,
}


@pytest.fixture
def report(conn: sqlite3.Connection):
    return verify_text(SAMPLE_MEMO.read_text(encoding="utf-8"), connection=conn)


def test_sample_report_matches_the_snapshot(conn: sqlite3.Connection) -> None:
    """The report rendered from the fixture index is byte-identical to the snapshot.

    ``examples/sample_report.md`` is the same report rendered against a full index; the
    two differ only in the nearest-section suggestions, which naturally depend on how
    much of Title 26 is indexed.
    """
    text = SAMPLE_MEMO.read_text(encoding="utf-8")
    rendered = to_markdown(verify_text(text, connection=conn), text=text)
    assert rendered == SNAPSHOT.read_text(encoding="utf-8")


def test_every_planted_citation_error_is_caught(report) -> None:
    found = {r.citation.display: r.status for r in report.results}
    for display, status in PLANTED.items():
        assert found.get(display) is status, f"{display}: {found.get(display)}"


def test_every_planted_quotation_error_is_caught(report) -> None:
    found = {r.citation.display: [q.status for q in r.quotes] for r in report.results if r.quotes}
    for display, status in PLANTED_QUOTES.items():
        assert status in found.get(display, []), f"{display}: {found.get(display)}"


def test_no_false_errors_on_the_valid_citations(report) -> None:
    """Everything not deliberately broken must come back clean."""
    broken = set(PLANTED) | set(PLANTED_QUOTES)
    offenders = [
        (r.citation.display, r.status, [q.status for q in r.quotes])
        for r in report.results
        if r.citation.display not in broken
        and r.worst_severity in {Severity.ERROR, Severity.WARNING}
        and r.status is not Status.SOURCE_UNAVAILABLE
    ]
    assert not offenders


def test_engagement_agreement_reference_is_not_a_citation(report) -> None:
    assert not any("Engagement" in r.citation.raw for r in report.results)


def test_shipped_report_names_every_planted_error() -> None:
    rendered = SAMPLE_REPORT.read_text(encoding="utf-8")
    for display in {**PLANTED, **PLANTED_QUOTES}:
        assert display in rendered
    for status in {*PLANTED.values(), *PLANTED_QUOTES.values()}:
        assert status.value in rendered


def test_shipped_report_carries_the_disclaimer() -> None:
    assert "not tax advice" in SAMPLE_REPORT.read_text(encoding="utf-8")


def test_sample_memo_says_it_contains_planted_errors() -> None:
    assert "planted" in SAMPLE_MEMO.read_text(encoding="utf-8")
