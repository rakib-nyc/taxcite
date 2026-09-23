"""Full-text search over the index (SPEC 8).

FTS5 indexes each provision's own text, so a phrase matches at the deepest provision
that actually contains it rather than at every ancestor as well.

BM25 alone ranks statute badly. Its length normalisation assumes a short document
mentioning a phrase is "more about" that phrase than a long one; in the Code the
reverse is usually true, because the operative provision is long and the short mentions
elsewhere borrow its language or point back to it. Searching "ordinary and necessary"
with plain BM25 buries § 162(a) below a dozen provisions that merely reuse the phrase.

So the BM25 candidates are re-ranked by fusing three rankings of the same candidate set
with reciprocal rank fusion:

1. **relevance** — BM25 over the heading and own text;
2. **authority** — how many distinct provisions cite the owning section;
3. **depth** — how far down the section's hierarchy the provision sits;
4. **length** — how substantial the provision's own text is.

Rank fusion rather than weighted scores, because the three signals are on wildly
different scales and multiplying them lets whichever has the widest dynamic range win.
The weights live in :mod:`taxcite.config`; ``docs/architecture.md`` records the query
sets they were tuned and validated against.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Final

from taxcite.citations import normalize
from taxcite.config import (
    DEFAULT_SEARCH_LIMIT,
    SEARCH_AUTHORITY_WEIGHT,
    SEARCH_DEPTH_WEIGHT,
    SEARCH_LENGTH_WEIGHT,
    SEARCH_RERANK_CANDIDATES,
    SEARCH_RRF_K,
)
from taxcite.index import db
from taxcite.models import SearchHit, SourceType

#: Characters that would otherwise be read as FTS5 query syntax.
_FTS_SPECIAL: Final = re.compile(r'["*()^:]')
_TOKEN_RE: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z'\-]*")

SNIPPET_TOKENS: Final = 28

#: Tokens this short are never distinctive enough to anchor a global quote search.
MIN_RARE_TOKEN_LENGTH: Final = 3

#: Cap on how many distinct tokens of a quote get a document-frequency lookup.
MAX_RARE_TOKEN_CANDIDATES: Final = 40


def escape_query(query: str) -> str:
    """Turn user input into a safe FTS5 MATCH expression.

    Every token is quoted, so punctuation and stray operators are searched for literally
    rather than being interpreted as query syntax.
    """
    tokens = _TOKEN_RE.findall(_FTS_SPECIAL.sub(" ", query))
    return " ".join(f'"{token}"' for token in tokens)


def _authority(connection: sqlite3.Connection, section_ids: set[str]) -> dict[str, int]:
    """Return how many distinct provisions cite each of the given sections."""
    if not section_ids:
        return {}
    placeholders = ",".join("?" * len(section_ids))
    rows = connection.execute(
        # The only interpolation is the placeholder list; the ids are bound.
        f"SELECT to_id, COUNT(DISTINCT from_id) AS n FROM refs "
        f"WHERE to_id IN ({placeholders}) GROUP BY to_id",
        tuple(sorted(section_ids)),
    ).fetchall()
    return {str(row["to_id"]): int(row["n"]) for row in rows}


def _ranks(order: list[int]) -> dict[int, int]:
    """Turn an ordering of candidate positions into one-based ranks."""
    return {position: rank for rank, position in enumerate(order, start=1)}


def search(
    connection: sqlite3.Connection,
    query: str,
    *,
    source: SourceType | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[SearchHit]:
    """Return the best-matching provisions for ``query``.

    Args:
        connection: An open index connection.
        query: Free text; treated as a bag of literal tokens, not FTS5 syntax.
        source: Restrict to the Code or the regulations; ``None`` searches both.
        limit: Maximum number of hits.
    """
    expression = escape_query(query)
    if not expression:
        return []
    sql = """
        SELECT p.id      AS id,
               p.source  AS source,
               p.section AS section,
               p.path    AS path,
               p.heading AS heading,
               LENGTH(p.text) AS text_length,
               bm25(provisions_fts) AS bm25,
               snippet(provisions_fts, 2, '**', '**', ' … ', ?) AS snippet
        FROM provisions_fts
        JOIN provisions p ON p.rowid = provisions_fts.rowid
        WHERE provisions_fts MATCH ?
    """
    params: list[object] = [SNIPPET_TOKENS, expression]
    if source is not None:
        sql += " AND p.source = ?"
        params.append(source.value)
    sql += " ORDER BY bm25 LIMIT ?"
    params.append(SEARCH_RERANK_CANDIDATES)

    rows = connection.execute(sql, params).fetchall()
    if not rows:
        return []

    sources = [SourceType(row["source"]) for row in rows]
    paths: list[list[str]] = [json.loads(row["path"]) for row in rows]
    section_ids = [normalize.canonical_id(sources[i], rows[i]["section"]) for i in range(len(rows))]
    authority = _authority(connection, set(section_ids))

    positions = range(len(rows))
    by_relevance = _ranks(sorted(positions, key=lambda i: (rows[i]["bm25"], rows[i]["id"])))
    by_authority = _ranks(
        sorted(positions, key=lambda i: (-authority.get(section_ids[i], 0), by_relevance[i]))
    )
    by_depth = _ranks(sorted(positions, key=lambda i: (len(paths[i]), by_relevance[i])))
    by_length = _ranks(
        sorted(positions, key=lambda i: (-(rows[i]["text_length"] or 0), by_relevance[i]))
    )

    def fused(i: int) -> float:
        return (
            1.0 / (SEARCH_RRF_K + by_relevance[i])
            + SEARCH_AUTHORITY_WEIGHT / (SEARCH_RRF_K + by_authority[i])
            + SEARCH_DEPTH_WEIGHT / (SEARCH_RRF_K + by_depth[i])
            + SEARCH_LENGTH_WEIGHT / (SEARCH_RRF_K + by_length[i])
        )

    scored = sorted(positions, key=lambda i: (-fused(i), rows[i]["id"]))

    hits: list[SearchHit] = []
    for i in scored[:limit]:
        row = rows[i]
        hit_source = sources[i]
        score = fused(i)
        hits.append(
            SearchHit(
                provision_id=row["id"],
                display=normalize.display_for(hit_source, row["section"], paths[i]),
                section=row["section"],
                source=hit_source,
                heading=row["heading"] or _section_heading(connection, hit_source, row["section"]),
                snippet=" ".join(str(row["snippet"]).split()),
                score=score,
            )
        )
    return hits


def _section_heading(
    connection: sqlite3.Connection, source: SourceType, section: str
) -> str | None:
    """Return the heading of the section containing a hit."""
    provision = db.get_provision(connection, normalize.canonical_id(source, section))
    return provision.heading if provision is not None else None


def rare_tokens(connection: sqlite3.Connection, text: str, count: int) -> list[str]:
    """Return the ``count`` rarest tokens of ``text``, by corpus document frequency.

    Used to build the global misattribution query for a quotation (SPEC 6.7 step 5).

    Document frequency comes from the FTS index rather than the ``provisions_vocab``
    table, because the vocabulary holds porter-stemmed terms ("kickback") while the
    caller has the words as written ("kickbacks"). Counting through a MATCH runs the
    query text through the same tokeniser and stemmer as the index did.
    """
    seen: set[str] = set()
    candidates: list[str] = []
    for token in _TOKEN_RE.findall(text):
        lowered = token.lower()
        if len(lowered) <= MIN_RARE_TOKEN_LENGTH or lowered in seen:
            continue
        seen.add(lowered)
        candidates.append(lowered)
    if not candidates:
        return []
    frequencies: dict[str, int] = {}
    for token in candidates[:MAX_RARE_TOKEN_CANDIDATES]:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM provisions_fts WHERE provisions_fts MATCH ?",
            (f'"{token}"',),
        ).fetchone()
        frequency = int(row["n"]) if row is not None else 0
        if frequency:
            frequencies[token] = frequency
    ranked = sorted(frequencies, key=lambda token: (frequencies[token], token))
    return ranked[:count]
