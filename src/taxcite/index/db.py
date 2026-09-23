"""SQLite connection handling and typed query helpers for the TaxCite index."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from taxcite.config import db_path
from taxcite.errors import IndexNotBuiltError
from taxcite.models import Provision, ProvisionStatus, SourceType

logger: Final = logging.getLogger("taxcite.index")

SCHEMA_PATH: Final = Path(__file__).with_name("schema.sql")

#: Bumped whenever the schema changes in a way that requires a rebuild.
SCHEMA_VERSION: Final = "1"


def connect(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the index, applying the schema if the file is new."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if read_only and not target.exists():
        raise IndexNotBuiltError(f"no index at {target}")
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if not read_only:
        apply_schema(connection)
    return connection


#: Columns added to existing tables after their first release. ``CREATE TABLE IF NOT
#: EXISTS`` does nothing to a table that already exists, so an index built by an
#: earlier version keeps its old shape until these are applied. Additive migration
#: only: an index costs minutes to build and a few hundred megabytes, and asking for
#: a rebuild over a new column would be rude.
MIGRATIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("cases", "cluster_id", "INTEGER"),
    ("cases", "opinion_id", "INTEGER"),
    ("cases", "text", "TEXT"),
    ("cases", "excerpt", "TEXT"),
    ("cases", "citing_count", "INTEGER"),
    ("cases", "last_cited", "TEXT"),
    ("cases", "last_citing", "TEXT"),
)


def apply_schema(connection: sqlite3.Connection) -> None:
    """Create every table, index, and virtual table the index needs."""
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate(connection)
    connection.commit()


def migrate(connection: sqlite3.Connection) -> list[str]:
    """Add any columns missing from an index built by an earlier version.

    Returns:
        The ``table.column`` names that were added.
    """
    existing = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    added: list[str] = []
    for table, column, kind in MIGRATIONS:
        if table not in existing:
            continue
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        added.append(f"{table}.{column}")
    if added:
        logger.info("migrated the index: added %s", ", ".join(added))
    return added


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction, rolling back on failure."""
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    connection.commit()


# --------------------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------------------


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert one row of build metadata."""
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(connection: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Return one row of build metadata."""
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def all_meta(connection: sqlite3.Connection) -> dict[str, str]:
    """Return every row of build metadata."""
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM meta")
    }


def source_versions(connection: sqlite3.Connection) -> dict[str, str]:
    """Return the source version recorded for each corpus that has been indexed."""
    versions: dict[str, str] = {}
    for key, label in (
        ("usc_release_point", "irc"),
        ("ecfr_date", "reg"),
        ("irb_years", "irb"),
    ):
        value = get_meta(connection, key)
        if value:
            versions[label] = value
    return versions


# --------------------------------------------------------------------------------------
# provisions
# --------------------------------------------------------------------------------------


def row_to_provision(row: sqlite3.Row) -> Provision:
    """Convert a ``provisions`` row into a :class:`~taxcite.models.Provision`."""
    status: ProvisionStatus = row["status"]
    return Provision(
        id=row["id"],
        source=SourceType(row["source"]),
        section=row["section"],
        path=json.loads(row["path"]),
        level=row["level"],
        num=row["num"] or "",
        heading=row["heading"],
        text=row["text"],
        full_text=row["full_text"],
        parent_id=row["parent_id"],
        status=status,
        source_version=row["source_version"],
    )


def insert_provisions(
    connection: sqlite3.Connection, provisions: Sequence[Provision], ordinals: dict[str, int]
) -> None:
    """Insert or replace a batch of provisions and their FTS rows."""
    rows = [
        (
            provision.id,
            provision.source.value,
            provision.section,
            json.dumps(provision.path),
            provision.level,
            provision.num,
            provision.heading,
            provision.text,
            provision.full_text,
            provision.parent_id,
            ordinals.get(provision.id, 0),
            provision.status,
            provision.source_version,
        )
        for provision in provisions
    ]
    connection.executemany(
        "INSERT INTO provisions("
        "id, source, section, path, level, num, heading, text, full_text, "
        "parent_id, ordinal, status, source_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "source=excluded.source, section=excluded.section, path=excluded.path, "
        "level=excluded.level, num=excluded.num, heading=excluded.heading, "
        "text=excluded.text, full_text=excluded.full_text, "
        "parent_id=excluded.parent_id, ordinal=excluded.ordinal, "
        "status=excluded.status, source_version=excluded.source_version",
        rows,
    )


def get_provision(connection: sqlite3.Connection, provision_id: str) -> Provision | None:
    """Return one provision by canonical id, or ``None``."""
    row = connection.execute("SELECT * FROM provisions WHERE id = ?", (provision_id,)).fetchone()
    return row_to_provision(row) if row is not None else None


