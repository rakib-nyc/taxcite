"""Tests for defined-term extraction and lookup (SPEC 6.9)."""

from __future__ import annotations

import sqlite3

import pytest

from taxcite.graph import definitions as defs


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("the term “capital asset” means property held by the taxpayer", "capital asset"),
        ("The term “person” includes an individual", "person"),
        ("the terms “spouse” and “husband” mean a person", "spouse"),
        ("“gross receipts” means all receipts", "gross receipts"),
        ("gross income means all income from whatever source derived", "gross income"),
    ],
)
def test_extract_definitions(text: str, term: str) -> None:
    assert term in [found[0] for found in defs.extract_definitions(text)]


def test_two_terms_defined_together() -> None:
    text = "the terms “spouse” and “husband” mean a married person"
    assert [found[0] for found in defs.extract_definitions(text)] == ["spouse", "husband"]


def test_the_longer_reading_of_an_overlapping_match_wins() -> None:
    text = "For purposes of this section, the term “widget” means a small thing."
    found = list(defs.extract_definitions(text))
    assert len(found) == 1
    assert found[0][0] == "widget"


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        ("For purposes of this section, the term “x y” means a thing", "this section"),
        ("For purposes of this title, the term “x y” means a thing", "this title"),
        ("When used in this title, the term “x y” means a thing", "this title"),
        ("For purposes of paragraph (2), the term “x y” means a thing", "paragraph (2)"),
        ("The term “x y” means a thing", None),
    ],
)
def test_scope_is_captured(text: str, scope: str | None) -> None:
    ((_term, _body, found, _ref),) = defs.extract_definitions(text)
    assert found == scope


def test_definition_by_reference_is_recorded() -> None:
    text = "the term “widget” has the meaning given to such term by section 7701"
    ((_term, _body, _scope, reference),) = defs.extract_definitions(text)
    assert reference is not None
    assert "meaning given" in reference


def test_function_words_are_never_treated_as_terms() -> None:
    assert list(defs.extract_definitions("such property means a thing")) == []
    assert list(defs.extract_definitions("which income means a thing")) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Gross Income", "gross income"), ("  U.S.  person ", "u s person")],
)
def test_normalize_term(raw: str, expected: str) -> None:
    assert defs.normalize_term(raw) == expected


def test_gross_income_resolves_to_section_61(conn: sqlite3.Connection) -> None:
    found = defs.find(conn, "gross income")
    assert found
    assert found[0].provision_id == "/us/usc/t26/s61/a"


def test_united_states_person_resolves_to_7701(conn: sqlite3.Connection) -> None:
    found = defs.find(conn, "United States person")
    assert found
    assert found[0].provision_id.startswith("/us/usc/t26/s7701/a/30")


def test_definitions_carry_a_display_citation(conn: sqlite3.Connection) -> None:
    (definition, *_rest) = defs.find(conn, "gross income")
    assert definition.display == "I.R.C. § 61(a)"


def test_lookup_is_case_insensitive(conn: sqlite3.Connection) -> None:
    assert defs.find(conn, "GROSS INCOME")[0].provision_id == "/us/usc/t26/s61/a"


def test_unknown_term_returns_nothing(conn: sqlite3.Connection) -> None:
    assert defs.find(conn, "zzzznotawordzzzz") == []


def test_limit_is_respected(conn: sqlite3.Connection) -> None:
    assert len(defs.find(conn, "trade or business", limit=2)) <= 2


def test_scope_is_inherited_from_an_ancestor(conn: sqlite3.Connection) -> None:
    """§ 7701(a) says "When used in this title"; its paragraphs inherit that."""
    scopes = defs.scope_map(conn)
    assert scopes.get("/us/usc/t26/s7701/a/30") == "this title"
