"""IRS guidance from the Internal Revenue Bulletin (roadmap 1.3).

Revenue Rulings, Revenue Procedures, Notices and Announcements are ordinary working
authority in tax practice — Treas. Reg. § 1.6662-4(d)(3)(iii) lists them — and they
were the largest category of citations TaxCite could only shrug at. The Bulletin is
published free at irs.gov and is a work of the United States government, so there is
no reason to shrug.

The markup is not stable across years: anchors are ``REV-PROC-2026-2`` in one year,
``RP-2015-1`` in another, and the heading whitespace moves around. Rather than track
those conventions, each article's **title** is run through TaxCite's own citation
parser. If the title parses as a guidance citation, that is the document, whatever the
surrounding HTML happens to look like this year.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from lxml import html as lxml_html

from taxcite.citations import extract_citations
from taxcite.config import IRB_BULLETIN_URL, IRB_INDEX_URL
from taxcite.errors import SourceNotFoundError, SourceParseError
from taxcite.models import SourceType
from taxcite.sources.http import HttpClient

logger: Final = logging.getLogger("taxcite.irb")

ProgressCallback = Callable[[str, int], None]

_WS_RE: Final = re.compile(r"\s+")

#: Guidance types the Bulletin publishes, mapped to the slug used in canonical ids.
KIND_SLUGS: Final[dict[str, str]] = {
    "Rev. Rul.": "rev-rul",
    "Rev. Proc.": "rev-proc",
    "Notice": "notice",
    "Ann.": "announcement",
    "T.D.": "td",
}

#: Headings that are bulletin furniture rather than guidance.
_FURNITURE: Final[frozenset[str]] = frozenset(
    {
        "the irs mission",
        "introduction",
        "definition of terms",
        "abbreviations",
        "numerical finding list",
        "finding list of current actions on previously published items",
    }
)


def canonical_id(kind: str, number: str) -> str:
    """Return the canonical identifier for a piece of published guidance."""
    slug = KIND_SLUGS.get(kind, kind.lower().replace(" ", "-").replace(".", ""))
    return f"/us/irs/{slug}/{number}"


def bulletin_url(year: int, week: int) -> str:
    """Return the URL of one weekly Bulletin."""
    return IRB_BULLETIN_URL.format(year=year, week=f"{week:02d}")


@dataclass(slots=True)
class GuidanceDocument:
    """One item published in the Bulletin."""

    id: str
    kind: str
    number: str
    title: str
    text: str
    bulletin: str
    url: str
    published: date | None = None
    acts_on: list[tuple[str, str]] = field(default_factory=list)
    status: str = "active"


def _articles(tree: lxml_html.HtmlElement) -> Iterator[lxml_html.HtmlElement]:
    """Yield every element the Bulletin marks as an article."""
    for element in tree.iter():
        classes = element.get("class")
        if classes and "article" in classes.split():
            yield element


def _clean(text: str) -> str:
    """Collapse whitespace."""
    return _WS_RE.sub(" ", text).strip()


def _title_of(article: lxml_html.HtmlElement) -> str:
    """Return an article's heading text."""
    for heading in article.iterfind(".//h2"):
        classes = heading.get("class") or ""
        if "title" in classes:
            text = _clean(heading.text_content())
            if text:
                return text
    return ""


def _identify(title: str) -> tuple[str, str] | None:
    """Read a guidance kind and number out of an article title.

    Uses the same parser the rest of TaxCite uses, so the Bulletin index and a
    citation in a memo agree on what a document is called.
    """
    if _clean(title).lower() in _FURNITURE:
        return None
    for citation in extract_citations(title):
        if (
            citation.source in {SourceType.IRS_GUIDANCE, SourceType.OTHER}
            and citation.guidance_type
            and citation.guidance_number
            and citation.guidance_type in KIND_SLUGS
        ):
            return citation.guidance_type, citation.guidance_number
    return None


#: The verbs the Bulletin uses when one document acts on an earlier one.
_RELATION_VERBS: Final = (
    r"supersed\w+|obsolet\w+|revok\w+|modif\w+|amplif\w+|clarif\w+"
    r"|supplement\w+|distinguish\w+"
)

#: A relationship, stated either way round. The window is deliberately short: a long
#: one picks up every ruling mentioned anywhere near the word "modified" and would
#: produce confident, wrong claims that a document had been superseded.
_RELATION_RES: Final[tuple[re.Pattern[str], ...]] = (
    # The window may contain full stops, because "Rev. Proc. 2014-40" does.
    re.compile(
        rf"(?P<cite>[^;\n]{{0,70}}?)\s+(?:is|are)\s+(?:hereby\s+)?"
        rf"(?P<verb>{_RELATION_VERBS})",
        re.IGNORECASE,
    ),
    re.compile(rf"(?P<verb>{_RELATION_VERBS})\s+(?P<cite>[^;\n]{{0,70}})", re.IGNORECASE),
)

_VERB_STEMS: Final[dict[str, str]] = {
    "supersed": "supersedes",
    "obsolet": "obsoletes",
    "revok": "revokes",
    "modif": "modifies",
    "amplif": "amplifies",
    "clarif": "clarifies",
    "supplement": "supplements",
    "distinguish": "distinguishes",
}


