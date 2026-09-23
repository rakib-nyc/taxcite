"""Defined terms (SPEC 6.9).

Tax law defines its own vocabulary constantly, and the same phrase can mean different
things in different chapters. Every definition therefore carries its scope — "for
purposes of this section", "this subtitle", "this title" — so a reader can tell whether
the definition they found is the one that governs them.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

from taxcite.citations import normalize as cite_norm
from taxcite.config import DEFINITION_TEXT_MAX_CHARS
from taxcite.index import db
from taxcite.index.search import escape_query
from taxcite.models import Definition, SourceType

logger: Final = logging.getLogger("taxcite.definitions")

ProgressCallback = Callable[[str, int], None]

#: The section that defines terms for the whole of Title 26.
GENERAL_DEFINITIONS_SECTION: Final = "7701"

_QUOTED: Final = r"[“\"]\s*(?P<{name}>[^“”\"]{{1,120}}?)\s*[”\"]"

#: "the term X means", "the terms X and Y mean", "X means", "X has the meaning given".
_DEFINITION_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\bthe terms?\s+"
        + _QUOTED.format(name="first")
        + r"(?:\s+and\s+"
        + _QUOTED.format(name="second")
        + r")?\s*"
        r"(?P<verb>means|mean|includes|include|shall mean|shall include"
        r"|has the meaning|have the meaning|does not include|shall not include)\b",
        re.IGNORECASE,
    ),
    re.compile(
        _QUOTED.format(name="first")
        + r"\s*(?P<verb>means|includes|shall mean|shall include|has the meaning)\b",
        re.IGNORECASE,
    ),
)

#: Function words that never begin a defined term, so that "such property means" and
#: "which income includes" do not become definitions.
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "as",
        "at",
        "be",
        "but",
        "by",
        "each",
        "for",
        "he",
        "her",
        "him",
        "his",
        "if",
        "in",
        "is",
        "it",
        "its",
        "no",
        "nor",
        "not",
        "of",
        "on",
        "or",
        "other",
        "she",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "those",
        "to",
        "was",
        "were",
        "when",
        "which",
        "who",
        "whom",
        "with",
        "term",
        "terms",
        "section",
        "subsection",
        "paragraph",
        "subparagraph",
        "clause",
        "sentence",
        "chapter",
        "subchapter",
        "part",
        "title",
        "subtitle",
        "purposes",
        "purpose",
        "case",
        "year",
        "period",
        "amount",
        "value",
        "time",
        "date",
        "extent",
        "meaning",
        "definition",
    }
)

#: Older drafting defines a term without quotation marks: "gross income means all
#: income from whatever source derived". Restricted to a short lowercase noun phrase
#: that starts a clause, because the unquoted form is otherwise far too greedy.
_UNQUOTED_RE: Final = re.compile(
    r"(?:^|(?<=[,;:])|(?<=\u2014))\s*"
    r"(?P<first>[a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,4})\s+"
    r"(?P<verb>means|shall mean)\b"
)

_BY_REFERENCE_RE: Final = re.compile(
    r"\bhas?v?e?\s+the\s+meaning\s+given\b[^.;]{0,120}", re.IGNORECASE
)

#: Scope phrases, longest first so "this subtitle" wins over a bare "this".
_SCOPE_RE: Final = re.compile(
    r"\b(?:for\s+purposes\s+of|when\s+used\s+in|as\s+used\s+in)\s+(?P<scope>"
    r"this\s+(?:subsubparagraph|subparagraph|paragraph|subsection|section|part|"
    r"subchapter|chapter|subtitle|title|subpart)"
    r"|(?:paragraph|subparagraph|subsection|section)\s+\([0-9A-Za-z]{1,6}\)"
    r"|this\s+\w+)",
    re.IGNORECASE,
)

_WS_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s]+")


def normalize_term(term: str) -> str:
    """Return the lookup form of a defined term: lowercase, unpunctuated, collapsed."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", term.casefold())).strip()


