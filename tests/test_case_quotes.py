"""Quotations from court decisions (roadmap 1.4).

Two routes are covered. When the opinion text is held locally — which needs a
CourtListener API token — the ordinary matcher runs and the pinpoint page is
checkable. When it is not, which is the usual situation, the quotation is checked by
asking the search backend how many of its opening words occur in that decision. The
opinion text used for the local-text tests is ``synthetic_``: it is invented, and
obviously so, because it exercises matching mechanics and not any real holding.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taxcite.citations import extract_citations, parse_citation
from taxcite.models import Status
from taxcite.sources import courtlistener as cl
from taxcite.verify.quotes import QuoteVerifier, extract_quotes, verify_quotes
from taxcite.verify.resolver import Resolver

# An invented opinion, with star pagination as the reporters print it. Page 112 holds
# the passage the tests quote; page 113 holds a different one.
SYNTHETIC_OPINION = (
    "*111 LOREM v. IPSUM. The taxpayer purchased seventeen decorative periscopes "
    "and sought to deduct their cost. *112 A periscope is ordinary and necessary "
    "to a submarine, and to nothing else. The Board so held, and we agree. *113 "
    "The remaining question, whether quarterly inspection fees are capital, we "
    "leave for another day."
)

SYNTHETIC_CASE = cl.CaseRecord(
    id=cl.canonical_id("999 U.S. 111"),
    reporter_cite="999 U.S. 111",
    case_name="Lorem v. Ipsum",
    court="Supreme Court of the United States",
    date_filed="1933-11-06",
    url="/opinion/999999/lorem-v-ipsum/",
    citations=["999 U.S. 111"],
    cite_count=3,
    text=SYNTHETIC_OPINION,
)

TEXTLESS_CASE = cl.CaseRecord(
    id=cl.canonical_id("999 U.S. 222"),
    reporter_cite="999 U.S. 222",
    case_name="Lorem v. Dolor",
    court="Supreme Court of the United States",
    date_filed="1940-01-02",
    url="/opinion/999998/lorem-v-dolor/",
    citations=["999 U.S. 222"],
    cite_count=1,
)

TRUE_QUOTE = "A periscope is ordinary and necessary to a submarine, and to nothing else."


@pytest.fixture
def cases(tmp_path: Path) -> sqlite3.Connection:
    """An index holding one decision with its text and one without."""
    from taxcite.index import db

    connection = db.connect(tmp_path / "cases.db")
    cl.index_case(connection, SYNTHETIC_CASE)
    cl.index_case(connection, TEXTLESS_CASE)
    return connection


def check(connection: sqlite3.Connection, document: str, **kwargs: object) -> list:
    """Verify one document and return the quote checks on its first citation."""
    citations = extract_citations(document)
    resolver = Resolver(connection)
    results = [resolver.resolve(citation) for citation in citations]
    verify_quotes(connection, document, citations, results, **kwargs)  # type: ignore[arg-type]
    return [quote for result in results for quote in result.quotes]


# --------------------------------------------------------------------------------------
# Pinpoint pages in the citation itself
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cite", "pin"),
    [
        ("Welch v. Helvering, 290 U.S. 111, 115 (1933)", "290 U.S. 111", 115),
        ("Welch v. Helvering, 290 U.S. 111 (1933)", "290 U.S. 111", None),
        ("See 290 U.S. 111, 115.", "290 U.S. 111", 115),
        ("Smith v. Commissioner, 123 T.C. 456, 470-72 (2004)", "123 T.C. 456", 470),
    ],
)
def test_a_pinpoint_page_is_read_off_the_citation(text: str, cite: str, pin: int | None) -> None:
    (citation,) = extract_citations(text)
    assert citation.reporter_cite == cite
    assert citation.pin_page == pin


def test_a_parallel_citation_is_not_a_pinpoint() -> None:
    """In "290 U.S. 111, 54 S. Ct. 8" the 54 is a volume number, not a page."""
    first, second = extract_citations("Welch v. Helvering, 290 U.S. 111, 54 S. Ct. 8 (1933)")
    assert first.pin_page is None
    assert second.reporter_cite == "54 S. Ct. 8"


# --------------------------------------------------------------------------------------
# With the opinion text held locally
# --------------------------------------------------------------------------------------


def test_a_verbatim_quotation_from_a_decision_passes(cases: sqlite3.Connection) -> None:
    (quote,) = check(cases, f'Lorem v. Ipsum, 999 U.S. 111 (1933) ("{TRUE_QUOTE}").')
    assert quote.status is Status.QUOTE_EXACT


def test_the_page_the_passage_sits_on_is_reported(cases: sqlite3.Connection) -> None:
    (quote,) = check(cases, f'Lorem v. Ipsum, 999 U.S. 111 (1933) ("{TRUE_QUOTE}").')
    assert quote.diff is not None
    assert "page 112" in quote.diff


def test_an_altered_quotation_is_close_not_exact(cases: sqlite3.Connection) -> None:
    altered = TRUE_QUOTE.replace("ordinary and necessary", "ordinary and essential")
    (quote,) = check(cases, f'Lorem v. Ipsum, 999 U.S. 111 (1933) ("{altered}").')
    assert quote.status is Status.QUOTE_CLOSE
    assert quote.diff is not None
    assert "essential" in quote.diff


def test_an_invented_quotation_is_not_found(cases: sqlite3.Connection) -> None:
    invented = "Every taxpayer may deduct the cost of a submarine without limitation."
    (quote,) = check(cases, f'Lorem v. Ipsum, 999 U.S. 111 (1933) ("{invented}").')
    assert quote.status is Status.QUOTE_NOT_FOUND


def test_a_correct_quotation_cited_to_the_wrong_page(cases: sqlite3.Connection) -> None:
    """The commonest real defect: the passage is genuine, the pinpoint is not."""
    (quote,) = check(cases, f'Lorem v. Ipsum, 999 U.S. 111, 117 (1933) ("{TRUE_QUOTE}").')
    assert quote.status is Status.QUOTE_WRONG_PINPOINT
    assert quote.diff is not None
    assert "page 112" in quote.diff
    assert "not page 117" in quote.diff


def test_the_right_page_is_not_flagged(cases: sqlite3.Connection) -> None:
    (quote,) = check(cases, f'Lorem v. Ipsum, 999 U.S. 111, 112 (1933) ("{TRUE_QUOTE}").')
    assert quote.status is Status.QUOTE_EXACT


def test_a_pinpoint_is_only_checked_against_real_pagination(cases: sqlite3.Connection) -> None:
    """An opinion with no star pagination cannot contradict a pinpoint."""
    from taxcite.index import db

    unpaginated = cl.CaseRecord(
        id=cl.canonical_id("999 U.S. 333"),
        reporter_cite="999 U.S. 333",
        case_name="Lorem v. Sit",
        court="Supreme Court of the United States",
        date_filed="1950-01-01",
        url="/opinion/999997/lorem-v-sit/",
        citations=["999 U.S. 333"],
        cite_count=1,
        text="Lorem v. Sit. " + TRUE_QUOTE,
    )
    cl.index_case(cases, unpaginated)
    del db
    (quote,) = check(cases, f'Lorem v. Sit, 999 U.S. 333, 400 (1950) ("{TRUE_QUOTE}").')
    assert quote.status is Status.QUOTE_EXACT
    assert quote.diff is None


# --------------------------------------------------------------------------------------
# Without the opinion text: the keyless probe
# --------------------------------------------------------------------------------------


def prober(answer: int | None) -> object:
    """A stand-in for the search backend that always gives the same answer."""

    def probe(_cite: str, _passage: str) -> int | None:
        return answer

    return probe


def test_a_passage_wholly_present_passes_with_a_caveat(cases: sqlite3.Connection) -> None:
    """A phrase search normalises punctuation away, and the report must say so."""
    document = f'Lorem v. Dolor, 999 U.S. 222 (1940) ("{TRUE_QUOTE}").'
    words = len(extract_quotes(document)[0].text.split())
    (quote,) = check(cases, document, case_prober=prober(words + 2))
    assert quote.status is Status.QUOTE_EXACT
    assert quote.diff is not None
    assert "punctuation" in quote.diff


def test_a_passage_that_diverges_names_the_word_it_diverges_at(
    cases: sqlite3.Connection,
) -> None:
    document = f'Lorem v. Dolor, 999 U.S. 222 (1940) ("{TRUE_QUOTE}").'
    (quote,) = check(cases, document, case_prober=prober(6))
    assert quote.status is Status.QUOTE_CLOSE
    assert quote.diff is not None
    assert "first 6 words" in quote.diff
    assert "to a submarine" in quote.diff


def test_a_passage_present_nowhere_is_not_found(cases: sqlite3.Connection) -> None:
    document = f'Lorem v. Dolor, 999 U.S. 222 (1940) ("{TRUE_QUOTE}").'
    (quote,) = check(cases, document, case_prober=prober(0))
    assert quote.status is Status.QUOTE_NOT_FOUND


def test_an_unsearchable_decision_is_unverifiable_not_wrong(
    cases: sqlite3.Connection,
) -> None:
    """A decision whose text was never ingested must not look like a fabrication."""
    document = f'Lorem v. Dolor, 999 U.S. 222 (1940) ("{TRUE_QUOTE}").'
    (quote,) = check(cases, document, case_prober=prober(None))
    assert quote.status is Status.UNVERIFIABLE
    assert quote.status is not Status.QUOTE_NOT_FOUND


def test_with_no_prober_at_all_the_quotation_is_unchecked(cases: sqlite3.Connection) -> None:
    document = f'Lorem v. Dolor, 999 U.S. 222 (1940) ("{TRUE_QUOTE}").'
    (quote,) = check(cases, document)
    assert quote.status is Status.UNVERIFIABLE
    assert quote.diff is not None
    assert "not available" in quote.diff


def test_the_probe_is_skipped_when_the_text_is_already_held(
    cases: sqlite3.Connection,
) -> None:
    """Held text is better evidence than a phrase search, and costs no requests."""
    calls: list[str] = []

    def probe(cite: str, _passage: str) -> int | None:
        calls.append(cite)
        return 0

    document = f'Lorem v. Ipsum, 999 U.S. 111 (1933) ("{TRUE_QUOTE}").'
    (quote,) = check(cases, document, case_prober=probe)
    assert quote.status is Status.QUOTE_EXACT
    assert calls == []


def test_an_unresolved_case_citation_does_not_reach_the_probe(
    cases: sqlite3.Connection,
) -> None:
    """Nothing is known about the decision, so nothing can be said about the quote."""
    calls: list[str] = []

    def probe(cite: str, _passage: str) -> int | None:
        calls.append(cite)
        return 0

    document = f'Lorem v. Nowhere, 999 U.S. 444 (1999) ("{TRUE_QUOTE}").'
    (quote,) = check(cases, document, case_prober=probe)
    assert calls == []
    assert quote.status is Status.UNVERIFIABLE
    assert quote.diff is not None
    assert "could not be read" in quote.diff


# --------------------------------------------------------------------------------------
# Star pagination
# --------------------------------------------------------------------------------------


def test_pages_are_read_off_the_markers() -> None:
    assert cl.pages_in(SYNTHETIC_OPINION) == [111, 112, 113]


@pytest.mark.parametrize(
    ("needle", "page"),
    [("seventeen", 111), ("submarine, and to nothing", 112), ("another day", 113)],
)
def test_the_page_of_an_offset(needle: str, page: int) -> None:
    assert cl.page_of(SYNTHETIC_OPINION, SYNTHETIC_OPINION.index(needle)) == page


def test_text_before_the_first_marker_has_no_page() -> None:
    assert cl.page_of("no markers here", 4) is None


def test_the_verifier_can_be_built_without_a_prober(cases: sqlite3.Connection) -> None:
    assert QuoteVerifier(cases).case_prober is None


def test_a_case_citation_still_resolves(cases: sqlite3.Connection) -> None:
    result = Resolver(cases).resolve(parse_citation("Lorem v. Ipsum, 999 U.S. 111 (1933)"))
    assert result.status is Status.VERIFIED


# --------------------------------------------------------------------------------------
# Declining to send the quotation
# --------------------------------------------------------------------------------------


def test_case_quote_checking_can_be_declined(tmp_path: Path) -> None:
    """--no-case-quotes must switch off the one request that carries document text."""
    from taxcite.verify.core import default_case_prober

    del tmp_path
    assert default_case_prober(True) is None
    assert default_case_prober(False) is not None


def test_offline_also_declines_it() -> None:
    from taxcite.verify.core import default_case_prober

    assert default_case_prober(offline=True) is None


# --------------------------------------------------------------------------------------
# What a check costs, and what happens when the budget runs out
# --------------------------------------------------------------------------------------


class FakeSearch:
    """A stand-in for the search backend that knows one opinion, and counts requests."""

    def __init__(self, text: str) -> None:
        """Hold one opinion's text and start the request log empty."""
        self.text = " ".join(text.lower().split())
        self.requests: list[str] = []

    def get_json(self, _url: str, params: dict[str, str] | None = None) -> dict[str, object]:
        query = (params or {}).get("q", "")
        self.requests.append(query)
        if " AND " not in query:
            return {"count": 1}
        phrase = query.split(" AND ", 1)[1].strip('"').lower()
        return {"count": 1 if phrase in self.text else 0}


