"""The memo fixtures drive verification end to end (SPEC 12).

Each `memos/*.md` has a sibling `*.expected.json` naming every planted error and the
status it must receive. These tests assert the existence-related expectations; the
quotation expectations are asserted in ``test_quotes.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from taxcite.citations import extract_citations
from taxcite.models import SEVERITY_ORDER, Severity, Status, VerificationReport
from taxcite.verify import verify_text

from conftest import MEMO_DIR

MEMOS = sorted(MEMO_DIR.glob("*.md"))

#: Statuses that a regulation citation may carry while the eCFR index is optional.
_REG_STATUSES = {Status.VERIFIED, Status.SOURCE_UNAVAILABLE, Status.NOT_FOUND}


def _expected(memo: Path) -> dict[str, object]:
    return json.loads(
        memo.with_suffix("").with_suffix(".expected.json").read_text(encoding="utf-8")
    )


def _memo_cases() -> Iterator[tuple[Path, dict[str, object]]]:
    for memo in MEMOS:
        yield memo, _expected(memo)


def test_every_memo_has_expectations() -> None:
    assert MEMOS
    for memo in MEMOS:
        assert memo.with_suffix("").with_suffix(".expected.json").exists()


@pytest.mark.parametrize("memo", MEMOS, ids=lambda p: p.stem)
def test_planted_errors_get_their_expected_status(conn: sqlite3.Connection, memo: Path) -> None:
    expected = _expected(memo)
    report: VerificationReport = verify_text(memo.read_text(encoding="utf-8"), connection=conn)
    actual = [(r.citation.display, r.status) for r in report.results]
    for entry in expected["citations"]:  # type: ignore[index]
        display = entry["display"]
        matches = [status for shown, status in actual if shown == display]
        assert matches, f"{memo.name}: {display} was not extracted at all"
        if entry["status"] == "reg":
            assert set(matches) <= _REG_STATUSES, f"{memo.name}: {display} -> {matches}"
        else:
            assert Status(entry["status"]) in matches, f"{memo.name}: {display} -> {matches}"


@pytest.mark.parametrize("memo", MEMOS, ids=lambda p: p.stem)
def test_planted_quote_errors_get_their_expected_status(
    conn: sqlite3.Connection, memo: Path
) -> None:
    expected = _expected(memo)
    report = verify_text(memo.read_text(encoding="utf-8"), connection=conn)
    by_display: dict[str, list[str]] = {}
    for result in report.results:
        by_display.setdefault(result.citation.display, []).extend(
            quote.status.value for quote in result.quotes
        )
    for entry in expected["citations"]:  # type: ignore[index]
        wanted = entry.get("quotes")
        if wanted is None:
            continue
        actual = by_display.get(entry["display"], [])
        assert sorted(actual) == sorted(wanted), f"{memo.name}: {entry['display']}"


@pytest.mark.parametrize("memo", MEMOS, ids=lambda p: p.stem)
def test_orphan_quotations_match_the_fixture(conn: sqlite3.Connection, memo: Path) -> None:
    expected = _expected(memo)
    report = verify_text(memo.read_text(encoding="utf-8"), connection=conn)
    actual = sorted(quote.status.value for quote in report.orphan_quotes)
    assert actual == sorted(expected.get("orphan_quotes", []))  # type: ignore[union-attr]


@pytest.mark.parametrize("memo", MEMOS, ids=lambda p: p.stem)
def test_suggestions_point_at_the_right_provision(conn: sqlite3.Connection, memo: Path) -> None:
    expected = _expected(memo)
    report = verify_text(memo.read_text(encoding="utf-8"), connection=conn)
    by_display = {r.citation.display: r for r in report.results}
    for entry in expected["citations"]:  # type: ignore[index]
        wanted = entry.get("suggestion_contains")
        if wanted is None:
            continue
        result = by_display[entry["display"]]
        assert result.suggestion is not None, f"{memo.name}: {entry['display']} has none"
        assert wanted in result.suggestion


@pytest.mark.parametrize("memo", MEMOS, ids=lambda p: p.stem)
def test_false_friends_are_never_matched(memo: Path) -> None:
    expected = _expected(memo)
    text = memo.read_text(encoding="utf-8")
    citations = extract_citations(text)
    for phrase in expected["must_not_match"]:  # type: ignore[index]
        start = text.index(phrase)
        end = start + len(phrase)
        overlapping = [c for c in citations if c.span[0] < end and start < c.span[1]]
        assert not overlapping, f"{memo.name}: matched inside {phrase!r}: {overlapping}"


@pytest.mark.parametrize("memo", MEMOS, ids=lambda p: p.stem)
def test_every_extracted_citation_is_accounted_for(conn: sqlite3.Connection, memo: Path) -> None:
    """No citation may appear that the fixture does not list."""
    expected = _expected(memo)
    listed = {entry["display"] for entry in expected["citations"]}  # type: ignore[index]
    report = verify_text(memo.read_text(encoding="utf-8"), connection=conn)
    found = {r.citation.display for r in report.results}
    assert found <= listed, f"{memo.name}: unexpected citations {sorted(found - listed)}"


def test_control_memo_reports_no_problems(conn: sqlite3.Connection) -> None:
    memo = MEMO_DIR / "memo02_quotes_good.md"
    report = verify_text(memo.read_text(encoding="utf-8"), connection=conn)
    worst = max((SEVERITY_ORDER[r.worst_severity] for r in report.results), default=0)
    assert worst <= SEVERITY_ORDER[Severity.OK], [
        (r.citation.display, r.status) for r in report.results if r.worst_severity != Severity.OK
    ]


def test_list_and_range_expansion_in_a_memo(conn: sqlite3.Connection) -> None:
    memo = MEMO_DIR / "memo04_lists_and_edits.md"
    report = verify_text(memo.read_text(encoding="utf-8"), connection=conn)
    sections = {r.citation.section for r in report.results}
    assert {"1031", "1032", "1033"} <= sections
    displays = {r.citation.display for r in report.results}
    assert {"I.R.C. § 162(a)", "I.R.C. § 162(b)", "I.R.C. § 162(c)"} <= displays


def test_a_five_page_memo_verifies_quickly(conn: sqlite3.Connection) -> None:
    """SPEC 13: verifying a five-page memo offline takes under two seconds."""
    import time

    body = "\n\n".join(memo.read_text(encoding="utf-8") for memo in MEMOS)
    text = body * 4  # comfortably more than five pages of prose
    assert len(text) > 15_000
    start = time.perf_counter()
    verify_text(text, offline=True, connection=conn)
    assert time.perf_counter() - start < 2.0