def get_children(connection: sqlite3.Connection, provision_id: str) -> list[Provision]:
    """Return a provision's direct children in document order."""
    rows = connection.execute(
        "SELECT * FROM provisions WHERE parent_id = ? ORDER BY ordinal", (provision_id,)
    ).fetchall()
    return [row_to_provision(row) for row in rows]


def get_descendants(connection: sqlite3.Connection, provision_id: str) -> list[Provision]:
    """Return every descendant of a provision, depth-first in document order."""
    out: list[Provision] = []
    stack = list(reversed(get_children(connection, provision_id)))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(get_children(connection, node.id)))
    return out


def section_numbers(connection: sqlite3.Connection, source: SourceType) -> list[str]:
    """Return every section number indexed for ``source``."""
    rows = connection.execute(
        "SELECT section FROM provisions WHERE source = ? AND path = '[]' ORDER BY section",
        (source.value,),
    ).fetchall()
    return [str(row["section"]) for row in rows]


def count_provisions(connection: sqlite3.Connection, source: SourceType | None = None) -> int:
    """Return the number of indexed provisions, optionally for one source."""
    if source is None:
        row = connection.execute("SELECT COUNT(*) AS n FROM provisions").fetchone()
    else:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM provisions WHERE source = ?", (source.value,)
        ).fetchone()
    return int(row["n"])


def require_index(connection: sqlite3.Connection) -> None:
    """Raise :class:`IndexNotBuiltError` if the index holds no provisions."""
    if count_provisions(connection) == 0:
        raise IndexNotBuiltError()


# --------------------------------------------------------------------------------------
# refs and definitions
# --------------------------------------------------------------------------------------


def insert_refs(
    connection: sqlite3.Connection, refs: Iterable[tuple[str, str, str, int, str]]
) -> None:
    """Insert cross-reference edges, ignoring duplicates."""
    connection.executemany(
        "INSERT OR IGNORE INTO refs(from_id, to_id, kind, external, raw) VALUES(?,?,?,?,?)",
        list(refs),
    )


def insert_definitions(
    connection: sqlite3.Connection,
    definitions: Iterable[tuple[str, str, str, str | None, str, str | None]],
) -> None:
    """Insert defined terms, replacing any earlier row for the same term and provision."""
    rows = list(definitions)
    connection.executemany(
        "INSERT INTO definitions("
        "term_norm, term_display, provision_id, scope, definition_text, by_reference) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(term_norm, provision_id) DO UPDATE SET "
        "term_display=excluded.term_display, scope=excluded.scope, "
        "definition_text=excluded.definition_text, by_reference=excluded.by_reference",
        rows,
    )
    connection.executemany(
        "INSERT INTO definitions_fts(term_norm, term_display) VALUES(?, ?)",
        [(row[0], row[1]) for row in rows],
    )


def insert_notes(
    connection: sqlite3.Connection,
    rows: Iterable[tuple[str, int, str, str | None, str, str, str | None]],
) -> None:
    """Insert statutory notes, replacing any earlier row for the same slot."""
    connection.executemany(
        "INSERT INTO notes(provision_id, ordinal, topic, heading, text, dates, "
        "applies_after) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(provision_id, ordinal) DO UPDATE SET "
        "topic=excluded.topic, heading=excluded.heading, text=excluded.text, "
        "dates=excluded.dates, applies_after=excluded.applies_after",
        list(rows),
    )


def get_notes(connection: sqlite3.Connection, provision_id: str) -> list[sqlite3.Row]:
    """Return the notes attached to a section, in document order."""
    return list(
        connection.execute(
            "SELECT * FROM notes WHERE provision_id = ? ORDER BY ordinal",
            (provision_id,),
        ).fetchall()
    )


def insert_guidance(
    connection: sqlite3.Connection,
    rows: Iterable[tuple[str, str, str, str, str, str, str, str | None, str]],
) -> None:
    """Insert published guidance documents, replacing any earlier copy."""
    connection.executemany(
        "INSERT INTO guidance(id, kind, number, title, text, bulletin, url, "
        "published, status) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, number=excluded.number, "
        "title=excluded.title, text=excluded.text, bulletin=excluded.bulletin, "
        "url=excluded.url, published=excluded.published, status=excluded.status",
        list(rows),
    )


def insert_guidance_relations(
    connection: sqlite3.Connection, rows: Iterable[tuple[str, str, str]]
) -> None:
    """Insert the relationships between guidance documents."""
    connection.executemany(
        "INSERT OR IGNORE INTO guidance_relations(from_id, to_id, kind) VALUES(?,?,?)",
        list(rows),
    )


def get_guidance(connection: sqlite3.Connection, guidance_id: str) -> sqlite3.Row | None:
    """Return one guidance document by canonical id."""
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM guidance WHERE id = ?", (guidance_id,)
    ).fetchone()
    return row


