"""Treasury Regulations from the eCFR (SPEC 4.2, 6.4).

The eCFR serves 26 C.F.R. as flat ``<P>`` elements. Paragraph structure lives only in
the leading markers of each paragraph — ``(a)``, ``(1)``, ``(i)``, ``(A)``, and then
italic ``(1)`` and ``(i)`` again — so the tree has to be rebuilt from them, and the
markers are ambiguous: ``(i)`` is both the ninth lowercase letter and roman one.
:func:`assign_level` resolves that the way a reader does, from what is already open.

Three things about the live API that the spec did not anticipate are handled here and
recorded in docs/architecture.md: the endpoint refuses requests that do not accept a
compressed response, a whole part can be fetched in one request, and italic markers are
written ``(<I>1</I>)`` with the parentheses outside the emphasis.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Final

from lxml import etree

from taxcite.citations.levels import REG_LEVELS
from taxcite.citations.roman import is_lower_roman
from taxcite.config import ECFR_FULL_URL, ECFR_STRUCTURE_URL, ECFR_TITLES_URL
from taxcite.errors import SourceNotFoundError, SourceParseError, SourceUnavailableError
from taxcite.models import Provision, ProvisionStatus, SourceType
from taxcite.sources.http import HttpClient

logger: Final = logging.getLogger("taxcite.ecfr")

ProgressCallback = Callable[[str, int], None]

CFR_TITLE: Final = 26

#: Sentinels that mark italic runs while the paragraph is a flat string.
_ITALIC_OPEN: Final = "\x01"
_ITALIC_CLOSE: Final = "\x02"

#: One leading paragraph marker, with or without italics inside the parentheses.
_MARKER_RE: Final = re.compile(rf"\s*\(\s*{_ITALIC_OPEN}?([0-9A-Za-z]{{1,6}}){_ITALIC_CLOSE}?\s*\)")

#: An italic run-in heading that follows a marker, plus the "s—" style tail that
#: sometimes sits between the heading and the next marker.
_HEADING_RE: Final = re.compile(
    rf"\s*{_ITALIC_OPEN}(?P<heading>[^{_ITALIC_CLOSE}]*){_ITALIC_CLOSE}(?P<tail>[^\s(]{{0,4}})"
)

_RESERVED_RE: Final = re.compile(r"\[\s*reserved\s*\]", re.IGNORECASE)
_HEAD_NUM_RE: Final = re.compile(r"^\s*§+\s*\S+\s*")
_WS_RE: Final = re.compile(r"\s+")

#: eCFR elements that are editorial apparatus rather than regulatory text.
SKIP_TAGS: Final[frozenset[str]] = frozenset(
    {"CITA", "SECAUTH", "EDNOTE", "EFFDNOT", "AUTH", "SOURCE", "HEAD", "APPRO"}
)

#: Level numbers, outermost first, as SPEC 6.4 names them.
L1, L2, L3, L4, L5, L6 = 1, 2, 3, 4, 5, 6
MAX_LEVEL: Final = L6

#: The first value at each level.
_FIRST: Final[dict[int, str]] = {L1: "a", L2: "1", L3: "i", L4: "A", L5: "1", L6: "i"}


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------


def latest_date(client: HttpClient) -> str:
    """Return the date Title 26 of the eCFR is current as of.

    Raises:
        SourceUnavailableError: if the titles endpoint does not describe Title 26.
    """
    payload = client.get_json(ECFR_TITLES_URL, ttl=60 * 60 * 6)
    for title in payload.get("titles", []):
        if title.get("number") == CFR_TITLE:
            date = title.get("up_to_date_as_of") or title.get("latest_issue_date")
            if date:
                logger.info("eCFR Title 26 is current as of %s", date)
                return str(date)
    raise SourceUnavailableError(
        "the eCFR titles endpoint did not describe Title 26",
        hint="Check https://www.ecfr.gov/api/versioner/v1/titles.json by hand.",
    )


def part_of(section: str) -> str:
    """Return the CFR part a section number belongs to: ``1.162-1`` is in part ``1``."""
    return section.split(".", 1)[0]


def fetch_section(client: HttpClient, date: str, section: str) -> bytes:
    """Fetch one regulation section as eCFR XML."""
    return client.get_bytes(
        ECFR_FULL_URL.format(date=date),
        params={"part": part_of(section), "section": section},
    )


def fetch_part(client: HttpClient, date: str, part: str) -> bytes:
    """Fetch a whole CFR part as eCFR XML, in one request."""
    return client.get_bytes(ECFR_FULL_URL.format(date=date), params={"part": part})


def part_sections(client: HttpClient, date: str, part: str) -> list[str]:
    """Return every section number in a part, from the structure endpoint."""
    payload = structure_json(client, date)
    found: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "section":
            identifier = str(node.get("identifier") or "")
            if identifier and part_of(identifier) == part:
                found.append(identifier)
        for child in node.get("children") or []:
            walk(child)

    walk(payload)
    return found


# --------------------------------------------------------------------------------------
# Paragraph hierarchy (SPEC 6.4)
# --------------------------------------------------------------------------------------


def _charsets(token: str, *, italic: bool) -> list[int]:
    """Return every level whose shape and emphasis a marker could belong to."""
    out: list[int] = []
    if italic:
        if token.isdigit():
            out.append(L5)
        if is_lower_roman(token):
            out.append(L6)
        return out
    if re.fullmatch(r"[a-z]{1,2}", token):
        out.append(L1)
    if token.isdigit():
        out.append(L2)
    if is_lower_roman(token):
        out.append(L3)
    if re.fullmatch(r"[A-Z]{1,2}", token):
        out.append(L4)
    return out


def _successor(token: str, level: int) -> str | None:
    """Return the marker that would follow ``token`` at ``level``."""
    if level in {L2, L5}:
        return str(int(token) + 1) if token.isdigit() else None
    if level in {L3, L6}:
        from taxcite.citations.roman import int_to_roman, roman_to_int

        if not is_lower_roman(token):
            return None
        return int_to_roman(roman_to_int(token) + 1)
    if level in {L1, L4}:
        upper = level == L4
        expected = r"[A-Z]{1,2}" if upper else r"[a-z]{1,2}"
        if not re.fullmatch(expected, token):
            return None
        last = "Z" if upper else "z"
        if len(set(token)) != 1:
            return None
        if token[-1] == last:
            first = "A" if upper else "a"
            return first * (len(token) + 1)
        return chr(ord(token[0]) + 1) * len(token)
    return None


def assign_level(token: str, *, italic: bool, stack: list[tuple[int, str]]) -> int | None:
    """Decide which level a paragraph marker belongs to (SPEC 6.4).

    ``(i)`` after ``(h)`` continues the lettered level; ``(i)`` after ``(2)`` opens a
    roman level one deeper. The rules, in order: the marker continues an open level, or
    it is the first value of the next level down, or it is the deepest level its shape
    allows that does not skip a level.

    Args:
        token: The marker text, without its parentheses.
        italic: Whether the marker was emphasised, which distinguishes L5/L6 from L2/L3.
        stack: The levels currently open, outermost first, as ``(level, marker)``.

    Returns:
        The level, or ``None`` if the marker fits no level at all.
    """
    candidates = _charsets(token, italic=italic)
    if not candidates:
        return None
    open_at = dict(stack)

    for level in candidates:
        current = open_at.get(level)
        if current is not None and _successor(current, level) == token:
            return level

    depth = stack[-1][0] if stack else 0
    for level in candidates:
        if token == _FIRST[level] and level == depth + 1:
            return level

    allowed = [level for level in candidates if level <= depth + 1]
    if allowed:
        return max(allowed)
    return min(candidates)


@dataclass(slots=True)
class _Node:
    """One reconstructed paragraph."""

    path: list[str]
    level: int
    text: list[str] = field(default_factory=list)
    heading: str | None = None


def _flatten(element: etree._Element) -> str:
    """Render a ``<P>`` as text, wrapping italic runs in sentinels."""
    parts: list[str] = []

    def walk(node: etree._Element, *, italic: bool) -> None:
        tag = str(node.tag) if isinstance(node.tag, str) else ""
        emphasised = italic or tag == "I"
        if node.text:
            parts.append(f"{_ITALIC_OPEN}{node.text}{_ITALIC_CLOSE}" if emphasised else node.text)
        for child in node:
            walk(child, italic=emphasised)
            if child.tail:
                parts.append(f"{_ITALIC_OPEN}{child.tail}{_ITALIC_CLOSE}" if italic else child.tail)

    if element.text:
        parts.append(element.text)
    for child in element:
        walk(child, italic=False)
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _strip_sentinels(text: str) -> str:
    """Remove the italic sentinels and collapse whitespace."""
    return _WS_RE.sub(" ", text.replace(_ITALIC_OPEN, "").replace(_ITALIC_CLOSE, "")).strip()


@dataclass(frozen=True, slots=True)
class Marker:
    """A paragraph marker and the run-in heading that follows it, if any."""

    token: str
    italic: bool
    heading: str | None = None


def split_markers(flat: str) -> tuple[list[Marker], str]:
    """Split a flattened paragraph into its leading markers and the rest.

    A marker is often followed by an italic run-in heading — "(b) *Cross references.*"
    — which belongs to the paragraph the marker opens, not to its text.

    Returns:
        The markers, and the remaining text.
    """
    markers: list[Marker] = []
    position = 0
    while True:
        match = _MARKER_RE.match(flat, position)
        if match is None:
            break
        token = match.group(1)
        italic = _ITALIC_OPEN in match.group(0)
        position = match.end()
        heading: str | None = None
        found = _HEADING_RE.match(flat, position)
        if found is not None:
            heading = _clean_heading(found.group("heading") + found.group("tail"))
            position = found.end()
        markers.append(Marker(token=token, italic=italic, heading=heading))
    return markers, flat[position:]


def _clean_heading(text: str) -> str | None:
    """Tidy a run-in heading: collapse whitespace and drop the trailing punctuation."""
    cleaned = _WS_RE.sub(" ", text).strip().rstrip(".\u2014-\u2013 ").strip()
    return cleaned or None


# --------------------------------------------------------------------------------------
# Section parsing
# --------------------------------------------------------------------------------------


#: The source credit the eCFR prints at the foot of a section, which is where a
#: regulation's issuance date lives: "[T.D. 8175, 53 FR 5725, Feb. 25, 1988, ...]".
_CITA_DATE_RE: Final = re.compile(
    r"\b(?P<month>[A-Z][a-z]{2})[a-z]*\.?\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})"
)

_MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def first_issued(credit: str) -> dt.date | None:
    """Return the earliest date in a source credit: when the section was issued.

    Later dates in the credit are amendments. For I.R.C. § 7805(e), which runs a
    three-year clock from issuance, the first one is the one that matters.
    """
    found: list[dt.date] = []
    for match in _CITA_DATE_RE.finditer(credit):
        month = _MONTHS.get(match.group("month").lower())
        if month is None:
            continue
        try:
            found.append(dt.date(int(match.group("year")), month, int(match.group("day"))))
        except ValueError:  # pragma: no cover - the credit is machine-generated
            continue
    return min(found) if found else None


@dataclass(slots=True)
class ParsedRegSection:
    """A regulation section reconstructed into provisions."""

    section: str
    provisions: list[Provision] = field(default_factory=list)
    source_credit: str = ""
    issued: dt.date | None = None


def _section_status(heading: str, body: str) -> ProvisionStatus:
    """Return ``reserved`` for a section the eCFR marks as such."""
    if _RESERVED_RE.search(heading) or (not body.strip() and _RESERVED_RE.search(body)):
        return "reserved"
    return "active"


def parse_section_element(element: etree._Element, ecfr_date: str) -> ParsedRegSection:
    """Turn one ``<DIV8 TYPE="SECTION">`` into provisions.

    Raises:
        SourceParseError: if the element carries no section number.
    """
    section = element.get("N")
    if not section:
        raise SourceParseError("eCFR section element has no N attribute")
    head = ""
    credit = ""
    for child in element:
        tag = str(child.tag)
        if tag == "HEAD" and not head:
            head = _strip_sentinels(_flatten(child))
        elif tag == "CITA" and not credit:
            credit = _strip_sentinels(_flatten(child)).strip("[]")
    heading = _HEAD_NUM_RE.sub("", head).strip()

    nodes: dict[tuple[str, ...], _Node] = {}
    order: list[tuple[str, ...]] = []
    root = _Node(path=[], level=0)
    stack: list[tuple[int, str]] = []
    current: _Node = root

    for child in element:
        tag = str(child.tag) if isinstance(child.tag, str) else ""
        if tag in SKIP_TAGS or tag == "DIV8":
            continue
        flat = _flatten(child)
        markers, rest = split_markers(flat)
        if not markers:
            body = _strip_sentinels(flat)
            if body:
                current.text.append(body)
            continue
        for index, marker in enumerate(markers):
            level = assign_level(marker.token, italic=marker.italic, stack=stack)
            if level is None:
                continue
            stack = [entry for entry in stack if entry[0] < level]
            parent_path = [token for _level, token in stack]
            stack.append((level, marker.token))
            path = [*parent_path, marker.token]
            key = tuple(path)
            node = nodes.get(key)
            if node is None:
                node = _Node(path=path, level=level)
                nodes[key] = node
                order.append(key)
            if marker.heading and node.heading is None:
                node.heading = marker.heading
            current = node
            if index == len(markers) - 1:
                body = _strip_sentinels(rest)
                if body:
                    current.text.append(body)
    section_text = " ".join(root.text).strip()
    parsed = ParsedRegSection(section=section, source_credit=credit, issued=first_issued(credit))
    status = _section_status(head, section_text)
    section_id = f"/us/cfr/t26/s{section}"
    parsed.provisions.append(
        Provision(
            id=section_id,
            source=SourceType.REG,
            section=section,
            path=[],
            level="section",
            num=section,
            heading=heading or None,
            text=section_text,
            full_text="",
            parent_id=None,
            status=status,
            source_version=ecfr_date,
        )
    )
    for key in order:
        node = nodes[key]
        parent = f"/us/cfr/t26/s{section}" + "".join(f"/{part}" for part in node.path[:-1])
        parsed.provisions.append(
            Provision(
                id=section_id + "".join(f"/{part}" for part in node.path),
                source=SourceType.REG,
                section=section,
                path=list(node.path),
                level=REG_LEVELS[min(len(node.path), len(REG_LEVELS)) - 1],
                num=f"({node.path[-1]})",
                heading=node.heading,
                text=" ".join(node.text).strip(),
                full_text="",
                parent_id=parent,
                status=status,
                source_version=ecfr_date,
            )
        )
    _compose_full_text(parsed.provisions)
    return parsed


def _compose_full_text(provisions: list[Provision]) -> None:
    """Fill in ``full_text`` bottom-up, in document order."""
    children: dict[str, list[Provision]] = {}
    for provision in provisions:
        if provision.parent_id is not None:
            children.setdefault(provision.parent_id, []).append(provision)

    def compose(provision: Provision) -> str:
        parts = [provision.text]
        parts.extend(compose(child) for child in children.get(provision.id, []))
        provision.full_text = _WS_RE.sub(" ", " ".join(part for part in parts if part)).strip()
        return provision.full_text

    for provision in provisions:
        if provision.parent_id is None:
            compose(provision)


def parse_sections(xml: bytes, ecfr_date: str) -> Iterator[ParsedRegSection]:
    """Parse every section in an eCFR XML document.

    Raises:
        SourceParseError: if the payload is not XML at all.
    """
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(huge_tree=True, recover=True))
    except etree.XMLSyntaxError as exc:
        raise SourceParseError(f"eCFR returned XML that could not be parsed: {exc}") from exc
    if root is None:  # pragma: no cover - recover=True keeps a root
        raise SourceParseError("eCFR returned an empty document")
    elements = [root] if root.get("TYPE") == "SECTION" else root.findall(".//DIV8[@TYPE='SECTION']")
    for element in elements:
        yield parse_section_element(element, ecfr_date)


# --------------------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------------------


def _insert(
    connection: sqlite3.Connection,
    provisions: list[Provision],
    credits: list[tuple[str, int, str, str | None, str, str, str | None]] | None = None,
) -> None:
    """Write a batch of regulation provisions and their source credits."""
    from taxcite.index import db
    from taxcite.sources.uscode import ordinal_of

    with db.transaction(connection):
        db.insert_provisions(connection, provisions, ordinal_of(provisions))
        if credits:
            db.insert_notes(connection, credits)


def credit_rows(
    parsed: ParsedRegSection,
) -> list[tuple[str, int, str, str | None, str, str, str | None]]:
    """Render a section's source credit as a note row.

    A credit is not regulatory text, so it goes where the statutory notes go, and it
    carries the issuance date that I.R.C. § 7805(e) runs its three-year clock from.
    """
    if not parsed.source_credit:
        return []
    import json

    return [
        (
            f"/us/cfr/t26/s{parsed.section}",
            0,
            "sourceCredit",
            None,
            parsed.source_credit,
            json.dumps([parsed.issued.isoformat()] if parsed.issued else []),
            None,
        )
    ]


def index_section(connection: sqlite3.Connection, parsed: ParsedRegSection) -> int:
    """Insert one parsed regulation section; return how many provisions were written."""
    _insert(connection, parsed.provisions, credit_rows(parsed))
    return len(parsed.provisions)


def build_parts(
    connection: sqlite3.Connection,
    *,
    client: HttpClient,
    parts: list[str],
    progress: ProgressCallback | None = None,
    date: str | None = None,
) -> tuple[int, int, int, str]:
    """Bulk-index whole CFR parts.

    One request per part rather than one per section: part 1 alone holds about 3,900
    sections, and asking for them one at a time would mean 3,900 requests to a public
    server for data it will happily send in a single response.

    Returns:
        Section count, provision count, ref count (always zero here, the post-pass
        builds them), and the eCFR date indexed.
    """
    ecfr_date = date or latest_date(client)
    sections = provisions = 0
    for part in parts:
        logger.info("fetching 26 CFR part %s", part)
        xml = fetch_part(client, ecfr_date, part)
        batch: list[Provision] = []
        credits: list[tuple[str, int, str, str | None, str, str, str | None]] = []
        for parsed in parse_sections(xml, ecfr_date):
            sections += 1
            batch.extend(parsed.provisions)
            credits.extend(credit_rows(parsed))
            if len(batch) >= 2000:
                _insert(connection, batch, credits)
                provisions += len(batch)
                batch.clear()
                credits.clear()
                if progress is not None:
                    progress("regulation sections", sections)
        if batch:
            _insert(connection, batch, credits)
            provisions += len(batch)
        if progress is not None:
            progress("regulation sections", sections)
    return sections, provisions, 0, ecfr_date


def make_fetcher(client: HttpClient, *, date: str | None = None) -> Callable[..., bool]:
    """Build the write-through cache used to fetch a regulation on demand.

    The returned callable matches :class:`~taxcite.verify.resolver.RegFetcher`. It
    returns ``False`` when the eCFR says the section does not exist, and lets a
    :class:`~taxcite.errors.SourceUnavailableError` propagate when the eCFR could not
    be reached — the caller must be able to tell those apart.
    """
    state: dict[str, str] = {}
    if date:
        state["date"] = date

    def fetch(connection: sqlite3.Connection, section: str) -> bool:
        if "date" not in state:
            state["date"] = latest_date(client)
        try:
            xml = fetch_section(client, state["date"], section)
        except SourceNotFoundError:
            logger.info("26 CFR %s does not exist in the eCFR", section)
            return False
        except SourceParseError as exc:
            logger.info("26 CFR %s could not be parsed: %s", section, exc)
            return False
        written = 0
        for parsed in parse_sections(xml, state["date"]):
            written += index_section(connection, parsed)
        if written:
            from taxcite.index import db

            db.rebuild_fts(connection)
        return written > 0

    return fetch


def structure_json(client: HttpClient, date: str) -> Any:
    """Return the Title 26 table of contents for a date."""
    return client.get_json(ECFR_STRUCTURE_URL.format(date=date), ttl=60 * 60 * 24)


__all__ = [
    "CFR_TITLE",
    "Marker",
    "ParsedRegSection",
    "assign_level",
    "build_parts",
    "fetch_part",
    "fetch_section",
    "index_section",
    "latest_date",
    "make_fetcher",
    "parse_section_element",
    "parse_sections",
    "part_of",
    "part_sections",
    "split_markers",
    "structure_json",
]
