"""Tax Court citations, which tax practice uses more than any other kind.

Three separate defects lived here, and all three were invisible until the live source
was actually asked about real Tax Court citations:

1. ``155 T.C. No. 8`` and ``68 T.C.M. 89`` did not parse at all, so they were silently
   never checked.
2. ``T.C. Memo. 2020-12`` parsed but never resolved, because the source stores the same
   decision as ``2020 T.C. Memo. 12``. It was therefore reported as a decision that does
   not exist — a false accusation on the most-cited category in tax.
3. The source's Tax Court coverage is genuinely incomplete, so even with the right
   query a miss is not evidence of nonexistence.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taxcite.citations import extract_citations, parse_citation
from taxcite.index import db
from taxcite.models import SourceType, Status
from taxcite.sources import courtlistener as cl
from taxcite.verify.resolver import Resolver

# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cite"),
    [
        ("Deckard v. Commissioner, 155 T.C. No. 8 (2020)", "155 T.C. No. 8"),
        ("Pasqualini v. Commissioner, 68 T.C.M. 89", "68 T.C.M. 89"),
        ("Smith v. Commissioner, T.C. Memo. 2020-12", "T.C. Memo. 2020-12"),
        ("Estate of Jones v. Commissioner, 81 T.C. 806 (1983)", "81 T.C. 806"),
        ("Brown v. Commissioner, 2020 T.C. Summary Opinion 4", None),
    ],
)
def test_tax_court_citations_are_recognised(text: str, cite: str | None) -> None:
    found = extract_citations(text)
    if cite is None:
        return
    assert [c.reporter_cite for c in found if c.source is SourceType.CASE] == [cite]


def test_a_tax_court_number_is_not_read_as_a_pinpoint() -> None:
    """In "155 T.C. No. 8" the 8 is the opinion number, part of the citation."""
    (citation,) = extract_citations("Deckard v. Commissioner, 155 T.C. No. 8 (2020)")
    assert citation.reporter_cite == "155 T.C. No. 8"
    assert citation.pin_page is None


# --------------------------------------------------------------------------------------
# The two spellings of a memorandum citation
# --------------------------------------------------------------------------------------


def test_a_memorandum_citation_is_also_queried_the_way_the_source_stores_it() -> None:
    assert cl.query_forms("T.C. Memo. 2020-12") == [
        "T.C. Memo. 2020-12",
        "2020 T.C. Memo. 12",
    ]


def test_a_leading_zero_is_dropped_in_the_source_form() -> None:
    assert cl.query_forms("T.C. Memo. 1994-023")[1] == "1994 T.C. Memo. 23"


@pytest.mark.parametrize("cite", ["290 U.S. 111", "81 T.C. 806", "155 T.C. No. 8"])
def test_other_citations_are_queried_once(cite: str) -> None:
    """Only the memorandum reporter has two spellings; nothing else pays for a retry."""
    assert cl.query_forms(cite) == [cite]


# --------------------------------------------------------------------------------------
# A miss is not evidence
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cite", ["T.C. Memo. 2020-12", "155 T.C. No. 8", "81 T.C. 806", "68 T.C.M. 89", "5 B.T.A. 1"]
)
def test_tax_court_reporters_are_known_to_be_partly_covered(cite: str) -> None:
    assert cl.coverage_is_partial(cite)


@pytest.mark.parametrize("cite", ["290 U.S. 111", "480 U.S. 23", "999 F.3d 1"])
def test_other_reporters_are_covered(cite: str) -> None:
    assert not cl.coverage_is_partial(cite)


@pytest.fixture
def empty(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "cases.db")


def test_an_unfound_tax_court_citation_is_not_called_fabricated(
    empty: sqlite3.Connection,
) -> None:
    """The source's Tax Court coverage is incomplete, so a miss proves nothing."""

    def fetcher(_c: sqlite3.Connection, _cite: str) -> None:
        return None

    result = Resolver(empty, case_fetcher=fetcher).resolve(
        parse_citation("Smith v. Commissioner, T.C. Memo. 2020-12")
    )
    assert result.status is Status.UNVERIFIABLE
    assert result.status is not Status.NOT_FOUND
    assert "not evidence" in result.message
    assert result.suggestion is not None
    assert "dawson.ustaxcourt.gov" in result.suggestion


def test_an_unfound_supreme_court_citation_is_still_an_error(
    empty: sqlite3.Connection,
) -> None:
    """Coverage of the published federal reporters is complete; a miss there counts."""

    def fetcher(_c: sqlite3.Connection, _cite: str) -> None:
        return None

    result = Resolver(empty, case_fetcher=fetcher).resolve(
        parse_citation("Smith v. Jones, 999 U.S. 999 (2020)")
    )
    assert result.status is Status.NOT_FOUND


# --------------------------------------------------------------------------------------
# A year that has not happened
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "year"),
    [
        ("Welch v. Helvering, 290 U.S. 111 (1933)", 1933),
        ("Smith v. Commissioner, T.C. Memo. 1994-323", 1994),
        ("Deckard v. Commissioner, 155 T.C. No. 8 (2020)", 2020),
        ("See 290 U.S. 111", None),
    ],
)
def test_the_year_is_read_off_the_citation(text: str, year: int | None) -> None:
    (citation,) = extract_citations(text)
    assert citation.case_year == year


def test_a_decision_dated_in_the_future_is_an_error(empty: sqlite3.Connection) -> None:
    """Certain without consulting any source, and fabrications often carry bad years."""

    def fetcher(_c: sqlite3.Connection, _cite: str) -> None:
        return None

    result = Resolver(empty, case_fetcher=fetcher).resolve(
        parse_citation("Smith v. Commissioner, T.C. Memo. 2099-999")
    )
    assert result.status is Status.NOT_FOUND
    assert "in the future" in result.message


def test_the_future_check_outranks_the_coverage_caveat(empty: sqlite3.Connection) -> None:
    """Partial coverage excuses a miss; it does not excuse a year that cannot exist."""

    def fetcher(_c: sqlite3.Connection, _cite: str) -> None:
        return None

    result = Resolver(empty, case_fetcher=fetcher).resolve(
        parse_citation("Smith v. Commissioner, T.C. Memo. 2099-999")
    )
    assert result.status is not Status.UNVERIFIABLE


def test_a_decision_from_this_year_is_not_flagged(empty: sqlite3.Connection) -> None:
    from datetime import UTC, datetime

    def fetcher(_c: sqlite3.Connection, _cite: str) -> None:
        return None

    year = datetime.now(UTC).year
    result = Resolver(empty, case_fetcher=fetcher).resolve(
        parse_citation(f"Smith v. Commissioner, T.C. Memo. {year}-1")
    )
    assert "in the future" not in result.message
