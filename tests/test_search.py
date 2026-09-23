"""Tests for full-text search and its ranking (SPEC 8, docs/architecture.md)."""

from __future__ import annotations

import sqlite3

import pytest

from taxcite.index import search as fts
from taxcite.models import SourceType


def _ids(hits: list[object]) -> list[str]:
    return [hit.provision_id for hit in hits]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ordinary and necessary", "/us/usc/t26/s162/a"),
        ("gross income means all income from whatever source derived", "/us/usc/t26/s61/a"),
        ("material participation", "/us/usc/t26/s469/h"),
    ],
)
def test_expected_provision_is_in_the_top_three(
    conn: sqlite3.Connection, query: str, expected: str
) -> None:
    hits = fts.search(conn, query, limit=3)
    assert expected in [hit.provision_id for hit in hits]


def test_results_are_sorted_by_descending_score(conn: sqlite3.Connection) -> None:
    hits = fts.search(conn, "trade or business", limit=10)
    assert hits
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)


def test_limit_is_respected(conn: sqlite3.Connection) -> None:
    assert len(fts.search(conn, "income", limit=4)) == 4


def test_hits_carry_a_display_citation_and_snippet(conn: sqlite3.Connection) -> None:
    (hit,) = fts.search(conn, "ordinary and necessary", limit=1)
    assert hit.display.startswith("I.R.C. § 162")
    assert "**" in hit.snippet
    assert hit.source is SourceType.IRC


def test_hits_fall_back_to_the_section_heading(conn: sqlite3.Connection) -> None:
    hits = fts.search(conn, "a citizen or resident of the United States", limit=5)
    deep = next(h for h in hits if h.provision_id.startswith("/us/usc/t26/s7701/a/30"))
    assert deep.heading == "Definitions"


def test_source_filter(conn: sqlite3.Connection) -> None:
    regs = fts.search(conn, "business expenses", source=SourceType.REG, limit=5)
    code = fts.search(conn, "business expenses", source=SourceType.IRC, limit=5)
    assert regs and all(hit.source is SourceType.REG for hit in regs)
    assert code and all(hit.source is SourceType.IRC for hit in code)


def test_empty_query_returns_nothing(conn: sqlite3.Connection) -> None:
    assert fts.search(conn, "   ", limit=5) == []
    assert fts.search(conn, "!!!", limit=5) == []


def test_no_match_returns_nothing(conn: sqlite3.Connection) -> None:
    assert fts.search(conn, "zzzznotawordzzzz", limit=5) == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ordinary and necessary", '"ordinary" "and" "necessary"'),
        ('a "quoted" phrase', '"a" "quoted" "phrase"'),
        ("section 162(a)", '"section" "162" "a"'),
        ("NEAR(a b)", '"NEAR" "a" "b"'),
        ("", ""),
    ],
)
def test_escape_query(query: str, expected: str) -> None:
    assert fts.escape_query(query) == expected


def test_fts5_operators_are_not_interpreted(conn: sqlite3.Connection) -> None:
    """A query full of FTS5 syntax must search for the words, not blow up."""
    assert fts.search(conn, 'income OR NOT "x" * (y)', limit=3) is not None


def test_rare_tokens_prefers_uncommon_words(conn: sqlite3.Connection) -> None:
    tokens = fts.rare_tokens(conn, "the ordinary and necessary expenses kickbacks", 3)
    assert "kickbacks" in tokens
    assert "the" not in tokens


def test_rare_tokens_handles_text_with_no_indexed_words(conn: sqlite3.Connection) -> None:
    assert fts.rare_tokens(conn, "zzzzq yyyyq", 4) == []


def test_indexing_own_text_avoids_duplicate_ancestor_hits(conn: sqlite3.Connection) -> None:
    """The chapeau phrase must match § 162(a), not § 162(a) and § 162 and § 162(a)(1)."""
    hits = fts.search(conn, "carrying on any trade or business, including", limit=10)
    matched = [h.provision_id for h in hits if h.provision_id.startswith("/us/usc/t26/s162")]
    assert matched == ["/us/usc/t26/s162/a"]
