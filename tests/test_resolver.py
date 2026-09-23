"""Tests for citation resolution and suggestions (SPEC 6.6)."""

from __future__ import annotations

import sqlite3

import pytest

from taxcite.citations import parse_citation
from taxcite.errors import SourceUnavailableError
from taxcite.models import RegKind, Severity, SourceType, Status
from taxcite.verify.resolver import Resolver, resolve_citations, worst_severity


def resolve(conn: sqlite3.Connection, citation: str, **kwargs: object) -> object:
    return Resolver(conn, **kwargs).resolve(parse_citation(citation))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "citation",
    [
        "§ 162",
        "§ 162(a)",
        "§ 162(a)(1)",
        "§ 7701(a)(30)(A)",
        "26 U.S.C. § 61",
        "§ 1400Z-2",
        "§ 263A",
    ],
)
def test_existing_provisions_verify(conn: sqlite3.Connection, citation: str) -> None:
    result = Resolver(conn).resolve(parse_citation(citation))
    assert result.status is Status.VERIFIED
    assert result.severity is Severity.OK


def test_verified_result_carries_heading_and_url(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 162(a)"))
    assert result.provision_heading == "In general"
    assert result.source_url is not None
    assert "title26-section162" in result.source_url


def test_missing_section_is_not_found(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 162A"))
    assert result.status is Status.NOT_FOUND
    assert result.severity is Severity.ERROR
    assert result.suggestion is not None
    assert "§ 162" in result.suggestion


def test_missing_pinpoint_names_the_real_subdivisions(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 162(z)"))
    assert result.status is Status.PINPOINT_NOT_FOUND
    assert result.severity is Severity.ERROR
    assert result.suggestion is not None
    assert "subsections (a)–(s)" in result.suggestion
    assert "no (z)" in result.suggestion


def test_missing_pinpoint_descends_to_the_deepest_real_ancestor(
    conn: sqlite3.Connection,
) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 7701(a)(30)(Z)"))
    assert result.status is Status.PINPOINT_NOT_FOUND
    assert result.suggestion is not None
    assert "§ 7701(a)(30) has" in result.suggestion


def test_263_subsection_a_versus_263a_confusion_is_named(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 263(a)(9)"))
    assert result.status is Status.PINPOINT_NOT_FOUND
    assert result.suggestion is not None
    assert "§ 263A" in result.suggestion


def test_repealed_section(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 4"))
    assert result.status is Status.REPEALED
    assert result.severity is Severity.WARNING
    assert "repealed" in result.message


def test_reserved_section(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 1000"))
    assert result.status is Status.RESERVED
    assert result.severity is Severity.WARNING


def test_omitted_section_is_reported_accurately(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 2614"))
    assert result.status is Status.REPEALED
    assert "omitted" in result.message


def test_subdivision_inherits_a_repealed_section_status(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 50A"))
    assert result.status is Status.REPEALED


@pytest.mark.parametrize(
    "citation",
    ["Rev. Rul. 2019-24", "Notice 2024-7", "PLR 202301001", "T.C. Memo. 2020-12"],
)
def test_out_of_scope_citations_are_unverifiable(conn: sqlite3.Connection, citation: str) -> None:
    result = Resolver(conn).resolve(parse_citation(citation))
    assert result.status is Status.UNVERIFIABLE
    assert result.severity is Severity.INFO
    assert "recognised but not verified" in result.message


def test_indexed_regulations_verify(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("Treas. Reg. § 1.263(a)-4(b)(1)"))
    assert result.status is Status.VERIFIED
    assert result.severity is Severity.OK


def test_temporary_regulations_verify(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 1.469-5T(a)"))
    assert result.status is Status.VERIFIED
    assert result.citation.reg_kind is RegKind.TEMPORARY


def test_proposed_regulations_resolve_against_the_final_rule(
    conn: sqlite3.Connection,
) -> None:
    result = Resolver(conn).resolve(parse_citation("Prop. Treas. Reg. § 1.162-1"))
    assert result.status is Status.VERIFIED
    assert "cited as proposed" in result.message


def test_unindexed_regulations_are_unavailable_without_a_fetcher(
    conn: sqlite3.Connection,
) -> None:
    result = Resolver(conn).resolve(parse_citation("Treas. Reg. § 1.9999-9(a)"))
    assert result.status is Status.SOURCE_UNAVAILABLE
    assert result.severity is Severity.WARNING


def test_offline_regulations_say_so(conn: sqlite3.Connection) -> None:
    result = Resolver(conn, offline=True).resolve(parse_citation("Treas. Reg. § 1.9999-9"))
    assert result.status is Status.SOURCE_UNAVAILABLE
    assert "offline" in result.message


def test_a_bad_reg_pinpoint_names_the_real_paragraphs(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("Treas. Reg. § 1.162-1(z)"))
    assert result.status is Status.PINPOINT_NOT_FOUND
    assert result.suggestion is not None
    assert "paragraphs (a), (b)" in result.suggestion


def test_an_unreachable_ecfr_is_not_reported_as_a_missing_citation(
    conn: sqlite3.Connection,
) -> None:
    """An outage must never look like a fabricated citation."""

    def unreachable(_connection: sqlite3.Connection, _section: str) -> bool:
        raise SourceUnavailableError("the eCFR is down")

    result = Resolver(conn, reg_fetcher=unreachable).resolve(
        parse_citation("Treas. Reg. § 1.9999-9")
    )
    assert result.status is Status.SOURCE_UNAVAILABLE


def test_level_warnings_surface_as_a_suggestion(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 7701(a)(30)"))
    assert result.status is Status.VERIFIED
    assert result.suggestion is None


def test_nearest_sections_ranks_by_edit_then_numeric_distance(
    conn: sqlite3.Connection,
) -> None:
    nearest = Resolver(conn).nearest_sections(parse_citation("§ 162A"))
    assert nearest[0] == "162"


def test_nearest_sections_finds_a_letter_suffix_variant(conn: sqlite3.Connection) -> None:
    nearest = Resolver(conn).nearest_sections(parse_citation("§ 263B"))
    assert "263A" in nearest


def test_nearest_sections_is_empty_for_guidance(conn: sqlite3.Connection) -> None:
    assert Resolver(conn).nearest_sections(parse_citation("Rev. Rul. 2019-24")) == []


def test_resolve_citations_helper(conn: sqlite3.Connection) -> None:
    from taxcite.citations import extract_citations

    results = resolve_citations(conn, extract_citations("§§ 162 and 162A"))
    assert [r.status for r in results] == [Status.VERIFIED, Status.NOT_FOUND]
    assert worst_severity(results) is Severity.ERROR


def test_worst_severity_of_nothing_is_ok() -> None:
    assert worst_severity([]) is Severity.OK


def test_reg_fetcher_is_consulted(conn: sqlite3.Connection) -> None:
    calls: list[str] = []

    def fetcher(_connection: sqlite3.Connection, section: str) -> bool:
        calls.append(section)
        return False

    result = Resolver(conn, reg_fetcher=fetcher).resolve(parse_citation("Treas. Reg. § 1.9999-9"))
    assert calls == ["1.9999-9"]
    assert result.status is Status.NOT_FOUND
    assert result.citation.source is SourceType.REG


def test_reg_fetcher_is_skipped_when_offline(conn: sqlite3.Connection) -> None:
    calls: list[str] = []

    def fetcher(_connection: sqlite3.Connection, section: str) -> bool:
        calls.append(section)
        return True

    result = Resolver(conn, offline=True, reg_fetcher=fetcher).resolve(
        parse_citation("Treas. Reg. § 1.9999-9")
    )
    assert calls == []
    assert result.status is Status.SOURCE_UNAVAILABLE