OPINION = "a periscope is ordinary and necessary to a submarine and to nothing else at all"


def test_an_accurate_quotation_costs_one_request() -> None:
    """The common case is an accurate quotation; it must not pay for a binary search."""
    search = FakeSearch(OPINION)
    matched = cl.longest_matching_prefix(search, "999 U.S. 111", OPINION)  # type: ignore[arg-type]
    assert matched == len(OPINION.split())
    assert len(search.requests) == 1


def test_an_invented_quotation_is_settled_in_three() -> None:
    search = FakeSearch(OPINION)
    invented = "every taxpayer may deduct the cost of a submarine without any limitation"
    assert cl.longest_matching_prefix(search, "999 U.S. 111", invented) == 0  # type: ignore[arg-type]
    assert len(search.requests) == 3


def test_a_divergence_is_located_in_a_handful_of_requests() -> None:
    search = FakeSearch(OPINION)
    altered = "a periscope is ordinary and necessary to a submarine and to nothing else at sea"
    matched = cl.longest_matching_prefix(search, "999 U.S. 111", altered)  # type: ignore[arg-type]
    assert matched is not None
    assert 10 <= matched < len(altered.split())
    assert len(search.requests) <= 10


def test_the_budget_stops_the_requests() -> None:
    from taxcite.errors import SourceUnavailableError

    search = FakeSearch(OPINION)
    budget = cl.ProbeBudget(remaining=2)
    cl.longest_matching_prefix(search, "999 U.S. 111", OPINION, budget)  # type: ignore[arg-type]
    assert budget.remaining == 1
    cl.longest_matching_prefix(search, "999 U.S. 111", OPINION, budget)  # type: ignore[arg-type]
    with pytest.raises(SourceUnavailableError, match="budget"):
        cl.longest_matching_prefix(search, "999 U.S. 111", OPINION, budget)  # type: ignore[arg-type]


def test_a_spent_budget_leaves_a_quotation_unverifiable_not_wrong(
    cases: sqlite3.Connection,
) -> None:
    """Running out of quota must never read as "this quotation is invented"."""
    from taxcite.errors import SourceUnavailableError

    def probe(_cite: str, _passage: str) -> int | None:
        raise SourceUnavailableError("the request budget for checking case quotations is spent")

    document = f'Lorem v. Dolor, 999 U.S. 222 (1940) ("{TRUE_QUOTE}").'
    citations = extract_citations(document)
    resolver = Resolver(cases)
    results = [resolver.resolve(citation) for citation in citations]

    def guarded(cite: str, passage: str) -> int | None:
        try:
            return probe(cite, passage)
        except SourceUnavailableError:
            return None

    verify_quotes(cases, document, citations, results, case_prober=guarded)
    (quote,) = [q for result in results for q in result.quotes]
    assert quote.status is Status.UNVERIFIABLE
