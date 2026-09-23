"""Tests for the SQLite index: schema, inserts, and the build pipeline (SPEC 6.5, 7)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taxcite.errors import IndexNotBuiltError
from taxcite.index import db
from taxcite.index.builder import index_sections
from taxcite.models import SourceType

from conftest import FIXTURE_RELEASE_POINT, parsed_fixture_sections


def test_schema_creates_every_table(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "empty.db")
    names = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"meta", "provisions", "refs", "definitions"} <= names
    assert {"provisions_fts", "provisions_vocab", "definitions_fts"} <= names
    connection.close()


def test_connect_read_only_requires_an_existing_file(tmp_path: Path) -> None:
    with pytest.raises(IndexNotBuiltError):
        db.connect(tmp_path / "missing.db", read_only=True)


def test_require_index_rejects_an_empty_index(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "empty.db")
    with pytest.raises(IndexNotBuiltError, match="Index not built"):
        db.require_index(connection)
    connection.close()


def test_fixture_index_has_every_fixture_section(conn: sqlite3.Connection) -> None:
    sections = set(db.section_numbers(conn, SourceType.IRC))
    assert {"1", "61", "162", "163", "199A", "263", "263A", "469", "1411", "7701"} <= sections
    assert "1400Z-2" in sections


def test_deep_pinpoint_row_exists(conn: sqlite3.Connection) -> None:
    provision = db.get_provision(conn, "/us/usc/t26/s7701/a/30/A")
    assert provision is not None
    assert provision.text == "a citizen or resident of the United States,"
    assert provision.path == ["a", "30", "A"]
    assert provision.source is SourceType.IRC
    assert provision.source_version == FIXTURE_RELEASE_POINT


def test_repealed_status_survives_the_round_trip(conn: sqlite3.Connection) -> None:
    provision = db.get_provision(conn, "/us/usc/t26/s4")
    assert provision is not None
    assert provision.status == "repealed"


def test_reserved_status_survives_the_round_trip(conn: sqlite3.Connection) -> None:
    provision = db.get_provision(conn, "/us/usc/t26/s1000")
    assert provision is not None
    assert provision.status == "reserved"


def test_children_are_returned_in_document_order(conn: sqlite3.Connection) -> None:
    children = db.get_children(conn, "/us/usc/t26/s162/a")
    assert [child.num for child in children] == ["(1)", "(2)", "(3)"]


def test_descendants_are_depth_first(conn: sqlite3.Connection) -> None:
    ids = [p.id for p in db.get_descendants(conn, "/us/usc/t26/s1411/a")]
    assert ids[:4] == [
        "/us/usc/t26/s1411/a/1",
        "/us/usc/t26/s1411/a/1/A",
        "/us/usc/t26/s1411/a/1/B",
        "/us/usc/t26/s1411/a/1/B/i",
    ]


def test_missing_provision_returns_none(conn: sqlite3.Connection) -> None:
    assert db.get_provision(conn, "/us/usc/t26/s162/z") is None


def test_meta_round_trip(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "meta.db")
    db.set_meta(connection, "usc_release_point", "119-110")
    db.set_meta(connection, "usc_release_point", "119-111")
    assert db.get_meta(connection, "usc_release_point") == "119-111"
    assert db.get_meta(connection, "nothing") is None
    assert db.get_meta(connection, "nothing", "fallback") == "fallback"
    assert db.all_meta(connection) == {"usc_release_point": "119-111"}
    assert db.source_versions(connection) == {"irc": "119-111"}
    connection.close()


def test_indexing_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"
    connection = db.connect(path)
    first = index_sections(connection, parsed_fixture_sections())
    count_after_first = db.count_provisions(connection)
    second = index_sections(connection, parsed_fixture_sections())
    assert first == second
    assert db.count_provisions(connection) == count_after_first
    connection.close()


def test_batching_writes_everything(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "batched.db")
    sections, provisions, _refs = index_sections(
        connection, parsed_fixture_sections(), batch_size=2
    )
    assert sections > 0
    assert db.count_provisions(connection) == provisions
    connection.close()


def test_progress_callback_is_called(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "progress.db")
    seen: list[tuple[str, int]] = []
    index_sections(
        connection,
        parsed_fixture_sections(),
        batch_size=2,
        progress=lambda stage, count: seen.append((stage, count)),
    )
    assert seen
    assert seen[-1][0] == "sections"
    connection.close()


def test_clear_source_empties_the_index(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "clear.db")
    index_sections(connection, parsed_fixture_sections())
    assert db.count_provisions(connection, SourceType.IRC) > 0
    db.clear_source(connection, SourceType.IRC)
    assert db.count_provisions(connection, SourceType.IRC) == 0
    connection.close()


def test_transaction_rolls_back_on_failure(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "rollback.db")
    with pytest.raises(RuntimeError), db.transaction(connection):
        db.set_meta(connection, "key", "value")
        raise RuntimeError("boom")
    assert db.get_meta(connection, "key") is None
    connection.close()
