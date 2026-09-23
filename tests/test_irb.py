"""Tests for Internal Revenue Bulletin indexing and guidance resolution (roadmap 1.3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taxcite.citations import parse_citation
from taxcite.errors import SourceParseError
from taxcite.index import db
from taxcite.models import Status
from taxcite.sources import irb
from taxcite.verify.resolver import Resolver

from conftest import IRB_DIR


def _parse(name: str) -> list[irb.GuidanceDocument]:
    path = IRB_DIR / f"{name}.html"
    return irb.parse_bulletin(path.read_bytes(), name, f"https://irs.gov/irb/{name}")


# --------------------------------------------------------------------------------------
# Parsing, across three markup generations
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bulletin", "expected"),
    [
        ("2019-45", "/us/irs/rev-rul/2019-25"),
        ("2024-30", "/us/irs/notice/2024-58"),
        ("2026-01", "/us/irs/rev-proc/2026-5"),
    ],
)
def test_every_markup_generation_parses(bulletin: str, expected: str) -> None:
    """The IRS markup is not stable across years; one code path has to read all of it."""
    assert expected in {doc.id for doc in _parse(bulletin)}


def test_documents_carry_their_text() -> None:
    (notice,) = [d for d in _parse("2024-30") if d.id == "/us/irs/notice/2024-58"]
    assert notice.kind == "Notice"
    assert notice.number == "2024-58"
    assert "applicable percentage" in notice.text
    assert notice.bulletin == "2024-30"


def test_a_treasury_decision_is_recognised() -> None:
    assert "/us/irs/td/9878" in {doc.id for doc in _parse("2019-45")}


def test_bulletin_furniture_is_skipped() -> None:
    titles = {doc.title.lower() for doc in _parse("2024-30")}
    assert "the irs mission" not in titles
    assert "introduction" not in titles


def test_relations_are_extracted_with_their_verb() -> None:
    (proc,) = [d for d in _parse("2026-01") if d.id == "/us/irs/rev-proc/2026-5"]
    assert ("/us/irs/rev-proc/2025-5", "supersedes") in proc.acts_on


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rev. Rul. 2015-12 is hereby superseded.", ("/us/irs/rev-rul/2015-12", "supersedes")),
        ("This notice modifies Notice 2020-35.", ("/us/irs/notice/2020-35", "modifies")),
        ("Rev. Proc. 2014-40 is obsoleted.", ("/us/irs/rev-proc/2014-40", "obsoletes")),
    ],
)
def test_relation_phrasings(text: str, expected: tuple[str, str]) -> None:
    assert expected in irb._relations(text)


def test_a_distant_mention_is_not_a_relation() -> None:
    """A long window turns every nearby ruling into a false "superseded"."""
    text = (
        "Notice 2020-35 addressed a different question entirely, and for a range of "
        "unrelated reasons that are set out at length in the discussion below, the "
        "position taken in an earlier document is hereby superseded."
    )
    assert ("/us/irs/notice/2020-35", "supersedes") not in irb._relations(text)


@pytest.mark.parametrize(
    ("kind", "number", "expected"),
    [
        ("Rev. Rul.", "2019-24", "/us/irs/rev-rul/2019-24"),
        ("Rev. Proc.", "2023-34", "/us/irs/rev-proc/2023-34"),
        ("Notice", "2024-7", "/us/irs/notice/2024-7"),
        ("T.D.", "9959", "/us/irs/td/9959"),
    ],
)
def test_canonical_ids(kind: str, number: str, expected: str) -> None:
    assert irb.canonical_id(kind, number) == expected


def test_bulletin_url() -> None:
    assert irb.bulletin_url(2024, 7) == "https://www.irs.gov/irb/2024-07_IRB"


def test_garbage_is_a_typed_error() -> None:
    with pytest.raises(SourceParseError):
        irb.parse_bulletin(b"", "x", "u")


def test_en_dash_numbering_is_normalised() -> None:
    """Older bulletins number with an en dash; the canonical form uses a hyphen."""
    citation = parse_citation("Rev. Proc. 2015–1")
    assert citation.guidance_number == "2015-1"


# --------------------------------------------------------------------------------------
# Indexing and resolution
# --------------------------------------------------------------------------------------


def test_guidance_is_indexed(conn: sqlite3.Connection) -> None:
    assert db.count_guidance(conn) >= 7
    row = db.get_guidance(conn, "/us/irs/notice/2024-58")
    assert row is not None
    assert row["kind"] == "Notice"


def test_an_indexed_ruling_verifies(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("Rev. Rul. 2019-25"))
    assert result.status is Status.VERIFIED
    assert result.source_url is not None
    assert "irs.gov" in result.source_url


def test_a_superseded_document_is_flagged_even_when_not_indexed(
    conn: sqlite3.Connection,
) -> None:
    """Rev. Proc. 2025-5 is not in the fixtures; 2026-5 says it superseded it."""
    result = Resolver(conn).resolve(parse_citation("Rev. Proc. 2025-5"))
    assert result.status is Status.SUPERSEDED
    assert result.suggestion is not None
    assert "2026-5" in result.suggestion


def test_an_unindexed_year_stays_unverifiable(conn: sqlite3.Connection) -> None:
    """Indexing one week of 2019 says nothing about a different 2019 ruling."""
    result = Resolver(conn).resolve(parse_citation("Rev. Rul. 2019-24"))
    assert result.status is Status.UNVERIFIABLE


def test_a_fully_indexed_year_can_report_not_found(conn: sqlite3.Connection) -> None:
    db.set_meta(conn, "irb_years", "2024")
    try:
        result = Resolver(conn).resolve(parse_citation("Rev. Rul. 2024-99"))
        assert result.status is Status.NOT_FOUND
    finally:
        conn.execute("DELETE FROM meta WHERE key = 'irb_years'")
        conn.commit()


def test_coverage_parsing() -> None:
    from taxcite.verify.resolver import _covered_years

    class FakeConn:
        def execute(self, _sql: str, _params: object = ()) -> object:
            raise AssertionError("should not query")

    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(":memory:")
    connection.row_factory = _sqlite3.Row
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES ('irb_years', '2019,2023-2025')")
    assert _covered_years(connection) == {2019, 2023, 2024, 2025}
    connection.close()
    del FakeConn


def test_no_guidance_index_means_no_guidance_verdicts(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "bare.db")
    try:
        result = Resolver(connection).resolve(parse_citation("Rev. Rul. 2019-24"))
        assert result.status is Status.UNVERIFIABLE
    finally:
        connection.close()


def test_indexing_is_idempotent(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "g.db")
    try:
        documents = _parse("2024-30")
        irb.index_documents(connection, documents)
        first = db.count_guidance(connection)
        irb.index_documents(connection, documents)
        assert db.count_guidance(connection) == first
    finally:
        connection.close()


def test_guidance_appears_in_source_versions(conn: sqlite3.Connection) -> None:
    db.set_meta(conn, "irb_years", "2024")
    try:
        assert db.source_versions(conn)["irb"] == "2024"
    finally:
        conn.execute("DELETE FROM meta WHERE key = 'irb_years'")
        conn.commit()


def test_fixtures_are_documented() -> None:
    sources = (IRB_DIR.parent / "SOURCES.md").read_text(encoding="utf-8")
    assert "irs.gov/irb" in sources
    assert "not* stable" in sources or "not stable" in sources
