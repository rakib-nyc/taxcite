"""The I.R.C. § 381(c) attribute carryover checklist.

§ 381(c) is a closed enumerated list, and the checklist is *derived* from the indexed
Code rather than typed into this project — which is the property these tests are
really about. If the enumeration were curated here it would drift from the statute,
and three of its items are already repealed.

The index built for these tests is synthetic: the structure mirrors § 381 exactly
(subsection (a) conditions, (b) operating rules, (c) enumerated items, some repealed)
but the attribute names are obviously invented, because what is under test is the
derivation and not the content of the Code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taxcite.index import db
from taxcite.model import carryover
from taxcite.models import Provision, SourceType


def _provision(path: list[str], heading: str | None, text: str, level: str) -> Provision:
    return Provision(
        id="/us/usc/t26/s381" + "".join(f"/{p}" for p in path),
        source=SourceType.IRC,
        section="381",
        path=path,
        level=level,
        num=f"({path[-1]})" if path else "381",
        heading=heading,
        text=text,
        full_text=text,
        parent_id=("/us/usc/t26/s381" + "".join(f"/{p}" for p in path[:-1]) if path else None),
        source_version="synthetic",
    )


@pytest.fixture
def indexed(tmp_path: Path) -> sqlite3.Connection:
    """A synthetic § 381 with the same shape as the real one."""
    connection = db.connect(tmp_path / "carryover.db")
    provisions = [
        _provision([], "Carryovers in certain corporate acquisitions", "", "section"),
        _provision(
            ["a"], "General rule", "In the case of the acquisition of assets—", "subsection"
        ),
        _provision(
            ["a", "1"],
            None,
            "in a lorem distribution to which section 332 applies; or",
            "paragraph",
        ),
        _provision(
            ["a", "2"], None, "in a lorem transfer to which section 361 applies,", "paragraph"
        ),
        _provision(
            ["b"], "Operating rules", "Except in the case of a lorem acquisition—", "subsection"
        ),
        _provision(["c"], "Items of distributor or transferor corporation", "", "subsection"),
        _provision(["c", "1"], "Net operating loss carryovers", "Lorem.", "paragraph"),
        _provision(["c", "2"], "Lorem earnings account", "Lorem.", "paragraph"),
        _provision(["c", "3"], "Capital loss carryover", "Lorem.", "paragraph"),
        _provision(["c", "4"], "Repealed. Pub. L. 99-999, § 1, Jan. 1, 1986", "", "paragraph"),
        _provision(["c", "5"], "Credit under section 38", "Lorem.", "paragraph"),
        _provision(
            ["c", "6"], "Carryforward of disallowed business interest", "Lorem.", "paragraph"
        ),
        _provision(["c", "7"], "Repealed. Pub. L. 100-100, § 2, Jan. 1, 1990", "", "paragraph"),
        # A deeper subdivision, which is not itself an enumerated item.
        _provision(["c", "1", "A"], "Lorem sub-item", "Lorem.", "subparagraph"),
    ]
    db.insert_provisions(connection, provisions, {p.id: i for i, p in enumerate(provisions)})
    connection.commit()
    return connection


# --------------------------------------------------------------------------------------
# The enumeration is derived, not curated
# --------------------------------------------------------------------------------------


def test_the_items_come_from_the_statute(indexed: sqlite3.Connection) -> None:
    checklist = carryover.build(indexed)
    assert checklist is not None
    assert [item.number for item in checklist.items] == ["(1)", "(2)", "(3)", "(5)", "(6)"]


def test_repealed_items_are_separated(indexed: sqlite3.Connection) -> None:
    """A repealed paragraph carries nothing, and the Code says so in its heading."""
    checklist = carryover.build(indexed)
    assert checklist is not None
    assert [item.number for item in checklist.repealed] == ["(4)", "(7)"]
    assert all(item.repealed for item in checklist.repealed)
    assert not any(item.repealed for item in checklist.items)


def test_deeper_subdivisions_are_not_items(indexed: sqlite3.Connection) -> None:
    """§ 381(c)(1)(A) is part of item (1), not a twenty-seventh attribute."""
    checklist = carryover.build(indexed)
    assert checklist is not None
    assert all(len(item.number) <= 4 for item in checklist.items)
    assert "Lorem sub-item" not in {item.heading for item in checklist.items}


def test_the_live_count_excludes_repeals(indexed: sqlite3.Connection) -> None:
    checklist = carryover.build(indexed)
    assert checklist is not None
    assert checklist.live == 5


def test_an_unindexed_code_has_no_checklist(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "empty.db")
    assert carryover.build(connection) is None


# --------------------------------------------------------------------------------------
# Conditions and limitations
# --------------------------------------------------------------------------------------


def test_the_qualifying_conditions_are_quoted_from_the_statute(
    indexed: sqlite3.Connection,
) -> None:
    checklist = carryover.build(indexed)
    assert checklist is not None
    assert len(checklist.qualifying) == 2
    assert any("section 332" in condition for condition in checklist.qualifying)
    assert any("section 361" in condition for condition in checklist.qualifying)


def test_limitations_are_matched_to_the_attributes_they_bear_on(
    indexed: sqlite3.Connection,
) -> None:
    checklist = carryover.build(indexed)
    assert checklist is not None
    by_citation = {lim.citation: lim for lim in checklist.limitations}
    numbers = {item.number for item in by_citation["I.R.C. § 382"].items}
    assert numbers == {"(1)", "(3)", "(6)"}  # NOL, capital loss, business interest


def test_credits_are_matched_to_section_383(indexed: sqlite3.Connection) -> None:
    checklist = carryover.build(indexed)
    assert checklist is not None
    by_citation = {lim.citation: lim for lim in checklist.limitations}
    numbers = {item.number for item in by_citation["I.R.C. § 383"].items}
    assert "(5)" in numbers  # credit under section 38


def test_a_general_limitation_bears_on_everything(indexed: sqlite3.Connection) -> None:
    """§ 269 is not keyed to a particular attribute."""
    checklist = carryover.build(indexed)
    assert checklist is not None
    by_citation = {lim.citation: lim for lim in checklist.limitations}
    assert by_citation["I.R.C. § 269"].items == []


# --------------------------------------------------------------------------------------
# What it refuses to decide
# --------------------------------------------------------------------------------------


def test_the_checklist_does_not_claim_the_transaction_qualifies(
    indexed: sqlite3.Connection,
) -> None:
    checklist = carryover.build(indexed)
    assert checklist is not None
    rendered = carryover.to_markdown(checklist)
    assert "question of characterisation" in rendered
    assert "This does not answer it." in rendered


def test_limitations_are_signposts_not_findings(indexed: sqlite3.Connection) -> None:
    checklist = carryover.build(indexed)
    assert checklist is not None
    rendered = carryover.to_markdown(checklist)
    assert "not findings that a limitation applies" in rendered
    assert "taxcite owner-shift" in rendered


def test_the_list_is_described_as_closed(indexed: sqlite3.Connection) -> None:
    """That is the structural fact that makes the checklist worth anything."""
    checklist = carryover.build(indexed)
    assert checklist is not None
    assert "The list is closed" in carryover.to_markdown(checklist)
