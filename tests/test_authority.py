"""Tests for authority classification under Treas. Reg. § 1.6662-4 (roadmap 2.1)."""

from __future__ import annotations

import sqlite3

import pytest

from taxcite.citations import extract_citations, parse_citation
from taxcite.models import SourceType, Status
from taxcite.verify import authority
from taxcite.verify.authority import AuthorityKind


def _classify(citation: str) -> authority.AuthorityAssessment:
    return authority.classify(parse_citation(citation))


# --------------------------------------------------------------------------------------
# The enumerated list
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("citation", "kind"),
    [
        ("§ 162(a)", AuthorityKind.STATUTE),
        ("26 U.S.C. § 61", AuthorityKind.STATUTE),
        ("Treas. Reg. § 1.162-1(a)", AuthorityKind.REGULATION),
        ("Temp. Treas. Reg. § 1.469-5T(a)", AuthorityKind.REGULATION),
        ("Prop. Treas. Reg. § 1.199A-1", AuthorityKind.REGULATION),
        ("Rev. Rul. 2019-24", AuthorityKind.PUBLISHED_RULING),
        ("Rev. Proc. 2023-34", AuthorityKind.PUBLISHED_RULING),
        ("Notice 2024-7", AuthorityKind.IRB_PRONOUNCEMENT),
        ("Ann. 2023-1", AuthorityKind.IRB_PRONOUNCEMENT),
        ("T.D. 9959", AuthorityKind.REGULATION),
        ("PLR 202301001", AuthorityKind.PRIVATE_RULING),
        ("TAM 9822004", AuthorityKind.PRIVATE_RULING),
        ("GCM 39000", AuthorityKind.INTERNAL_MEMORANDUM),
        ("AOD 2012-01", AuthorityKind.INTERNAL_MEMORANDUM),
        ("Welch v. Helvering, 290 U.S. 111 (1933)", AuthorityKind.CASE),
    ],
)
def test_kind(citation: str, kind: AuthorityKind) -> None:
    assert _classify(citation).kind is kind


@pytest.mark.parametrize(
    "citation",
    [
        "§ 162(a)",
        "Treas. Reg. § 1.162-1(a)",
        "Rev. Rul. 2019-24",
        "Notice 2024-7",
        "PLR 202301001",
        "Welch v. Helvering, 290 U.S. 111 (1933)",
    ],
)
def test_these_are_authority(citation: str) -> None:
    assert _classify(citation).is_authority


@pytest.mark.parametrize(
    "citation",
    [
        "Mertens, Law of Federal Income Taxation § 25.01",
        "85 Tax L. Rev. 123",
        "Bittker & Eustice ¶ 3.02",
    ],
)
def test_commentary_is_not_authority(citation: str) -> None:
    """The regulation says so in terms, and this is the whole point of the feature."""
    assessment = _classify(citation)
    assert not assessment.is_authority
    assert assessment.kind is AuthorityKind.SECONDARY
    assert "not authority" in assessment.reason


def test_commentary_points_at_the_underlying_authorities() -> None:
    assessment = _classify("85 Tax L. Rev. 123")
    assert any("underlying" in caveat for caveat in assessment.caveats)


# --------------------------------------------------------------------------------------
# The two date cutoffs
# --------------------------------------------------------------------------------------


def test_a_private_ruling_from_before_1976_is_not_authority() -> None:
    assessment = _classify("PLR 7512001")
    assert not assessment.is_authority
    assert "1976-10-31" in assessment.reason


def test_a_modern_private_ruling_is_authority() -> None:
    assert _classify("PLR 202301001").is_authority


def test_an_unreadable_issue_date_is_admitted_rather_than_assumed() -> None:
    """A sequential GCM number carries no year; saying so beats implying a check."""
    assessment = _classify("GCM 39000")
    assert assessment.is_authority
    assert any("has not been checked" in caveat for caveat in assessment.caveats)


def test_a_private_ruling_carries_its_limits() -> None:
    caveats = " ".join(_classify("PLR 202301001").caveats)
    assert "binds only the taxpayer" in caveats
    assert "revoked" in caveats


def test_a_temporary_regulation_carries_the_sunset_caveat() -> None:
    caveats = " ".join(_classify("Temp. Treas. Reg. § 1.469-5T(a)").caveats)
    assert "7805(e)" in caveats


def test_a_proposed_regulation_is_authority_but_weaker() -> None:
    assessment = _classify("Prop. Treas. Reg. § 1.199A-1")
    assert assessment.is_authority
    assert any("less weight" in caveat for caveat in assessment.caveats)


