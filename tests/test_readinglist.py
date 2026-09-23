"""Reading lists, and the defined terms that make a regulation readable.

The consolidated return regulations are the case this was built for. A sentence of
Treas. Reg. § 1.1502-21 is nearly all terms of art — "member", "group", "SRLY" — each
defined somewhere else, and reading it without them is reading it wrong.
"""

from __future__ import annotations

import sqlite3

import pytest

from taxcite.graph import definitions, readinglist

# --------------------------------------------------------------------------------------
# Extracting definitions the regulations write without quotation marks
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The term member means a corporation included in the group.", ["member"]),
        (
            "the term separate return limitation year (or SRLY) means any separate "
            "return year of a member.",
            ["separate return limitation year", "SRLY"],
        ),
        (
            "The term controlled foreign corporation (CFC) means a foreign corporation.",
            ["controlled foreign corporation", "CFC"],
        ),
        ("The term “consolidated group” means a group filing returns.", ["consolidated group"]),
        (
            "For purposes of this subtitle, the term “corporation” includes associations.",
            ["corporation"],
        ),
    ],
)
def test_terms_the_regulations_define_without_quotes(text: str, expected: list[str]) -> None:
    """The eCFR italicises defined terms, and italics do not survive text extraction."""
    assert [found[0] for found in definitions.extract_definitions(text)] == expected


def test_an_abbreviation_is_indexed_alongside_its_term() -> None:
    """The regulations define SRLY once and then write SRLY everywhere."""
    found = {
        name: body
        for name, body, _scope, _ref in definitions.extract_definitions(
            "the term separate return limitation year (or SRLY) means a separate return year."
        )
    }
    assert "SRLY" in found
    assert "separate return limitation year" in found


@pytest.mark.parametrize(
    "text",
    [
        "Such property means nothing in particular.",
        "The amount means the sum of the items.",
        "Where the of means something.",
    ],
)
def test_phrases_that_are_not_definitions(text: str) -> None:
    assert list(definitions.extract_definitions(text)) == []


def test_a_long_parenthetical_is_not_an_abbreviation() -> None:
    """A long parenthetical is an aside, not a short name for the term."""
    text = (
        "The term basis (as adjusted under the rules of this section and in the manner "
        "described) means cost."
    )
    names = [found[0] for found in definitions.extract_definitions(text)]
    assert not any(len(name.split()) > 3 for name in names)


# --------------------------------------------------------------------------------------
# Assembling a reading list
# --------------------------------------------------------------------------------------


def test_a_reading_list_names_the_cross_references(conn: sqlite3.Connection) -> None:
    reading = readinglist.build(conn, "/us/usc/t26/s199A/c", depth=1, limit=10)
    assert reading is not None
    assert reading.display.startswith("I.R.C. § 199A")


def test_an_unindexed_provision_has_no_reading_list(conn: sqlite3.Connection) -> None:
    assert readinglist.build(conn, "/us/usc/t26/s9999/z") is None


def test_the_provision_does_not_list_its_own_definitions(conn: sqlite3.Connection) -> None:
    """A section that defines a term is not borrowing someone else's."""
    reading = readinglist.build(conn, "/us/usc/t26/s199A", depth=1, limit=5)
    assert reading is not None
    for entry in reading.terms:
        for definition in entry.definitions:
            assert definition.provision_id != reading.provision.id


def test_common_words_are_not_reported_as_terms_of_art(conn: sqlite3.Connection) -> None:
    reading = readinglist.build(conn, "/us/usc/t26/s199A", depth=1, limit=5)
    assert reading is not None
    reported = {entry.term.lower() for entry in reading.terms}
    assert not (reported & readinglist.COMMON_TERMS)


def test_the_term_cap_is_respected(conn: sqlite3.Connection) -> None:
    reading = readinglist.build(conn, "/us/usc/t26/s199A", depth=1, limit=5, max_terms=3)
    assert reading is not None
    assert len(reading.terms) <= 3


def test_the_markdown_says_what_it_is_not(conn: sqlite3.Connection) -> None:
    reading = readinglist.build(conn, "/us/usc/t26/s199A/c", depth=1, limit=5)
    assert reading is not None
    rendered = readinglist.to_markdown(reading)
    assert "does not tell you what the provisions say" in rendered


# --------------------------------------------------------------------------------------
# Relevance: proximity beats string length
# --------------------------------------------------------------------------------------


def test_short_terms_match_on_word_boundaries() -> None:
    """A bare substring test finds "cfc" inside unrelated words."""
    assert readinglist._uses_term("the srly limitation applies", "srly")
    assert not readinglist._uses_term("describes the srlyness of it", "srly")
    assert readinglist._uses_term("a controlled foreign corporation", "controlled foreign")


def test_regulation_families_are_recognised() -> None:
    """§ 1.1502-21 and § 1.1502-1 are one body of rules; § 1.162-1 is not."""
    assert readinglist._family("/us/cfr/t26/s1.1502-21/c") == "1.1502"
    assert readinglist._family("/us/cfr/t26/s1.1502-1/b") == "1.1502"
    assert readinglist._family("/us/cfr/t26/s1.162-1/a") == "1.162"


def test_a_tax_year_flags_a_provision_that_did_not_govern_it(conn: sqlite3.Connection) -> None:
    reading = readinglist.build(conn, "/us/usc/t26/s199A", depth=1, limit=3, tax_year=2017)
    assert reading is not None
    assert any("did not govern tax year 2017" in flag.lower() for flag in reading.flags)


def test_no_tax_year_means_no_timing_flag(conn: sqlite3.Connection) -> None:
    reading = readinglist.build(conn, "/us/usc/t26/s199A", depth=1, limit=3)
    assert reading is not None
    assert not any("did not govern" in flag for flag in reading.flags)