def extract_definitions(text: str) -> Iterator[tuple[str, str, str | None, str | None]]:
    """Find every defined term in ``text``.

    Yields:
        ``(term, definition_text, scope, by_reference)`` for each term defined.
    """
    seen: set[tuple[str, int]] = set()
    quoted_spans: list[tuple[int, int]] = []
    for pattern in _DEFINITION_RES:
        for match in pattern.finditer(text):
            # The two quoted patterns overlap: "the term X means" also matches the
            # bare "X means" form, and only the longer reading is right.
            if any(low <= match.start("first") < high for low, high in quoted_spans):
                continue
            terms = [match.group("first")]
            if "second" in match.groupdict() and match.group("second"):
                terms.append(match.group("second"))
            body = text[match.start() : match.start() + DEFINITION_TEXT_MAX_CHARS].strip()
            scope = _scope_before(text, match.start())
            reference = _BY_REFERENCE_RE.search(text[match.start() : match.end() + 160])
            quoted_spans.append(match.span())
            for term in terms:
                cleaned = term.strip().strip(",;:")
                key = (normalize_term(cleaned), match.start())
                if not cleaned or key in seen:
                    continue
                seen.add(key)
                yield cleaned, body, scope, reference.group(0) if reference else None

    for match in _UNQUOTED_RE.finditer(text):
        if any(low <= match.start("first") < high for low, high in quoted_spans):
            continue
        term = match.group("first").strip()
        words = term.split()
        if not words or words[0] in _STOP_WORDS or words[-1] in _STOP_WORDS:
            continue
        key = (normalize_term(term), match.start())
        if key in seen:
            continue
        seen.add(key)
        start = match.start("first")
        body = text[start : start + DEFINITION_TEXT_MAX_CHARS].strip()
        yield term, body, _scope_before(text, start), None


def _scope_before(text: str, position: int) -> str | None:
    """Return the scope phrase governing a definition, if one precedes it."""
    window = text[max(0, position - 240) : position]
    matches = list(_SCOPE_RE.finditer(window))
    if not matches:
        return None
    return _WS_RE.sub(" ", matches[-1].group("scope")).strip().lower()


def scope_map(connection: sqlite3.Connection) -> dict[str, str]:
    """Return the scope phrase governing each provision, inherited from its ancestors.

    A definition usually sits under a chapeau that sets the scope once — "When used in
    this title" at § 7701(a), with the definitions themselves in § 7701(a)(1) and down.
    Without inheritance every one of those would look unscoped.
    """
    parents: dict[str, str | None] = {}
    own: dict[str, str] = {}
    for row in connection.execute("SELECT id, parent_id, text FROM provisions"):
        identifier = str(row["id"])
        parents[identifier] = row["parent_id"]
        found = _SCOPE_RE.search(str(row["text"] or ""))
        if found:
            own[identifier] = _WS_RE.sub(" ", found.group("scope")).strip().lower()

    resolved: dict[str, str] = {}

    def resolve(identifier: str) -> str | None:
        if identifier in resolved:
            return resolved[identifier]
        scope = own.get(identifier)
        if scope is None:
            parent = parents.get(identifier)
            scope = resolve(parent) if parent else None
        if scope is not None:
            resolved[identifier] = scope
        return scope

    for identifier in parents:
        resolve(identifier)
    return resolved


def build_definitions(
    connection: sqlite3.Connection, *, progress: ProgressCallback | None = None
) -> int:
    """Extract every defined term in the index into the ``definitions`` table.

    Returns:
        The number of definitions written.
    """
    inherited = scope_map(connection)
    rows = connection.execute(
        "SELECT id, text FROM provisions WHERE text LIKE '%term%' OR text LIKE '%means%'"
    ).fetchall()
    batch: list[tuple[str, str, str, str | None, str, str | None]] = []
    written = 0
    for position, row in enumerate(rows, start=1):
        provision_id = str(row["id"])
        for term, body, scope, reference in extract_definitions(str(row["text"])):
            governing = scope or inherited.get(provision_id)
            batch.append((normalize_term(term), term, provision_id, governing, body, reference))
        if len(batch) >= 2000:
            written += _flush(connection, batch)
            batch.clear()
            if progress is not None:
                progress("definitions", position)
    written += _flush(connection, batch)
    if progress is not None:
        progress("definitions", len(rows))
    logger.info("wrote %d definitions", written)
    return written


