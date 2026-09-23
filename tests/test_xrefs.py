"""Tests for the cross-reference graph (SPEC 6.8)."""

from __future__ import annotations

import sqlite3

import pytest

from taxcite.graph import xrefs
from taxcite.models import SourceType


def test_incoming_does_not_match_a_longer_section_number(conn: sqlite3.Connection) -> None:
    """§ 61 must not pick up references to §§ 611, 613, or 6166."""
    edges = xrefs.incoming(conn, "/us/usc/t26/s61")
    assert all(edge.to_id == "/us/usc/t26/s61" for edge in edges)


def test_every_ref_kind_is_one_of_the_three(conn: sqlite3.Connection) -> None:
    kinds = {str(row["kind"]) for row in conn.execute("SELECT DISTINCT kind FROM refs")}
    assert kinds <= {"explicit", "implicit", "relative"}


def test_implicit_refs_are_extracted_from_prose(conn: sqlite3.Connection) -> None:
    edges = xrefs.outgoing(conn, "/us/usc/t26/s1411")
    targets = {edge.to_id for edge in edges}
    assert "/us/usc/t26/s469" in targets
    assert all(edge.kind in {"implicit", "explicit", "relative"} for edge in edges)


def test_outgoing_skips_the_provisions_own_section_by_default(
    conn: sqlite3.Connection,
) -> None:
    default = {e.to_id for e in xrefs.outgoing(conn, "/us/usc/t26/s1411")}
    assert not any(t.startswith("/us/usc/t26/s1411") for t in default)
    internal = {e.to_id for e in xrefs.outgoing(conn, "/us/usc/t26/s1411", internal=True)}
    assert any(t.startswith("/us/usc/t26/s1411/") for t in internal)


def test_relative_references_resolve_against_ancestors(conn: sqlite3.Connection) -> None:
    edges = xrefs.outgoing(conn, "/us/usc/t26/s1411", internal=True)
    relative = {(e.from_id, e.to_id) for e in edges if e.kind == "relative"}
    assert ("/us/usc/t26/s1411", "/us/usc/t26/s1411/e") in {
        ("/us/usc/t26/s1411", t) for _f, t in relative
    }


def test_incoming_finds_the_citing_provisions(conn: sqlite3.Connection) -> None:
    edges = xrefs.incoming(conn, "/us/usc/t26/s263A")
    assert edges
    assert all(edge.to_id == "/us/usc/t26/s263A" for edge in edges)
    assert any(edge.from_id.startswith("/us/usc/t26/s") for edge in edges)


def test_edges_carry_a_display_citation(conn: sqlite3.Connection) -> None:
    edges = xrefs.outgoing(conn, "/us/usc/t26/s1411")
    assert all(edge.display for edge in edges if not edge.external)


def test_a_provision_never_cites_itself(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT COUNT(*) AS n FROM refs WHERE from_id = to_id").fetchone()
    assert int(rows["n"]) == 0


def test_the_act_reference_guard_keeps_false_edges_out(conn: sqlite3.Connection) -> None:
    """A reference to ACA section 1411 is not a reference to I.R.C. § 1411."""
    edges = xrefs.incoming(conn, "/us/usc/t26/s1411")
    assert not any("4980H" in edge.from_id for edge in edges)


def test_authority_counts(conn: sqlite3.Connection) -> None:
    counts = xrefs.authority_counts(conn)
    assert counts
    assert all(isinstance(value, int) for value in counts.values())


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        (["a"], "as provided in this section", "/us/usc/t26/s162"),
        (["a"], "under this subsection", "/us/usc/t26/s162/a"),
        (["a"], "described in subsection (b)", "/us/usc/t26/s162/b"),
        (["a", "1"], "described in paragraph (2)", "/us/usc/t26/s162/a/2"),
        (["a", "1"], "under this paragraph", "/us/usc/t26/s162/a/1"),
    ],
)
def test_relative_targets(path: list[str], text: str, expected: str) -> None:
    targets = xrefs.relative_targets(SourceType.IRC, "162", path, text)
    assert expected in [target for target, _raw in targets]


def test_relative_targets_in_a_regulation_shift_one_level() -> None:
    targets = xrefs.relative_targets(SourceType.REG, "1.162-1", ["a"], "under paragraph (b)")
    assert "/us/cfr/t26/s1.162-1/b" in [target for target, _raw in targets]


def test_relative_targets_ignores_what_it_cannot_resolve() -> None:
    assert xrefs.relative_targets(SourceType.IRC, "162", [], "under this clause") == []
