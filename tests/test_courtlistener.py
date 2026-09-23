"""Tests for case-law verification through CourtListener (roadmap 1.4)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from taxcite.citations import extract_citations, parse_citation
from taxcite.index import db
from taxcite.models import Status
from taxcite.sources import courtlistener as cl
from taxcite.verify.resolver import Resolver

WELCH = cl.CaseRecord(
    id=cl.canonical_id("290 U.S. 111"),
    reporter_cite="290 U.S. 111",
    case_name="Welch v. Helvering",
    court="Supreme Court of the United States",
    date_filed="1933-11-06",
    url="/opinion/102139/welch-v-helvering/",
    citations=["290 U.S. 111", "54 S. Ct. 8"],
    cite_count=1700,
)


# --------------------------------------------------------------------------------------
# Splitting a case citation into its parts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "name", "cite"),
    [
        ("Welch v. Helvering, 290 U.S. 111 (1933)", "Welch v. Helvering", "290 U.S. 111"),
        ("See Welch v. Helvering, 290 U.S. 111", "Welch v. Helvering", "290 U.S. 111"),
        ("Compare Smith v. Jones, 999 U.S. 999", "Smith v. Jones", "999 U.S. 999"),
        (
            "Estate of Smith v. Commissioner, 123 T.C. 456",
            "Estate of Smith v. Commissioner",
            "123 T.C. 456",
        ),
        ("123 T.C. 456", None, "123 T.C. 456"),
    ],
)
def test_name_and_cite_are_separated(text: str, name: str | None, cite: str) -> None:
    """They fail independently: a real cite can carry the wrong name."""
    (citation,) = extract_citations(text)
    assert citation.case_name == name
    assert citation.reporter_cite == cite


@pytest.mark.parametrize("signal", ["See", "Compare", "Cf.", "Accord", "But see"])
def test_introductory_signals_are_not_party_names(signal: str) -> None:
    (citation,) = extract_citations(f"{signal} Welch v. Helvering, 290 U.S. 111")
    assert citation.case_name == "Welch v. Helvering"


# --------------------------------------------------------------------------------------
# Name comparison
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cited", "reported"),
    [
        ("Welch v. Helvering", "Welch v. Helvering"),
        ("Welch v. Helvering", "WELCH v. HELVERING"),
        ("Groetzinger v. Comm'r", "Commissioner v. Groetzinger"),
        ("Estate of Smith v. Commissioner", "Smith v. Commissioner"),
        ("Acme, Inc. v. United States", "Acme Inc. v. United States"),
    ],
)
def test_names_that_should_match(cited: str, reported: str) -> None:
    matched, _score = cl.names_match(cited, reported)
    assert matched


@pytest.mark.parametrize(
    ("cited", "reported"),
    [
        ("Welch v. Helvering", "Commissioner v. Groetzinger"),
        ("Smith v. Jones", "Brown v. Board of Education"),
    ],
)
def test_names_that_should_not_match(cited: str, reported: str) -> None:
    matched, _score = cl.names_match(cited, reported)
    assert not matched


def test_a_missing_name_is_not_a_mismatch() -> None:
    assert cl.names_match("", "Welch v. Helvering")[0]


def test_canonical_ids() -> None:
    assert cl.canonical_id("290 U.S. 111") == "/us/case/290-u-s-111"
    assert cl.canonical_id("T.C. Memo. 2020-12") == "/us/case/t-c-memo-2020-12"


# --------------------------------------------------------------------------------------
# Resolution against the cache
# --------------------------------------------------------------------------------------


@pytest.fixture
def cached(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "cases.db")
    cl.index_case(connection, WELCH)
    return connection


def test_a_cached_case_verifies(cached: sqlite3.Connection) -> None:
    result = Resolver(cached).resolve(parse_citation("Welch v. Helvering, 290 U.S. 111 (1933)"))
    assert result.status is Status.VERIFIED
    assert result.source_url is not None
    assert "courtlistener.com" in result.source_url


def test_the_result_says_how_a_quotation_was_matched(
    cached: sqlite3.Connection,
) -> None:
    """The keyless API has no opinion text; a reader must know what passed."""
    result = Resolver(cached).resolve(parse_citation("Welch v. Helvering, 290 U.S. 111 (1933)"))
    assert result.suggestion is not None
    assert "matched on words alone" in result.suggestion


def test_a_pinpoint_that_could_not_be_checked_says_so(cached: sqlite3.Connection) -> None:
    """Without the opinion text there is no pagination, so no page to compare."""
    result = Resolver(cached).resolve(
        parse_citation("Welch v. Helvering, 290 U.S. 111, 115 (1933)")
    )
    assert result.suggestion is not None
    assert "pinpoint page could not be checked" in result.suggestion


def test_a_wrong_name_on_a_real_citation_is_caught(cached: sqlite3.Connection) -> None:
    result = Resolver(cached).resolve(parse_citation("Smith v. Jones, 290 U.S. 111 (1933)"))
    assert result.status is Status.NOT_FOUND
    assert "Welch v. Helvering" in result.message
    assert result.suggestion is not None
    assert "disagree" in result.suggestion


def test_an_uncached_case_offline_stays_unverifiable(cached: sqlite3.Connection) -> None:
    result = Resolver(cached, offline=True).resolve(
        parse_citation("Smith v. Jones, 999 U.S. 999 (2020)")
    )
    assert result.status is Status.UNVERIFIABLE


def test_a_lookup_that_finds_nothing_is_not_found(cached: sqlite3.Connection) -> None:
    def fetcher(_c: sqlite3.Connection, _cite: str) -> None:
        return None

    result = Resolver(cached, case_fetcher=fetcher).resolve(
        parse_citation("Smith v. Jones, 999 U.S. 999 (2020)")
    )
    assert result.status is Status.NOT_FOUND
    assert "999 U.S. 999" in result.message


def test_an_unreachable_courtlistener_does_not_accuse(cached: sqlite3.Connection) -> None:
    """An outage must not turn a real case into a fabricated one."""
    from taxcite.errors import SourceUnavailableError

    def fetcher(_c: sqlite3.Connection, _cite: str) -> None:
        raise SourceUnavailableError("down")

    result = Resolver(cached, case_fetcher=fetcher).resolve(
        parse_citation("Smith v. Jones, 999 U.S. 999 (2020)")
    )
    assert result.status is Status.UNVERIFIABLE
    assert result.status is not Status.NOT_FOUND


def test_the_cache_is_used_before_the_network(cached: sqlite3.Connection) -> None:
    calls: list[str] = []

    def fetcher(_c: sqlite3.Connection, cite: str) -> None:
        calls.append(cite)
        return None

    Resolver(cached, case_fetcher=fetcher).resolve(
        parse_citation("Welch v. Helvering, 290 U.S. 111")
    )
    assert calls == []


def test_cached_round_trip(cached: sqlite3.Connection) -> None:
    record = cl.cached(cached, "290 U.S. 111")
    assert record is not None
    assert record.case_name == "Welch v. Helvering"
    assert record.citations == ["290 U.S. 111", "54 S. Ct. 8"]
    assert record.absolute_url.startswith("https://www.courtlistener.com/")


def test_nothing_cached_returns_nothing(cached: sqlite3.Connection) -> None:
    assert cl.cached(cached, "999 U.S. 999") is None


def test_result_filtering_requires_the_exact_cite() -> None:
    assert cl._matches_cite({"citation": ["290 U.S. 111"]}, "290 U.S. 111")
    assert cl._matches_cite({"citation": ["290 U.S.  111"]}, "290 U.S. 111")
    assert not cl._matches_cite({"citation": ["111 S. Ct. 290"]}, "290 U.S. 111")
    assert not cl._matches_cite({}, "290 U.S. 111")


# --------------------------------------------------------------------------------------
# Citation history, and the boundary it does not cross
# --------------------------------------------------------------------------------------


def test_history_reads_as_a_clause() -> None:
    record = replace(WELCH, citing_count=1401, last_cited="2026-08-20")
    described = cl.describe_history(record)
    assert described is not None
    assert "1,401 later decisions" in described
    assert "2026-08-20" in described


def test_a_case_nothing_cites_is_described_as_such() -> None:
    record = replace(WELCH, citing_count=0)
    assert cl.describe_history(record) == "no later decision in CourtListener cites this one"


def test_an_unknown_history_says_nothing() -> None:
    assert cl.describe_history(WELCH) is None


def test_one_citing_decision_is_not_pluralised() -> None:
    record = replace(WELCH, citing_count=1)
    described = cl.describe_history(record)
    assert described is not None
    assert described == "cited in 1 later decision"


def test_history_survives_the_cache_round_trip(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "hist.db")
    cl.index_case(
        connection,
        replace(WELCH, citing_count=1401, last_cited="2026-08-20"),
    )
    record = cl.cached(connection, "290 U.S. 111")
    assert record is not None
    assert record.citing_count == 1401
    assert record.last_cited == "2026-08-20"


def test_a_report_with_a_case_says_treatment_is_not_checked(
    cached: sqlite3.Connection,
) -> None:
    """A reader must never take "verified" for "still good law"."""
    from taxcite.verify import verify_text
    from taxcite.verify.report import to_markdown

    report = verify_text(
        "See Welch v. Helvering, 290 U.S. 111 (1933).", offline=True, connection=cached
    )
    rendered = to_markdown(report)
    assert "reversed, vacated, or overruled" in rendered
    assert rendered.count("reversed, vacated, or overruled") == 1


def test_a_report_with_no_case_does_not_carry_the_case_note(conn: sqlite3.Connection) -> None:
    from taxcite.verify import verify_text
    from taxcite.verify.report import to_markdown

    report = verify_text("See I.R.C. § 162(a).", offline=True, connection=conn)
    assert "reversed, vacated, or overruled" not in to_markdown(report)


def test_history_is_reported_when_it_is_known(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "hist2.db")
    cl.index_case(
        connection,
        replace(WELCH, citing_count=1401, last_cited="2026-08-20"),
    )
    result = Resolver(connection).resolve(parse_citation("Welch v. Helvering, 290 U.S. 111 (1933)"))
    assert result.suggestion is not None
    assert "1,401 later decisions" in result.suggestion


def test_a_case_with_no_cluster_id_is_left_alone() -> None:
    """A history query needs a cluster; without one the fields stay unknown."""

    class Unused:
        def get_json(self, *_a: object, **_k: object) -> dict[str, object]:
            raise AssertionError("should not have been called")

    record = cl.history(Unused(), replace(WELCH))  # type: ignore[arg-type]
    assert record.citing_count is None