def _flush(
    connection: sqlite3.Connection,
    batch: list[tuple[str, str, str, str | None, str, str | None]],
) -> int:
    """Write a batch of definitions in one transaction, keeping ids unique."""
    if not batch:
        return 0
    unique: dict[tuple[str, str], tuple[str, str, str, str | None, str, str | None]] = {}
    for entry in batch:
        unique.setdefault((entry[0], entry[2]), entry)
    with db.transaction(connection):
        db.insert_definitions(connection, list(unique.values()))
    return len(unique)


# --------------------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------------------


def find(connection: sqlite3.Connection, term: str, *, limit: int = 5) -> list[Definition]:
    """Look up a defined term.

    Exact matches on the normalised term come first; if there are none, the term index
    is searched. Within each group, a definition that governs the whole title outranks
    one that governs only a paragraph, and § 7701 — the Code's general definitions
    section — is preferred for title-wide scope.
    """
    rows = connection.execute(
        "SELECT d.*, p.source, p.section, p.path FROM definitions d "
        "JOIN provisions p ON p.id = d.provision_id WHERE d.term_norm = ? ",
        (normalize_term(term),),
    ).fetchall()
    if not rows:
        rows = _search_terms(connection, term)
    definitions = [_to_definition(row) for row in rows]
    headings = _section_headings(connection, definitions)
    definitions.sort(key=lambda d: _rank(d, headings))
    return definitions[:limit]


def _section_headings(
    connection: sqlite3.Connection, definitions: list[Definition]
) -> dict[str, str | None]:
    """Return the owning section's heading for each definition."""
    out: dict[str, str | None] = {}
    cache: dict[str, str | None] = {}
    for definition in definitions:
        source, section, _ = cite_norm.split_canonical_id(definition.provision_id)
        section_id = cite_norm.canonical_id(source, section)
        if section_id not in cache:
            provision = db.get_provision(connection, section_id)
            cache[section_id] = provision.heading if provision is not None else None
        out[definition.provision_id] = cache[section_id]
    return out


def _search_terms(connection: sqlite3.Connection, term: str) -> list[sqlite3.Row]:
    """Fall back to a full-text search over the defined terms."""
    expression = escape_query(term)
    if not expression:
        return []
    return list(
        connection.execute(
            "SELECT d.*, p.source, p.section, p.path FROM definitions_fts f "
            "JOIN definitions d ON d.term_norm = f.term_norm "
            "JOIN provisions p ON p.id = d.provision_id "
            "WHERE definitions_fts MATCH ? "
            "ORDER BY bm25(definitions_fts) LIMIT 100",
            (expression,),
        ).fetchall()
    )


#: How widely a scope reaches, largest first.
_SCOPE_RANK: Final[dict[str, int]] = {
    "this title": 0,
    "this subtitle": 1,
    "this chapter": 2,
    "this subchapter": 3,
    "this part": 4,
    "this subpart": 5,
    "this section": 6,
    "this subsection": 7,
    "this paragraph": 8,
    "this subparagraph": 9,
}


def _rank(
    definition: Definition, headings: dict[str, str | None]
) -> tuple[int, int, int, int, int, str]:
    """Rank definitions for a reader looking for the one that governs them.

    Breadth of scope first, because a title-wide definition beats one that governs a
    single paragraph; then whether the section is *about* the term, which is what
    "Gross income defined" tells you; then how shallow the provision is, since the
    operative definition is rarely five levels down.
    """
    scope_rank = _SCOPE_RANK.get(definition.scope or "", 10)
    section = definition.provision_id.rsplit("/s", 1)[-1].split("/")[0]
    general = 0 if section == GENERAL_DEFINITIONS_SECTION and scope_rank <= 1 else 1
    heading = (headings.get(definition.provision_id) or "").casefold()
    names_the_term = 0 if definition.term_norm in normalize_term(heading) else 1
    depth = definition.provision_id.count("/")
    by_reference = 1 if definition.by_reference else 0
    return (general, names_the_term, scope_rank, by_reference, depth, definition.provision_id)