def guidance_acting_on(connection: sqlite3.Connection, guidance_id: str) -> list[sqlite3.Row]:
    """Return the later documents that act on this one."""
    return list(
        connection.execute(
            "SELECT g.*, r.kind AS relation FROM guidance_relations r "
            "JOIN guidance g ON g.id = r.from_id WHERE r.to_id = ? "
            "ORDER BY g.number DESC",
            (guidance_id,),
        ).fetchall()
    )


def count_guidance(connection: sqlite3.Connection) -> int:
    """Return how many guidance documents are indexed."""
    row = connection.execute("SELECT COUNT(*) AS n FROM guidance").fetchone()
    return int(row["n"])


def insert_cases(
    connection: sqlite3.Connection,
    rows: Iterable[
        tuple[
            str,
            str,
            str,
            str,
            str | None,
            str,
            str,
            int,
            int | None,
            int | None,
            str | None,
            str | None,
            int | None,
            str | None,
            str | None,
        ]
    ],
) -> None:
    """Insert cached court decisions, replacing any earlier copy.

    Opinion text is only overwritten when the new row actually has some, so a
    keyless refresh never discards text a token previously fetched.
    """
    connection.executemany(
        "INSERT INTO cases(id, reporter_cite, case_name, court, date_filed, url, "
        "citations, cite_count, cluster_id, opinion_id, text, excerpt, "
        "citing_count, last_cited, last_citing) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET case_name=excluded.case_name, "
        "court=excluded.court, date_filed=excluded.date_filed, url=excluded.url, "
        "citations=excluded.citations, cite_count=excluded.cite_count, "
        "cluster_id=COALESCE(excluded.cluster_id, cases.cluster_id), "
        "opinion_id=COALESCE(excluded.opinion_id, cases.opinion_id), "
        "text=COALESCE(excluded.text, cases.text), "
        "excerpt=COALESCE(excluded.excerpt, cases.excerpt), "
        "citing_count=COALESCE(excluded.citing_count, cases.citing_count), "
        "last_cited=COALESCE(excluded.last_cited, cases.last_cited), "
        "last_citing=COALESCE(excluded.last_citing, cases.last_citing)",
        list(rows),
    )


def insert_rates(
    connection: sqlite3.Connection,
    rows: Iterable[tuple[str, float, float | None, str, str, str]],
) -> None:
    """Insert published rates, replacing any earlier copy of the same month."""
    connection.executemany(
        "INSERT INTO rates(month, long_term_tax_exempt, adjusted_federal_long_term, "
        "ruling, bulletin, url) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(month) DO UPDATE SET "
        "long_term_tax_exempt=excluded.long_term_tax_exempt, "
        "adjusted_federal_long_term=COALESCE("
        "excluded.adjusted_federal_long_term, rates.adjusted_federal_long_term), "
        "ruling=excluded.ruling, bulletin=excluded.bulletin, url=excluded.url",
        list(rows),
    )


def get_rate(connection: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    """Return the published rates for one month, by its first day."""
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM rates WHERE month = ?", (month,)
    ).fetchone()
    return row


def rate_months(connection: sqlite3.Connection) -> list[str]:
    """Return every month the index holds rates for, earliest first."""
    return [str(r["month"]) for r in connection.execute("SELECT month FROM rates ORDER BY month")]


def get_case(connection: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    """Return one cached decision by canonical id."""
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM cases WHERE id = ?", (case_id,)
    ).fetchone()
    return row


def rebuild_guidance_fts(connection: sqlite3.Connection) -> None:
    """Rebuild the guidance full-text index."""
    connection.execute("INSERT INTO guidance_fts(guidance_fts) VALUES('rebuild')")
    connection.commit()


def rebuild_fts(connection: sqlite3.Connection) -> None:
    """Rebuild the full-text index from the ``provisions`` table."""
    connection.execute("INSERT INTO provisions_fts(provisions_fts) VALUES('rebuild')")
    connection.commit()


def clear_source(connection: sqlite3.Connection, source: SourceType) -> None:
    """Delete every provision, ref, and definition belonging to one source."""
    connection.execute(
        "DELETE FROM refs WHERE from_id IN (SELECT id FROM provisions WHERE source = ?)",
        (source.value,),
    )
    connection.execute(
        "DELETE FROM definitions WHERE provision_id IN "
        "(SELECT id FROM provisions WHERE source = ?)",
        (source.value,),
    )
    connection.execute(
        "DELETE FROM notes WHERE provision_id IN (SELECT id FROM provisions WHERE source = ?)",
        (source.value,),
    )
    connection.execute("DELETE FROM provisions WHERE source = ?", (source.value,))
    connection.commit()


def query(
    connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> list[sqlite3.Row]:
    """Run a query and return every row."""
    return list(connection.execute(sql, tuple(params)).fetchall())
