"""Historical versions of the Code: "what did this say in 2022?" (roadmap 1.1).

Tax work is retrospective. An examination of the 2022 return turns on the law as it
stood in 2022, and current law is simply the wrong answer. The OLRC publishes a
*release point* for every public law — the state of the Code immediately after that
law was folded in — which makes any past state of the Code addressable by date.

Each indexed release point gets its own database file. The alternative, a version
column on ``provisions``, would mean tens of millions of rows for a corpus that barely
changes between release points, and would slow down the overwhelmingly common case of
asking about current law. Separate files keep current-law queries untouched and make
the disk cost of history visible and deletable.

Versions are built on demand. Nobody wants 165 copies of Title 26 on their laptop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Final

from taxcite.config import data_dir
from taxcite.errors import ConfigurationError, SourceUnavailableError
from taxcite.index import db
from taxcite.sources.http import HttpClient
from taxcite.sources.uscode import (
    ReleasePoint,
    discover_release_points,
    release_point_for,
)

logger: Final = logging.getLogger("taxcite.versions")

ProgressCallback = Callable[[str, int], None]


def versions_dir() -> Path:
    """Return the directory holding per-release-point databases."""
    path = data_dir() / "versions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def version_db_path(release: str) -> Path:
    """Return the database path for one release point."""
    return versions_dir() / f"usc26@{release}.db"


# --------------------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------------------


def _row_to_point(row: sqlite3.Row) -> ReleasePoint:
    """Build a :class:`ReleasePoint` from a catalogue row."""
    enacted = row["enacted"]
    return ReleasePoint(
        release=str(row["release"]),
        url=str(row["url"]),
        enacted=date.fromisoformat(enacted) if enacted else None,
        titles=tuple(json.loads(row["titles"] or "[]")),
    )


def catalogue(connection: sqlite3.Connection) -> list[ReleasePoint]:
    """Return every known release point, newest first."""
    rows = connection.execute(
        "SELECT release, enacted, titles, url FROM release_points "
        "ORDER BY enacted IS NULL DESC, enacted DESC, release DESC"
    ).fetchall()
    return [_row_to_point(row) for row in rows]


def indexed_releases(connection: sqlite3.Connection) -> set[str]:
    """Return the release points that have been downloaded and indexed locally."""
    rows = connection.execute("SELECT release FROM release_points WHERE indexed = 1").fetchall()
    return {str(row["release"]) for row in rows}


def current_release(connection: sqlite3.Connection) -> str | None:
    """Return the release point the main index holds."""
    return db.get_meta(connection, "usc_release_point")


def refresh_catalogue(connection: sqlite3.Connection, client: HttpClient) -> list[ReleasePoint]:
    """Scrape the OLRC release-point listing and store it.

    Returns:
        Every release point that touched Title 26, newest first.
    """
    points = discover_release_points(client)
    current = current_release(connection)
    with db.transaction(connection):
        connection.executemany(
            "INSERT INTO release_points(release, enacted, titles, url, is_current) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(release) DO UPDATE SET "
            "enacted=COALESCE(excluded.enacted, release_points.enacted), "
            "titles=excluded.titles, url=excluded.url, "
            "is_current=excluded.is_current",
            [
                (
                    point.release,
                    point.enacted.isoformat() if point.enacted else None,
                    json.dumps(list(point.titles)),
                    point.url,
                    int(point.release == current),
                )
                for point in points
            ],
        )
        _mark_indexed(connection)
    return points


def _mark_indexed(connection: sqlite3.Connection) -> None:
    """Record which release points actually have a database on disk."""
    on_disk = {path.stem.split("@", 1)[-1] for path in versions_dir().glob("usc26@*.db")}
    current = current_release(connection)
    if current:
        on_disk.add(current)
    connection.execute("UPDATE release_points SET indexed = 0")
    if on_disk:
        placeholders = ",".join("?" * len(on_disk))
        connection.execute(
            # The only interpolation is the placeholder list; the releases are bound.
            f"UPDATE release_points SET indexed = 1 WHERE release IN ({placeholders})",
            tuple(sorted(on_disk)),
        )


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------


def resolve(
    connection: sqlite3.Connection, as_of: date, *, client: HttpClient | None = None
) -> ReleasePoint:
    """Return the release point stating the law in force on ``as_of``.

    Raises:
        ConfigurationError: if no release point covers the date.
    """
    points = catalogue(connection)
    if not points and client is not None:
        points = refresh_catalogue(connection, client)
    if not points:
        raise ConfigurationError(
            "no release points are known",
            hint="Run: taxcite versions --refresh",
        )
    point = release_point_for(points, as_of)
    if point is None:
        earliest = min((p.enacted for p in points if p.enacted), default=None)
        raise ConfigurationError(
            f"no published release point covers {as_of.isoformat()}",
            hint=(
                f"The earliest release point available is {earliest.isoformat()}."
                if earliest
                else "Run: taxcite versions --refresh"
            ),
        )
    return point


def open_as_of(
    as_of: date,
    *,
    main: sqlite3.Connection,
    client: HttpClient | None = None,
    build: bool = False,
) -> tuple[sqlite3.Connection, ReleasePoint]:
    """Open the index stating the law in force on ``as_of``.

    Returns the main connection unchanged when the date falls inside the current
    release point, so that the common case costs nothing.

    Raises:
        ConfigurationError: if the version is not indexed and ``build`` is false.
    """
    point = resolve(main, as_of, client=client)
    if point.release == current_release(main):
        return main, point
    path = version_db_path(point.release)
    if not path.exists():
        if not build:
            raise ConfigurationError(
                f"the law as of {as_of.isoformat()} (release point {point.release}) "
                "is not indexed locally",
                hint=f"Run: taxcite build-index --as-of {as_of.isoformat()}",
            )
        if client is None:  # pragma: no cover - callers pass a client when building
            raise ConfigurationError("cannot build a historical index without a client")
        build_version(point, client=client)
        _mark_indexed_after_build(main, point.release)
    return db.connect(path, read_only=True), point


def _mark_indexed_after_build(main: sqlite3.Connection, release: str) -> None:
    """Flag a release point as present on disk."""
    main.execute("UPDATE release_points SET indexed = 1 WHERE release = ?", (release,))
    main.commit()


# --------------------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------------------


def build_version(
    point: ReleasePoint,
    *,
    client: HttpClient,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> Path:
    """Download and index one historical release point into its own database.

    Raises:
        SourceUnavailableError: if that release point's archive cannot be fetched.
    """
    from taxcite.graph import definitions, xrefs
    from taxcite.index.builder import index_sections
    from taxcite.sources.uscode import download_title26, iter_sections

    path = version_db_path(point.release)
    if path.exists() and not force:
        logger.info("release point %s is already indexed", point.release)
        return path

    logger.info("building the index for release point %s", point.release)
    try:
        xml_path, release = download_title26(client, force=force, point=point)
    except SourceUnavailableError as exc:
        raise SourceUnavailableError(
            f"release point {point.release} could not be downloaded: {exc.message}",
            hint="The OLRC does not keep every historical archive in the same place.",
        ) from exc

    partial = path.with_suffix(".db.partial")
    partial.unlink(missing_ok=True)
    connection = db.connect(partial)
    try:
        index_sections(connection, iter_sections(xml_path, release), progress=progress)
        db.rebuild_fts(connection)
        xrefs.build_xrefs(connection, progress=progress)
        definitions.build_definitions(connection, progress=progress)
        db.set_meta(connection, "usc_release_point", release)
        db.set_meta(connection, "schema_version", db.SCHEMA_VERSION)
        if point.enacted:
            db.set_meta(connection, "release_enacted", point.enacted.isoformat())
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()
    partial.replace(path)
    logger.info("release point %s indexed at %s", point.release, path)
    return path


def disk_usage() -> dict[str, int]:
    """Return the size in bytes of each indexed historical version."""
    return {
        path.stem.split("@", 1)[-1]: path.stat().st_size
        for path in sorted(versions_dir().glob("usc26@*.db"))
    }


def remove_version(release: str) -> bool:
    """Delete one historical version's database. Returns ``True`` if it existed."""
    path = version_db_path(release)
    if not path.exists():
        return False
    path.unlink()
    logger.info("removed the index for release point %s", release)
    return True