def _to_definition(row: sqlite3.Row) -> Definition:
    """Build a :class:`Definition` from a joined definitions row."""
    source = SourceType(row["source"])
    path: list[str] = json.loads(row["path"])
    return Definition(
        term_display=row["term_display"],
        term_norm=row["term_norm"],
        provision_id=row["provision_id"],
        display=cite_norm.display_for(source, row["section"], path),
        scope=row["scope"],
        definition_text=row["definition_text"],
        by_reference=row["by_reference"],
    )


# --------------------------------------------------------------------------------------
# Scope conflicts (roadmap 2.6)
# --------------------------------------------------------------------------------------

#: How far each scope phrase reaches, as a canonical-id prefix test.
_REACH: Final[dict[str, str]] = {
    "this title": "title",
    "this subtitle": "subtitle",
    "this chapter": "chapter",
    "this subchapter": "chapter",
    "this part": "chapter",
    "this subpart": "chapter",
    "this section": "section",
    "this subsection": "subsection",
    "this paragraph": "paragraph",
    "this subparagraph": "paragraph",
}


#: A scope naming a specific subdivision — "paragraph (2)", "subsection (b)" — is
#: local to the section that states it, whatever the subdivision.
_NAMED_SUBDIVISION_RE: Final = re.compile(
    r"^(?:section|subsection|paragraph|subparagraph|clause)\s+\([0-9A-Za-z]{1,6}\)$",
    re.IGNORECASE,
)


def _reach_of(scope: str | None) -> str:
    """Return how far a scope phrase reaches."""
    if not scope:
        return "unknown"
    if scope in _REACH:
        return _REACH[scope]
    if _NAMED_SUBDIVISION_RE.match(scope):
        return "section"
    return "unknown"


@dataclass(slots=True)
class ScopeConflict:
    """A definition a reader may have relied on outside the range it governs."""

    term: str
    definition: Definition
    used_at: str
    used_display: str
    reason: str


def reaches(definition: Definition, provision_id: str) -> bool:
    """Return ``True`` if a definition governs the provision at ``provision_id``.

    Only the scopes that can be checked from a canonical identifier are checked. "This
    title" reaches everything in Title 26; "this section" reaches only its own section.
    The intermediate scopes — subtitle, chapter, part — are not encoded in the
    identifier, so they are treated as reaching, and the caller is told nothing rather
    than told something false.
    """
    reach = _reach_of(definition.scope)
    if reach in {"title", "subtitle", "chapter", "unknown"}:
        return True
    try:
        source, section, path = cite_norm.split_canonical_id(provision_id)
        _dsource, dsection, dpath = cite_norm.split_canonical_id(definition.provision_id)
    except ValueError:  # pragma: no cover - callers pass canonical ids
        return True
    if section != dsection or source.value != _dsource.value:
        return False
    if reach == "section":
        return True
    depth = {"subsection": 1, "paragraph": 2}[reach]
    return path[:depth] == dpath[:depth]


def scope_conflicts(
    connection: sqlite3.Connection,
    term: str,
    used_at: str,
    *,
    limit: int = 5,
) -> list[ScopeConflict]:
    """Return definitions of ``term`` that do **not** govern ``used_at``.

    Tax law defines the same phrase differently in different places, and a definition
    borrowed from a section that does not govern yours is a real and common mistake —
    "trade or business" as § 513(c) defines it says nothing about § 162.
    """
    out: list[ScopeConflict] = []
    for definition in find(connection, term, limit=limit):
        if reaches(definition, used_at):
            continue
        source, section, path = cite_norm.split_canonical_id(used_at)
        out.append(
            ScopeConflict(
                term=term,
                definition=definition,
                used_at=used_at,
                used_display=cite_norm.display_for(source, section, path),
                reason=(
                    f"{definition.display} defines “{definition.term_display}” "
                    f"for purposes of {definition.scope}, which does not reach "
                    f"{cite_norm.display_for(source, section, path)}"
                ),
            )
        )
    return out


def governing(
    connection: sqlite3.Connection, term: str, provision_id: str, *, limit: int = 5
) -> list[Definition]:
    """Return the definitions of ``term`` that actually govern ``provision_id``."""
    return [d for d in find(connection, term, limit=limit) if reaches(d, provision_id)]
