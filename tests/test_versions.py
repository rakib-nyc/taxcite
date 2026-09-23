"""Tests for historical versions and provision comparison (roadmap 1.1)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from taxcite.errors import ConfigurationError
from taxcite.index import compare, db, versions
from taxcite.sources.uscode import ReleasePoint, release_point_for

from conftest import HISTORICAL_RELEASE_POINT

POINTS = [
    ReleasePoint("119-110", "https://example.invalid/current.zip", date(2026, 9, 16)),
    ReleasePoint("118-42", "https://example.invalid/a.zip", date(2024, 3, 9), ("26",)),
    ReleasePoint("117-328", "https://example.invalid/b.zip", date(2022, 12, 29), ("26",)),
    ReleasePoint("115-97", "https://example.invalid/c.zip", date(2017, 12, 22), ("26",)),
]


# --------------------------------------------------------------------------------------
# Resolving a date to a release point
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (date(2024, 6, 1), "118-42"),
        (date(2024, 3, 9), "118-42"),
        (date(2024, 3, 8), "117-328"),
        (date(2023, 1, 1), "117-328"),
        (date(2018, 1, 1), "115-97"),
    ],
)
def test_release_point_for_a_date(as_of: date, expected: str) -> None:
    point = release_point_for(POINTS, as_of)
    assert point is not None
    assert point.release == expected


def test_a_date_after_every_release_point_is_current_law() -> None:
    point = release_point_for(POINTS, date(2030, 1, 1))
    assert point is not None
    assert point.release == "119-110"


def test_a_date_before_every_release_point_falls_back_to_the_undated_current() -> None:
    undated = [ReleasePoint("119-110", "u"), *POINTS[1:]]
    point = release_point_for(undated, date(1990, 1, 1))
    assert point is not None
    assert point.release == "119-110"


def test_no_release_points_resolves_to_nothing() -> None:
    assert release_point_for([], date(2024, 1, 1)) is None


def test_a_release_point_with_no_title_list_is_assumed_to_touch_title_26() -> None:
    """Omitting the list means a general revision; skipping it would lose changes."""
    assert ReleasePoint("119-1", "u").affects_title_26
    assert ReleasePoint("119-1", "u", titles=("26", "31")).affects_title_26
    assert not ReleasePoint("119-1", "u", titles=("18", "28")).affects_title_26


# --------------------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------------------


@pytest.fixture
def catalogued(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "main.db")
    db.set_meta(connection, "usc_release_point", "119-110")
    with db.transaction(connection):
        connection.executemany(
            "INSERT INTO release_points(release, enacted, titles, url) VALUES(?,?,?,?)",
            [
                (p.release, p.enacted.isoformat() if p.enacted else None, "[]", p.url)
                for p in POINTS
            ],
        )
    return connection


def test_catalogue_is_newest_first(catalogued: sqlite3.Connection) -> None:
    releases = [point.release for point in versions.catalogue(catalogued)]
    assert releases[0] == "119-110"
    assert releases[1:] == ["118-42", "117-328", "115-97"]


def test_resolve_uses_the_catalogue(catalogued: sqlite3.Connection) -> None:
    assert versions.resolve(catalogued, date(2023, 6, 1)).release == "117-328"


def test_resolve_without_a_catalogue_is_a_friendly_error(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "empty.db")
    with pytest.raises(ConfigurationError, match="no release points are known"):
        versions.resolve(connection, date(2023, 1, 1))
    connection.close()


def test_current_release(catalogued: sqlite3.Connection) -> None:
    assert versions.current_release(catalogued) == "119-110"


def test_open_as_of_returns_the_main_index_for_the_current_release(
    catalogued: sqlite3.Connection,
) -> None:
    catalogued.execute(
        "UPDATE release_points SET enacted = ? WHERE release = ?",
        ("2026-09-16", "119-110"),
    )
    opened, point = versions.open_as_of(date(2030, 1, 1), main=catalogued)
    assert opened is catalogued
    assert point.release == "119-110"


def test_open_as_of_refuses_to_build_silently(catalogued: sqlite3.Connection) -> None:
    with pytest.raises(ConfigurationError, match="not indexed locally"):
        versions.open_as_of(date(2023, 1, 1), main=catalogued)


def test_version_db_paths_are_per_release(data_dir: Path) -> None:
    assert versions.version_db_path("118-42").name == "usc26@118-42.db"
    assert versions.version_db_path("118-42").parent == data_dir / "versions"


def test_disk_usage_and_removal(data_dir: Path) -> None:
    path = versions.version_db_path("118-42")
    path.write_bytes(b"x" * 1024)
    assert versions.disk_usage() == {"118-42": 1024}
    assert versions.remove_version("118-42")
    assert not versions.remove_version("118-42")
    assert versions.disk_usage() == {}


# --------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------


def test_the_historical_fixture_is_the_earlier_release(
    historical_conn: sqlite3.Connection,
) -> None:
    assert db.get_meta(historical_conn, "usc_release_point") == HISTORICAL_RELEASE_POINT


def test_tcja_renamed_section_163j(
    historical_conn: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    result = compare.compare_provision(historical_conn, conn, "/us/usc/t26/s163/j")
    assert not result.unchanged
    renamed = next(c for c in result.changes if c.provision_id == "/us/usc/t26/s163/j")
    assert renamed.before_heading == "Limitation on deduction for interest on certain indebtedness"
    assert renamed.after_heading == "Limitation on business interest"


def test_tcja_added_paragraphs_to_section_163j(
    historical_conn: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    result = compare.compare_provision(historical_conn, conn, "/us/usc/t26/s163/j")
    added = {c.provision_id for c in result.changes if c.kind == "added"}
    assert "/us/usc/t26/s163/j/10" in added


def test_a_provision_that_did_not_exist_before(
    historical_conn: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """§ 199A was created by the TCJA; it is absent from the 115-35 fixture."""
    result = compare.compare_provision(historical_conn, conn, "/us/usc/t26/s199A")
    assert not result.existed_before
    assert result.exists_after
    assert "Did not exist" in compare.to_markdown(result)


def test_a_provision_that_no_longer_exists(
    historical_conn: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """§ 199 was repealed by the TCJA and is not in the current fixture set."""
    result = compare.compare_provision(historical_conn, conn, "/us/usc/t26/s199")
    assert result.existed_before
    assert not result.exists_after
    assert "No longer exists" in compare.to_markdown(result)


def test_an_unchanged_provision_reports_no_change(
    historical_conn: sqlite3.Connection,
) -> None:
    result = compare.compare_provision(historical_conn, historical_conn, "/us/usc/t26/s163/j")
    assert result.unchanged
    assert "No change." in compare.to_markdown(result)


def test_markdown_names_both_release_points(
    historical_conn: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    result = compare.compare_provision(
        historical_conn, conn, "/us/usc/t26/s163/j", citation="I.R.C. § 163(j)"
    )
    rendered = compare.to_markdown(result)
    assert "I.R.C. § 163(j): 115-35 → 119-110" in rendered
    assert "~~" in rendered and "**" in rendered


def test_corpus_comparison_lists_added_and_removed_sections(
    historical_conn: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    added, removed = compare.compare_corpora(historical_conn, conn)
    assert "199A" in added
    assert "199" in removed


def test_word_diff_of_identical_text_is_none() -> None:
    assert compare._word_diff("same words", "same words") is None


def test_word_diff_elides_long_unchanged_runs() -> None:
    before = " ".join(f"w{i}" for i in range(60)) + " old"
    after = " ".join(f"w{i}" for i in range(60)) + " new"
    diff = compare._word_diff(before, after)
    assert diff is not None
    assert "…" in diff
    assert "~~old~~" in diff
    assert "**new**" in diff
