"""Tests for the public Python API (SPEC 8)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taxcite import api
from taxcite.errors import IndexNotBuiltError, MalformedCitationError, ProvisionNotFoundError
from taxcite.models import SourceType


def test_lookup_returns_real_text(conn: sqlite3.Connection) -> None:
    result = api.lookup("§ 162(a)", include_children=False, connection=conn)
    assert result.text.startswith("There shall be allowed as a deduction all the ordinary")
    assert result.provision.heading == "In general"
    assert result.citation.display == "I.R.C. § 162(a)"
    assert result.source_version == "119-110"


def test_lookup_children_extends_the_text(conn: sqlite3.Connection) -> None:
    own = api.lookup("§ 162(a)", include_children=False, connection=conn)
    full = api.lookup("§ 162(a)", include_children=True, connection=conn)
    assert "reasonable allowance for salaries" not in own.text
    assert "reasonable allowance for salaries" in full.text
    assert [child.num for child in full.children] == ["(1)", "(2)", "(3)"]


def test_lookup_source_url(conn: sqlite3.Connection) -> None:
    result = api.lookup("§ 162", connection=conn)
    assert result.source_url == (
        "https://uscode.house.gov/view.xhtml?"
        "req=granuleid:USC-prelim-title26-section162&num=0&edition=prelim"
    )


def test_lookup_truncates_on_a_word_boundary(conn: sqlite3.Connection) -> None:
    result = api.lookup("§ 162", max_chars=100, connection=conn)
    assert result.truncated
    assert len(result.text) <= 100
    assert not result.text.endswith(" ")


def test_lookup_deep_pinpoint(conn: sqlite3.Connection) -> None:
    result = api.lookup("§ 7701(a)(30)(A)", connection=conn)
    assert result.text == "a citizen or resident of the United States,"


def test_lookup_unknown_provision(conn: sqlite3.Connection) -> None:
    with pytest.raises(ProvisionNotFoundError, match="not in the index"):
        api.lookup("§ 162(z)", connection=conn)


def test_lookup_rejects_unparseable_input(conn: sqlite3.Connection) -> None:
    with pytest.raises(MalformedCitationError):
        api.lookup("what is the rule about lunches", connection=conn)


def test_lookup_rejects_out_of_scope_citation(conn: sqlite3.Connection) -> None:
    with pytest.raises(ProvisionNotFoundError, match="not a statutory or regulatory"):
        api.lookup("Rev. Rul. 2019-24", connection=conn)


@pytest.mark.parametrize(
    ("text", "max_chars", "expected", "truncated"),
    [
        ("short text", 100, "short text", False),
        ("one two three four", 7, "one two", True),
        ("abcdefghij", 4, "abcd", True),
        ("anything", 0, "anything", False),
    ],
)
def test_truncate(text: str, max_chars: int, expected: str, truncated: bool) -> None:
    assert api.truncate(text, max_chars) == (expected, truncated)


def test_search_wrapper(conn: sqlite3.Connection) -> None:
    hits = api.search("ordinary and necessary", limit=3, connection=conn)
    assert "/us/usc/t26/s162/a" in [hit.provision_id for hit in hits]


@pytest.mark.parametrize("source", ["all", None, "irc", SourceType.IRC])
def test_search_source_argument_forms(
    conn: sqlite3.Connection, source: str | SourceType | None
) -> None:
    assert api.search("income", source=source, limit=2, connection=conn)


def test_list_subdivisions(conn: sqlite3.Connection) -> None:
    provision, children = api.list_subdivisions("§ 162", connection=conn)
    assert provision.heading == "Trade or business expenses"
    assert "(a)" in [child.num for child in children]


def test_index_info(conn: sqlite3.Connection) -> None:
    info = api.index_info(connection=conn)
    assert info["usc_release_point"] == "119-110"
    assert int(info["irc_sections"]) >= 15
    assert int(info["irc_provisions"]) > 1000
    assert int(info["reg_provisions"]) > 100
    assert info["ecfr_date"] == "2026-09-08"


def test_open_index_rejects_a_missing_index(tmp_path: Path) -> None:
    with pytest.raises(IndexNotBuiltError), api.open_index(tmp_path / "nope.db"):
        pass


def test_lazy_public_exports() -> None:
    import taxcite

    assert callable(taxcite.extract_citations)
    assert callable(taxcite.parse_citation)
    with pytest.raises(AttributeError):
        _ = taxcite.does_not_exist


def test_lookup_a_regulation(conn: sqlite3.Connection) -> None:
    result = api.lookup("Treas. Reg. \u00a7 1.162-1(a)", include_children=False, connection=conn)
    assert result.text.startswith("Business expenses deductible from gross income")
    assert result.provision.source is SourceType.REG
    assert result.source_url is not None
    assert result.source_url.startswith("https://www.ecfr.gov/current/title-26/section-1.162-1")


def test_lookup_an_unindexed_regulation_offline(conn: sqlite3.Connection) -> None:
    with pytest.raises(ProvisionNotFoundError, match="not in the index"):
        api.lookup("Treas. Reg. \u00a7 1.9999-9", offline=True, connection=conn)
