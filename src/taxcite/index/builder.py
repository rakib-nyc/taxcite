"""The ``build-index`` pipeline (SPEC 6.5).

Downloads Title 26, streams it into the SQLite index in batches, then runs the
post-passes that need the whole corpus in place: implicit cross-references and defined
terms. Treasury Regulation parts are fetched from the eCFR when asked for.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from taxcite import __version__
from taxcite.config import BUILD_BATCH_SECTIONS
from taxcite.index import db
from taxcite.models import Provision, SourceType
from taxcite.sources.http import HttpClient
from taxcite.sources.notes import StatutoryNote, to_rows
from taxcite.sources.uscode import (
    ParsedSection,
    download_title26,
    iter_sections,
    ordinal_of,
)

logger: Final = logging.getLogger("taxcite.builder")

ProgressCallback = Callable[[str, int], None]


@dataclass(slots=True)
class BuildResult:
    """A summary of what one build wrote to the index."""

    irc_sections: int = 0
    irc_provisions: int = 0
    reg_sections: int = 0
    reg_provisions: int = 0
    guidance: int = 0
    rates: int = 0
    refs: int = 0
    definitions: int = 0
    source_versions: dict[str, str] = field(default_factory=dict)


def _flush(
    connection: sqlite3.Connection,
    provisions: list[Provision],
    refs: list[tuple[str, str, str, int, str]],
    notes: list[StatutoryNote] | None = None,
) -> None:
    """Write one batch of provisions, refs, and notes inside a single transaction."""
    with db.transaction(connection):
        db.insert_provisions(connection, provisions, ordinal_of(provisions))
        if refs:
            db.insert_refs(connection, refs)
        if notes:
            db.insert_notes(connection, to_rows(notes))


def index_sections(
    connection: sqlite3.Connection,
    sections: Iterable[ParsedSection],
    *,
    batch_size: int = BUILD_BATCH_SECTIONS,
    progress: ProgressCallback | None = None,
) -> tuple[int, int, int]:
    """Insert parsed sections in batches.

    Returns:
        The number of sections, provisions, and cross-references written.
    """
    provisions: list[Provision] = []
    refs: list[tuple[str, str, str, int, str]] = []
    notes: list[StatutoryNote] = []
    n_sections = n_provisions = n_refs = 0
    pending = 0
    for parsed in sections:
        provisions.extend(parsed.provisions)
        notes.extend(parsed.notes)
        refs.extend(
            (ref.from_id, ref.to_id, ref.kind, int(ref.external), ref.raw) for ref in parsed.refs
        )
        n_sections += 1
        n_provisions += len(parsed.provisions)
        n_refs += len(parsed.refs)
        pending += 1
        if pending >= batch_size:
            _flush(connection, provisions, refs, notes)
            provisions.clear()
            refs.clear()
            notes.clear()
            pending = 0
            if progress is not None:
                progress("sections", n_sections)
    if provisions or refs or notes:
        _flush(connection, provisions, refs, notes)
    if progress is not None:
        progress("sections", n_sections)
    return n_sections, n_provisions, n_refs


def build_irc(
    connection: sqlite3.Connection,
    *,
    client: HttpClient,
    force: bool = False,
    xml_path: Path | None = None,
    release_point: str | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[int, int, int, str]:
    """Download and index Title 26.

    Returns:
        Section count, provision count, ref count, and the release point.
    """
    if xml_path is None or release_point is None:
        xml_path, release_point = download_title26(client, force=force)
    logger.info("indexing %s (release point %s)", xml_path, release_point)
    db.clear_source(connection, SourceType.IRC)
    counts = index_sections(connection, iter_sections(xml_path, release_point), progress=progress)
    return (*counts, release_point)


def build_index(
    *,
    irc: bool = True,
    reg_parts: list[str] | None = None,
    irb_years: list[int] | None = None,
    rate_years: list[int] | None = None,
    force: bool = False,
    connection: sqlite3.Connection | None = None,
    client: HttpClient | None = None,
    progress: ProgressCallback | None = None,
) -> BuildResult:
    """Build or refresh the index.

    Args:
        irc: Download and index Title 26.
        reg_parts: CFR Title 26 parts to bulk-fetch from the eCFR, e.g. ``["1", "301"]``.
        irb_years: Years of the Internal Revenue Bulletin to index, e.g. ``[2024, 2025]``.
        rate_years: Years to read published § 382 rates for, from the monthly
            applicable-federal-rate rulings in the Bulletin.
        force: Re-download sources even if they are already on disk.
        connection: An open index connection; one is opened if omitted.
        client: An HTTP client; one is created if omitted.
        progress: Called with ``(stage, count)`` as work proceeds.
    """
    owns_connection = connection is None
    owns_client = client is None
    connection = connection or db.connect()
    client = client or HttpClient()
    result = BuildResult()
    try:
        if irc:
            sections, provisions, refs, release = build_irc(
                connection, client=client, force=force, progress=progress
            )
            result.irc_sections = sections
            result.irc_provisions = provisions
            result.refs += refs
            result.source_versions["irc"] = release
            db.set_meta(connection, "usc_release_point", release)
        if reg_parts:
            from taxcite.sources import ecfr

            db.clear_source(connection, SourceType.REG)
            sections, provisions, refs, ecfr_date = ecfr.build_parts(
                connection, client=client, parts=reg_parts, progress=progress
            )
            result.reg_sections = sections
            result.reg_provisions = provisions
            result.refs += refs
            result.source_versions["reg"] = ecfr_date
            db.set_meta(connection, "ecfr_date", ecfr_date)

        if irb_years:
            from taxcite.sources import irb

            count, covered = irb.build_years(
                connection, client=client, years=irb_years, progress=progress
            )
            result.guidance = count
            db.rebuild_guidance_fts(connection)
            db.set_meta(connection, "irb_years", covered)
            result.source_versions["irb"] = covered

        if rate_years:
            result.rates = _build_rates(
                connection, client=client, years=rate_years, progress=progress
            )

        db.rebuild_fts(connection)
        result.refs += _build_xrefs(connection, progress=progress)
        result.definitions = _build_definitions(connection, progress=progress)

        db.set_meta(connection, "built_at", datetime.now(UTC).isoformat())
        db.set_meta(connection, "taxcite_version", __version__)
        db.set_meta(connection, "schema_version", db.SCHEMA_VERSION)
        db.set_meta(connection, "provision_count", str(db.count_provisions(connection)))
        average = connection.execute("SELECT AVG(LENGTH(text)) AS n FROM provisions").fetchone()
        if average is not None and average["n"]:
            db.set_meta(connection, "avg_text_length", f"{float(average['n']):.2f}")
        connection.commit()
        connection.execute("VACUUM")
        connection.execute("ANALYZE")
        connection.commit()
    finally:
        if owns_client:
            client.close()
        if owns_connection:
            connection.close()
    return result


def _build_rates(
    connection: sqlite3.Connection,
    *,
    client: HttpClient,
    years: list[int],
    progress: ProgressCallback | None,
) -> int:
    """Read the § 382 long-term tax-exempt rate out of each week's Bulletin.

    The rate rulings appear on no fixed week, so every Bulletin in the year is read
    and the ones that carry a rate table contribute. A week that does not exist yet —
    a future week of the current year — is skipped rather than treated as an error.
    """
    from taxcite.errors import SourceUnavailableError
    from taxcite.sources import rates as rate_source

    written = 0
    for year in years:
        for week in range(1, 53):
            try:
                found = rate_source.fetch_rates(client, year, week)
            except SourceUnavailableError:
                continue
            if not found:
                continue
            written += rate_source.index_rates(connection, found)
            if progress is not None:
                progress("published rates", written)
    return written


def _build_xrefs(
    connection: sqlite3.Connection, *, progress: ProgressCallback | None = None
) -> int:
    """Run the cross-reference post-pass over everything just indexed (SPEC 6.8)."""
    from taxcite.graph import xrefs

    return xrefs.build_xrefs(connection, progress=progress)


def _build_definitions(
    connection: sqlite3.Connection, *, progress: ProgressCallback | None = None
) -> int:
    """Run the defined-terms post-pass over everything just indexed (SPEC 6.9)."""
    from taxcite.graph import definitions

    return definitions.build_definitions(connection, progress=progress)