def test_a_case_carries_the_overruling_caveat() -> None:
    caveats = " ".join(_classify("Welch v. Helvering, 290 U.S. 111 (1933)").caveats)
    assert "overruled" in caveats


# --------------------------------------------------------------------------------------
# Whole documents
# --------------------------------------------------------------------------------------


def test_analyse_deduplicates() -> None:
    citations = extract_citations("§ 162(a) and again § 162(a) and § 263")
    report = authority.analyse(citations)
    assert len(report.assessments) == 2


def test_a_nonexistent_section_is_not_authority() -> None:
    citations = extract_citations("§ 162A supports this")
    report = authority.analyse(citations, statuses={"/us/usc/t26/s162A": Status.NOT_FOUND})
    assert not report.assessments[0].is_authority
    assert "does not exist" in report.assessments[0].reason
    assert report.unresolved == ["I.R.C. § 162A"]


def test_a_repealed_section_is_not_authority() -> None:
    citations = extract_citations("§ 4 supports this")
    report = authority.analyse(citations, statuses={"/us/usc/t26/s4": Status.REPEALED})
    assert not report.assessments[0].is_authority
    assert "no longer operative" in report.assessments[0].reason


def test_a_provision_that_did_not_govern_the_year_is_caveated() -> None:
    citations = extract_citations("§ 199A(a) applied")
    report = authority.analyse(
        citations, statuses={"/us/usc/t26/s199A/a": Status.NOT_YET_EFFECTIVE}
    )
    assert any("did not govern" in c for c in report.assessments[0].caveats)


def test_counts_by_kind() -> None:
    citations = extract_citations("§ 162(a), § 263, Rev. Rul. 2019-24, 85 Tax L. Rev. 123")
    counts = authority.analyse(citations).counts
    assert counts["statute"] == 2
    assert counts["published ruling"] == 1
    assert counts["commentary"] == 1


def test_end_to_end_against_the_index(conn: sqlite3.Connection) -> None:
    text = (
        "The rule is I.R.C. § 162(a); see also Rev. Rul. 2019-24. "
        "But § 162A does not exist, § 4 was repealed, and "
        "Mertens, Law of Federal Income Taxation § 25.01 is commentary."
    )
    report, year = authority.analyse_text(text, connection=conn)
    assert year is None
    by_display = {a.citation.display: a for a in report.assessments}
    assert by_display["I.R.C. § 162(a)"].is_authority
    assert by_display["Rev. Rul. 2019-24"].is_authority
    assert not by_display["I.R.C. § 162A"].is_authority
    assert not by_display["I.R.C. § 4"].is_authority
    assert not by_display["Mertens, Law of Federal Income Taxation § 25.01"].is_authority


def test_markdown_states_what_it_does_not_do(conn: sqlite3.Connection) -> None:
    report, _year = authority.analyse_text("See § 162(a).", connection=conn)
    rendered = authority.to_markdown(report)
    assert "1.6662-4(d)(3)(iii)" in rendered
    assert "classification, not a weighing" in rendered
    assert "relevance and persuasiveness" in rendered


def test_markdown_for_an_empty_document(conn: sqlite3.Connection) -> None:
    report, _year = authority.analyse_text("Nothing here.", connection=conn)
    assert "No citations found." in authority.to_markdown(report)


def test_markdown_names_the_tax_year(conn: sqlite3.Connection) -> None:
    report, year = authority.analyse_text("See § 162(a).", connection=conn, tax_year=2022)
    assert "for tax year 2022" in authority.to_markdown(report, tax_year=year)


# --------------------------------------------------------------------------------------
# Secondary sources as citations
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Mertens, Law of Federal Income Taxation § 25.01",
        "Saltzman, IRS Practice and Procedure ¶ 7.03",
        "85 Tax L. Rev. 123",
        "55 Tax Law. 401",
        "Tax Management Portfolio 500-3rd",
    ],
)
def test_secondary_sources_are_extracted(text: str) -> None:
    citations = extract_citations(text)
    assert citations
    assert any(c.source is SourceType.SECONDARY for c in citations)


def test_secondary_sources_are_unverifiable(conn: sqlite3.Connection) -> None:
    from taxcite.verify.resolver import Resolver

    (citation,) = extract_citations("85 Tax L. Rev. 123")
    result = Resolver(conn).resolve(citation)
    assert result.status is Status.UNVERIFIABLE
    assert "Commentary" in result.message


def test_a_statute_is_not_mistaken_for_commentary() -> None:
    citations = extract_citations("I.R.C. § 162(a)")
    assert all(c.source is not SourceType.SECONDARY for c in citations)
