"""The public Python API (SPEC 8).

Everything here opens the index, does one job, and returns pydantic models. The CLI and
the MCP server are both thin wrappers around these functions, so that the three
interfaces cannot drift apart.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from taxcite.citations import normalize as norm
from taxcite.citations import parse_citation
from taxcite.citations.patterns import IRC_SECTION_NUM, PINPOINT_PATH, REG_SECTION_NUM
from taxcite.config import DEFAULT_MAX_CHARS, DEFAULT_SEARCH_LIMIT
from taxcite.errors import ProvisionNotFoundError
from taxcite.index import db
from taxcite.index import search as fts
from taxcite.models import Citation, Provision, SearchHit, SourceType

#: A bare section number, with or without a pinpoint: "162", "1.162-1(a)", "7701(a)(30)".
_BARE_SECTION_RE = re.compile(
    rf"^\s*(?:{REG_SECTION_NUM}|{IRC_SECTION_NUM})(?:{PINPOINT_PATH})\s*$"
)


def coerce_citation(text: str) -> Citation:
    """Parse a citation, accepting a bare section number as well as a full citation.

    Models and people both write ``162`` when they mean ``\u00a7 162``. The parser is
    deliberately strict about that — a bare number in prose is almost never a citation —
    so the leniency lives here, where the whole argument is known to be a citation.

    Raises:
        MalformedCitationError: if the text holds no citation at all.
    """
    stripped = text.strip()
    if _BARE_SECTION_RE.match(stripped):
        return parse_citation(f"\u00a7 {stripped}")
    return parse_citation(stripped)


@contextmanager
def open_index(
    path: Path | None = None, *, as_of: date | None = None
) -> Iterator[sqlite3.Connection]:
    """Open the index for reading, raising a friendly error if it has not been built.

    Args:
        path: An explicit index file; the configured one is used if omitted.
        as_of: Read the law as it stood on this date, from the historical version
            covering it. Requires that version to have been built.
    """
    connection = db.connect(path, read_only=True)
    historical: sqlite3.Connection | None = None
    try:
        db.require_index(connection)
        if as_of is None:
            yield connection
            return
        from taxcite.index import versions

        opened, _point = versions.open_as_of(as_of, main=connection)
        historical = opened if opened is not connection else None
        yield opened
    finally:
        if historical is not None:
            historical.close()
        connection.close()


@dataclass(slots=True)
class LookupResult:
    """A provision plus the context a reader or a model needs to cite it."""

    provision: Provision
    citation: Citation
    source_url: str | None
    text: str
    truncated: bool = False
    children: list[Provision] = field(default_factory=list)
    source_version: str = ""


def lookup(
    citation: str,
    *,
    include_children: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
    offline: bool = False,
    as_of: date | None = None,
    connection: sqlite3.Connection | None = None,
    path: Path | None = None,
) -> LookupResult:
    """Look up the text of a cited provision.

    Args:
        citation: Any supported citation form, e.g. ``"§ 162(a)"``.
        include_children: Return the provision's full text rather than its own text.
        max_chars: Truncate the returned text at this length, on a word boundary.
        offline: Never fetch an unindexed regulation from the eCFR.
        as_of: Read the law as it stood on this date.
        connection: An open index connection; one is opened if omitted.
        path: An index path, when opening a connection.

    Raises:
        MalformedCitationError: if ``citation`` cannot be parsed.
        ProvisionNotFoundError: if the provision is not in the index.
    """
    parsed = coerce_citation(citation)
    if parsed.canonical_id is None:
        raise ProvisionNotFoundError(
            f"{parsed.display} is not a statutory or regulatory citation",
            hint="TaxCite v1 indexes the Internal Revenue Code and Treasury Regulations.",
        )
    if connection is not None:
        return _lookup(connection, parsed, include_children, max_chars, offline)
    with open_index(path, as_of=as_of) as conn:
        return _lookup(conn, parsed, include_children, max_chars, offline)


def _lookup(
    connection: sqlite3.Connection,
    citation: Citation,
    include_children: bool,
    max_chars: int,
    offline: bool = False,
) -> LookupResult:
    """Look up a parsed citation against an open index."""
    provision_id = citation.canonical_id
    if provision_id is None:  # pragma: no cover - guarded by the caller
        raise ProvisionNotFoundError(f"{citation.display} has no canonical identifier")
    provision = db.get_provision(connection, provision_id)
    if provision is None and citation.source is SourceType.REG and not offline:
        provision = _fetch_regulation(connection, citation, provision_id)
    if provision is None:
        raise ProvisionNotFoundError(
            f"{citation.display} is not in the index",
            hint=(
                "Regulations are fetched on demand; check the citation, or run without --offline."
                if citation.source is SourceType.REG
                else "Run: taxcite build-index"
            ),
        )
    body = provision.full_text if include_children else provision.text
    text, truncated = truncate(body, max_chars)
    children = db.get_children(connection, provision.id) if include_children else []
    return LookupResult(
        provision=provision,
        citation=citation,
        source_url=norm.source_url_for(provision.source, provision.section, provision.path),
        text=text,
        truncated=truncated,
        children=children,
        source_version=provision.source_version,
    )


def _fetch_regulation(
    connection: sqlite3.Connection, citation: Citation, provision_id: str
) -> Provision | None:
    """Fetch an unindexed regulation section from the eCFR and write it through."""
    from taxcite.sources.ecfr import make_fetcher
    from taxcite.sources.http import HttpClient

    if citation.section is None:  # pragma: no cover - reg citations always have one
        return None
    with HttpClient() as client:
        if not make_fetcher(client)(connection, citation.section):
            return None
    return db.get_provision(connection, provision_id)


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate ``text`` at a word boundary, reporting whether anything was cut."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    # Look one character past the limit, so a word that ends exactly on it survives.
    cut = text.rfind(" ", 0, max_chars + 1)
    return text[: cut if cut > 0 else max_chars].rstrip(), True


def search(
    query: str,
    *,
    source: SourceType | str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    as_of: date | None = None,
    connection: sqlite3.Connection | None = None,
    path: Path | None = None,
) -> list[SearchHit]:
    """Search the indexed text of the Code and the regulations.

    Args:
        query: Free text. Treated as literal tokens, not FTS5 query syntax.
        source: ``"irc"``, ``"reg"``, ``"all"``, or ``None`` for everything.
        limit: Maximum number of hits.
        as_of: Search the law as it stood on this date.
        connection: An open index connection; one is opened if omitted.
        path: An index path, when opening a connection.
    """
    resolved = _as_source(source)
    if connection is not None:
        return fts.search(connection, query, source=resolved, limit=limit)
    with open_index(path, as_of=as_of) as conn:
        return fts.search(conn, query, source=resolved, limit=limit)


def _as_source(source: SourceType | str | None) -> SourceType | None:
    """Coerce a source argument, treating ``"all"`` as no restriction."""
    if source is None or source == "all":
        return None
    return SourceType(source)


def list_subdivisions(
    citation: str,
    *,
    connection: sqlite3.Connection | None = None,
    path: Path | None = None,
) -> tuple[Provision, list[Provision]]:
    """Return a provision and its direct children, for navigating before fetching text.

    Raises:
        ProvisionNotFoundError: if the provision is not in the index.
    """
    result = lookup(citation, include_children=True, max_chars=0, connection=connection, path=path)
    return result.provision, result.children


def index_info(
    *, connection: sqlite3.Connection | None = None, path: Path | None = None
) -> dict[str, str]:
    """Return build metadata and provision counts for the index."""
    if connection is not None:
        return _index_info(connection)
    with open_index(path) as conn:
        return _index_info(conn)


def _index_info(connection: sqlite3.Connection) -> dict[str, str]:
    """Collect metadata from an open index."""
    info = dict(db.all_meta(connection))
    info["irc_provisions"] = str(db.count_provisions(connection, SourceType.IRC))
    info["reg_provisions"] = str(db.count_provisions(connection, SourceType.REG))
    info["irc_sections"] = str(len(db.section_numbers(connection, SourceType.IRC)))
    info["reg_sections"] = str(len(db.section_numbers(connection, SourceType.REG)))
    return info