def _normalise_verb(verb: str) -> str:
    """Reduce a relation verb to a canonical stem."""
    lowered = verb.lower()
    for stem, canonical in _VERB_STEMS.items():
        if lowered.startswith(stem):
            return canonical
    return lowered


def _relations(text: str) -> list[tuple[str, str]]:
    """Return what this document says it does to earlier ones.

    The verb is kept, so a report can say "Rev. Proc. 2026-1 states that it supersedes
    this" rather than asserting as a fact that the earlier document is dead. A false
    "superseded" is the same class of error as a false citation.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern in _RELATION_RES:
        for match in pattern.finditer(text):
            verb = _normalise_verb(match.group("verb"))
            for citation in extract_citations(match.group("cite")):
                kind = citation.guidance_type
                if kind is None or kind not in KIND_SLUGS or not citation.guidance_number:
                    continue
                identifier = canonical_id(kind, citation.guidance_number)
                key = f"{identifier}:{verb}"
                if key in seen:
                    continue
                seen.add(key)
                found.append((identifier, verb))
    return found


def parse_bulletin(payload: bytes, bulletin: str, url: str) -> list[GuidanceDocument]:
    """Extract every guidance document from one Bulletin page.

    Raises:
        SourceParseError: if the payload is not parseable HTML.
    """
    try:
        tree = lxml_html.fromstring(payload)
    except Exception as exc:  # lxml raises several unrelated types on bad input
        raise SourceParseError(f"the Bulletin page could not be parsed: {exc}") from exc

    out: list[GuidanceDocument] = []
    seen: set[str] = set()
    for article in _articles(tree):
        title = _title_of(article)
        identified = _identify(title)
        if identified is None:
            continue
        kind, number = identified
        identifier = canonical_id(kind, number)
        if identifier in seen:
            continue
        seen.add(identifier)
        body = _clean(article.text_content())
        if body.startswith(title):
            body = body[len(title) :].strip()
        if not body:
            continue
        out.append(
            GuidanceDocument(
                id=identifier,
                kind=kind,
                number=number,
                title=title,
                text=body,
                bulletin=bulletin,
                url=f"{url}#{_anchor(article) or ''}".rstrip("#"),
                acts_on=_relations(body),
            )
        )
    return out


def _anchor(article: lxml_html.HtmlElement) -> str | None:
    """Return the article's named anchor, so a citation can deep-link to it."""
    for node in article.iterfind(".//a"):
        name = node.get("name")
        if name and not name.startswith("idm"):
            return str(name)
    return None


def fetch_bulletin(client: HttpClient, year: int, week: int) -> list[GuidanceDocument] | None:
    """Fetch and parse one weekly Bulletin, or ``None`` if there is no such issue."""
    url = bulletin_url(year, week)
    try:
        payload = client.get_bytes(url)
    except SourceNotFoundError:
        return None
    return parse_bulletin(payload, f"{year}-{week:02d}", url)


def iter_year(client: HttpClient, year: int, *, max_weeks: int = 53) -> Iterator[GuidanceDocument]:
    """Yield every guidance document the Bulletin published in a year.

    Stops after four consecutive misses, because the Bulletin skips weeks and the
    count per year is not fixed.
    """
    misses = 0
    for week in range(1, max_weeks + 1):
        documents = fetch_bulletin(client, year, week)
        if documents is None:
            misses += 1
            if misses >= 4:
                break
            continue
        misses = 0
        yield from documents


def index_documents(connection: sqlite3.Connection, documents: list[GuidanceDocument]) -> int:
    """Write guidance documents into the index."""
    from taxcite.index import db

    if not documents:
        return 0
    with db.transaction(connection):
        db.insert_guidance(
            connection,
            [
                (
                    doc.id,
                    doc.kind,
                    doc.number,
                    doc.title,
                    doc.text,
                    doc.bulletin,
                    doc.url,
                    doc.published.isoformat() if doc.published else None,
                    doc.status,
                )
                for doc in documents
            ],
        )
        db.insert_guidance_relations(
            connection,
            [(doc.id, target, verb) for doc in documents for target, verb in doc.acts_on],
        )
    return len(documents)


def build_years(
    connection: sqlite3.Connection,
    *,
    client: HttpClient,
    years: list[int],
    progress: ProgressCallback | None = None,
) -> tuple[int, str]:
    """Index every Bulletin published in the given years.

    Returns:
        The number of documents indexed, and the range covered.
    """
    total = 0
    batch: list[GuidanceDocument] = []
    for year in sorted(years):
        logger.info("indexing Internal Revenue Bulletins for %d", year)
        for document in iter_year(client, year):
            batch.append(document)
            if len(batch) >= 200:
                total += index_documents(connection, batch)
                batch.clear()
                if progress is not None:
                    progress("guidance documents", total)
        total += index_documents(connection, batch)
        batch.clear()
        if progress is not None:
            progress("guidance documents", total)
    covered = f"{min(years)}-{max(years)}" if years else ""
    return total, covered


def index_page(client: HttpClient) -> str:
    """Return the Bulletin index page, for discovering what is published."""
    return client.get_text(IRB_INDEX_URL, ttl=60 * 60 * 24)
