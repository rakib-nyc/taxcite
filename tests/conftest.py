"""Shared fixtures: a session-scoped index built from the real USLM excerpts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from taxcite.config import DATA_DIR_ENV
from taxcite.errors import OfflineError, SourceUnavailableError
from taxcite.graph import definitions, xrefs
from taxcite.index import db
from taxcite.index.builder import index_sections
from taxcite.sources.ecfr import ParsedRegSection, credit_rows, parse_sections
from taxcite.sources.http import HttpClient, network_is_sealed
from taxcite.sources.irb import GuidanceDocument, index_documents, parse_bulletin
from taxcite.sources.uscode import ParsedSection, iter_sections, ordinal_of

FIXTURES = Path(__file__).parent / "fixtures"
USLM_DIR = FIXTURES / "uslm"
#: The same sections at an earlier release point, for the time-travel tests.
USLM_HISTORICAL_DIR = FIXTURES / "uslm_115-35"
#: One section with its statutory notes intact, for the effective-date tests. The
#: other USLM fixtures have notes stripped, which keeps them small and keeps the
#: text-extraction tests honest about what is and is not statutory text.
USLM_NOTES_DIR = FIXTURES / "uslm_notes"
HISTORICAL_RELEASE_POINT = "115-35"
ECFR_DIR = FIXTURES / "ecfr"
MEMO_DIR = FIXTURES / "memos"
IRB_DIR = FIXTURES / "irb"

#: The release point the `uslm/` fixtures were taken from (see fixtures/SOURCES.md).
FIXTURE_RELEASE_POINT = "119-110"

#: The eCFR issue date the `ecfr/` fixtures were taken from.
FIXTURE_ECFR_DATE = "2026-09-08"


def parsed_fixture_sections(*, include_synthetic: bool = False) -> Iterator[ParsedSection]:
    """Yield every fixture section, skipping the synthetic ones by default."""
    for directory in (USLM_DIR, USLM_NOTES_DIR):
        for path in sorted(directory.glob("*.xml")):
            if path.name.startswith("synthetic_") and not include_synthetic:
                continue
            yield from iter_sections(path, FIXTURE_RELEASE_POINT)


def parsed_fixture_regs(*, include_synthetic: bool = False) -> Iterator[ParsedRegSection]:
    """Yield every eCFR fixture section, skipping the synthetic ones by default."""
    for path in sorted(ECFR_DIR.glob("*.xml")):
        if path.name.startswith("synthetic_") and not include_synthetic:
            continue
        yield from parse_sections(path.read_bytes(), FIXTURE_ECFR_DATE)


def parsed_historical_sections() -> Iterator[ParsedSection]:
    """Yield the pre-TCJA fixture sections."""
    for path in sorted(USLM_HISTORICAL_DIR.glob("*.xml")):
        yield from iter_sections(path, HISTORICAL_RELEASE_POINT)


@pytest.fixture(scope="session")
def historical_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build an index of the pre-TCJA fixture sections."""
    path = tmp_path_factory.mktemp("taxcite-historical") / "usc26@115-35.db"
    connection = db.connect(path)
    try:
        index_sections(connection, parsed_historical_sections())
        db.rebuild_fts(connection)
        db.set_meta(connection, "usc_release_point", HISTORICAL_RELEASE_POINT)
        db.set_meta(connection, "release_enacted", "2017-05-17")
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture
def historical_conn(historical_db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection to the pre-TCJA fixture index."""
    connection = db.connect(historical_db_path)
    try:
        yield connection
    finally:
        connection.close()


def parsed_fixture_guidance() -> list[GuidanceDocument]:
    """Parse every Internal Revenue Bulletin fixture."""
    out: list[GuidanceDocument] = []
    for path in sorted(IRB_DIR.glob("*.html")):
        out.extend(parse_bulletin(path.read_bytes(), path.stem, f"https://irs.gov/irb/{path.stem}"))
    return out


@pytest.fixture(scope="session")
def fixture_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the fixture index once per test session and return its path."""
    path = tmp_path_factory.mktemp("taxcite-index") / "taxcite.db"
    connection = db.connect(path)
    try:
        index_sections(connection, parsed_fixture_sections())
        for parsed in parsed_fixture_regs():
            db.insert_provisions(connection, parsed.provisions, ordinal_of(parsed.provisions))
            db.insert_notes(connection, credit_rows(parsed))
        index_documents(connection, parsed_fixture_guidance())
        connection.commit()
        db.rebuild_fts(connection)
        db.rebuild_guidance_fts(connection)
        xrefs.build_xrefs(connection)
        definitions.build_definitions(connection)
        db.set_meta(connection, "usc_release_point", FIXTURE_RELEASE_POINT)
        db.set_meta(connection, "ecfr_date", FIXTURE_ECFR_DATE)
        # Three individual bulletins, not whole years: the fixture index must not
        # claim coverage it does not have, or a real ruling from 2019 would be
        # reported as nonexistent.
        db.set_meta(connection, "irb_bulletins", "2019-45, 2024-30, 2026-01")
        db.set_meta(connection, "schema_version", db.SCHEMA_VERSION)
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture
def conn(fixture_db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection to the session fixture index."""
    connection = db.connect(fixture_db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``TAXCITE_DATA_DIR`` at a temporary directory for the duration of a test."""
    monkeypatch.setenv("TAXCITE_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run async MCP tests on asyncio only."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail any unmarked test that tries to reach the network.

    Blocking raises :class:`SourceUnavailableError`, the same thing a real outage
    raises, so the code under test takes its genuine unreachable-source path rather
    than an artificial one.
    """
    if request.node.get_closest_marker("network"):
        return

    # Point the data directory — and so the HTTP cache — at a scratch path. Blocking
    # requests is not enough on its own: a cached response from a developer's own
    # machine would be served without any request being made, and the test would pass
    # on their laptop and fail in CI.
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "taxcite-data"))

    def blocked(*_args: object, **_kwargs: object) -> None:
        # Defer to the product's own seal when a test has set it, so that
        # --offline's guarantee is exercised rather than shadowed by this guard.
        if network_is_sealed():
            raise OfflineError("offline mode is in force; refusing to make a request")
        raise SourceUnavailableError(
            "network access is disabled in the default test run; mark the test @pytest.mark.network"
        )

    monkeypatch.setattr(HttpClient, "_request", blocked)
    monkeypatch.setattr(HttpClient, "download", blocked)
