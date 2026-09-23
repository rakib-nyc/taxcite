"""The cross-reference graph (SPEC 6.8).

Three kinds of edge end up in the ``refs`` table:

``explicit``
    A USLM ``<ref href>`` in statutory text. High confidence, but rare: Title 26 has
    around 120,000 ``<ref>`` elements and only about 1,400 of them sit in operative
    text rather than in notes and source credits.
``implicit``
    A citation written out in prose — "as provided in section 170" — found by running
    the citation parser over indexed text. This carries most of the graph.
``relative``
    A reference to the provision's own surroundings: "this section", "subsection (b)",
    "paragraph (2)". Resolved against the citing provision's own ancestors.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from taxcite.citations import extract_citations
from taxcite.citations import normalize as norm
from taxcite.index import db
from taxcite.models import CrossReference, RefKind, SourceType

logger: Final = logging.getLogger("taxcite.xrefs")

ProgressCallback = Callable[[str, int], None]

#: Relative references TaxCite resolves in v1 (SPEC 6.8).
_RELATIVE_RE: Final = re.compile(
    r"\bthis (?P<this>section|subsection|paragraph|subparagraph|clause)\b"
    r"|\b(?P<named>subsection|paragraph|subparagraph|clause)\s+\((?P<token>[0-9A-Za-z]{1,6})\)",
    re.IGNORECASE,
)

#: Path depth of each level, so a relative reference knows which ancestor it means.
_DEPTH: Final[dict[str, int]] = {
    "section": 0,
    "subsection": 1,
    "paragraph": 2,
    "subparagraph": 3,
    "clause": 4,
}


def relative_targets(
    source: SourceType, section: str, path: list[str], text: str
) -> list[tuple[str, str]]:
    """Resolve relative references in ``text`` against the citing provision's ancestors.

    Returns:
        ``(canonical_id, raw)`` pairs, skipping references that cannot be resolved.
    """
    out: list[tuple[str, str]] = []
    # Regulation paths have no "subsection" level, so every named level shifts up one.
    offset = 0 if source is SourceType.IRC else -1
    for match in _RELATIVE_RE.finditer(text):
        word = (match.group("this") or match.group("named") or "").lower()
        depth = _DEPTH.get(word)
        if depth is None:
            continue
        if depth > 0:
            depth += offset
        if match.group("this"):
            if depth > len(path):
                continue
            target = path[:depth]
        else:
            if depth < 1 or depth - 1 > len(path):
                continue
            target = [*path[: depth - 1], match.group("token")]
        out.append((norm.canonical_id(source, section, target), match.group(0)))
    return out


def build_xrefs(connection: sqlite3.Connection, *, progress: ProgressCallback | None = None) -> int:
    """Extract implicit and relative references from every indexed provision.

    Returns:
        The number of edges written.
    """
    rows = connection.execute(
        "SELECT id, source, section, path, text FROM provisions WHERE text != ''"
    ).fetchall()
    edges: list[tuple[str, str, str, int, str]] = []
    written = 0
    for position, row in enumerate(rows, start=1):
        provision_id = str(row["id"])
        source = SourceType(row["source"])
        section = str(row["section"])
        path: list[str] = json.loads(row["path"])
        text = str(row["text"])

        for citation in extract_citations(text):
            if citation.canonical_id is None or citation.canonical_id == provision_id:
                continue
            edges.append((provision_id, citation.canonical_id, "implicit", 0, citation.raw))
        for target, raw in relative_targets(source, section, path, text):
            if target == provision_id:
                continue
            edges.append((provision_id, target, "relative", 0, raw))

        if len(edges) >= 5000:
            written += _flush(connection, edges)
            edges.clear()
            if progress is not None:
                progress("cross-references", position)
    written += _flush(connection, edges)
    if progress is not None:
        progress("cross-references", len(rows))
    logger.info("wrote %d cross-reference edges", written)
    return written


def _flush(connection: sqlite3.Connection, edges: list[tuple[str, str, str, int, str]]) -> int:
    """Write a batch of edges in one transaction."""
    if not edges:
        return 0
    with db.transaction(connection):
        db.insert_refs(connection, edges)
    return len(edges)


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------


def outgoing(
    connection: sqlite3.Connection, provision_id: str, *, internal: bool = False
) -> list[CrossReference]:
    """Return everything the provision and its descendants cite, deduplicated.

    Args:
        connection: An open index connection.
        provision_id: The canonical id to start from.
        internal: Include references to the provision's own section. These are mostly
            "this subsection" and "paragraph (2)" pointers, which say nothing about how
            a provision relates to the rest of the Code.
    """
    rows = connection.execute(
        "SELECT DISTINCT r.to_id, r.kind, r.external, r.raw, p.heading, p.section, "
        "p.source, p.path FROM refs r LEFT JOIN provisions p ON p.id = r.to_id "
        "WHERE r.from_id = ? OR r.from_id LIKE ? || '/%' "
        "ORDER BY r.to_id",
        (provision_id, provision_id),
    ).fetchall()
    edges = [_to_cross_reference(row, from_id=provision_id, direction="out") for row in rows]
    return edges if internal else _without_internal(edges, provision_id, "to_id")


def incoming(
    connection: sqlite3.Connection, provision_id: str, *, internal: bool = False
) -> list[CrossReference]:
    """Return everything that cites the provision or any of its descendants."""
    rows = connection.execute(
        "SELECT DISTINCT r.from_id, r.kind, r.external, r.raw, p.heading, p.section, "
        "p.source, p.path FROM refs r LEFT JOIN provisions p ON p.id = r.from_id "
        "WHERE r.to_id = ? OR r.to_id LIKE ? || '/%' "
        "ORDER BY r.from_id",
        (provision_id, provision_id),
    ).fetchall()
    edges = [_to_cross_reference(row, from_id=provision_id, direction="in") for row in rows]
    return edges if internal else _without_internal(edges, provision_id, "from_id")


def _without_internal(
    edges: list[CrossReference], provision_id: str, side: str
) -> list[CrossReference]:
    """Drop edges that stay inside the section being asked about."""
    try:
        own_section = norm.section_id(provision_id)
    except ValueError:  # pragma: no cover - callers pass canonical ids
        return edges
    prefix = f"{own_section}/"
    out: list[CrossReference] = []
    for edge in edges:
        other = edge.to_id if side == "to_id" else edge.from_id
        if other == own_section or other.startswith(prefix):
            continue
        out.append(edge)
    return out


def _to_cross_reference(row: sqlite3.Row, *, from_id: str, direction: str) -> CrossReference:
    """Build a :class:`CrossReference` from a joined refs row."""
    other = str(row["to_id"] if direction == "out" else row["from_id"])
    display: str | None = None
    if row["section"] is not None:
        display = norm.display_for(
            SourceType(row["source"]), str(row["section"]), json.loads(row["path"])
        )
    elif other.startswith(("/us/usc/t26/s", "/us/cfr/t26/s")):
        source, section, path = norm.split_canonical_id(other)
        display = norm.display_for(source, section, path)
    kind: RefKind = str(row["kind"])  # type: ignore[assignment]
    return CrossReference(
        from_id=from_id if direction == "out" else other,
        to_id=other if direction == "out" else from_id,
        kind=kind,
        external=bool(row["external"]),
        raw=row["raw"],
        heading=row["heading"],
        display=display,
    )


@dataclass(slots=True)
class ClosureEntry:
    """One provision you have to read to understand the one you started from."""

    provision_id: str
    display: str
    heading: str | None
    depth: int
    reached_via: str
    excerpt: str


def closure(
    connection: sqlite3.Connection,
    provision_id: str,
    *,
    max_depth: int = 2,
    limit: int = 40,
    excerpt_chars: int = 220,
) -> list[ClosureEntry]:
    """Return everything you must read to understand a provision (roadmap 2.7).

    Tax provisions are a graph, not a list. Understanding § 163(j)(1) means reading
    what it points at, and what that points at, and the practical question is not
    "what cites this" but "what do I have to have read before this sentence means
    anything". This walks outward breadth-first, so the things you need first come
    first, and stops at ``max_depth`` because the transitive closure of the Code is
    the Code.

    Internal references — "this subsection", "paragraph (2)" — are followed but not
    listed: they tell you where to look inside something you are already reading.
    """
    from taxcite.index import db

    start = norm.section_id(provision_id)
    seen: set[str] = {provision_id, start}
    out: list[ClosureEntry] = []
    frontier: list[tuple[str, str]] = [(provision_id, provision_id)]

    for depth in range(1, max_depth + 1):
        next_frontier: list[tuple[str, str]] = []
        for current, _origin in frontier:
            for edge in outgoing(connection, current):
                target = edge.to_id
                if target in seen or edge.external:
                    continue
                seen.add(target)
                provision = db.get_provision(connection, target)
                if provision is None:
                    continue
                out.append(
                    ClosureEntry(
                        provision_id=target,
                        display=edge.display or target,
                        heading=provision.heading,
                        depth=depth,
                        reached_via=norm.display_for(*norm.split_canonical_id(current)),
                        excerpt=_excerpt(provision.full_text, excerpt_chars),
                    )
                )
                next_frontier.append((target, current))
                if len(out) >= limit:
                    return out
        frontier = next_frontier
        if not frontier:
            break
    return out


def _excerpt(text: str, limit: int) -> str:
    """Trim a provision's text to a readable excerpt."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed.rfind(" ", 0, limit)
    return collapsed[: cut if cut > 0 else limit].rstrip() + "\u2026"


def authority_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Return the number of distinct provisions citing each section."""
    rows = connection.execute(
        "SELECT to_id, COUNT(DISTINCT from_id) AS n FROM refs GROUP BY to_id"
    ).fetchall()
    return {str(row["to_id"]): int(row["n"]) for row in rows}
