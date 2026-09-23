"""Internal Revenue Code from the U.S. Code USLM XML (SPEC 4.1).

The Office of the Law Revision Counsel publishes Title 26 as a single USLM XML document
inside a per-release-point zip. Every element already carries the identifier TaxCite
uses as its canonical id, so parsing is mostly a matter of walking the hierarchy and
separating statutory text from the editorial notes that surround it.

The release point is discovered from the download page rather than hardcoded, because a
new one appears whenever a public law is enacted.
"""

from __future__ import annotations

import logging
import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import BinaryIO, Final

from lxml import etree

from taxcite.config import (
    USCODE_BASE,
    USCODE_DOWNLOAD_PAGE,
    USCODE_PRIOR_RELEASE_POINTS,
    raw_dir,
)
from taxcite.errors import SourceParseError, SourceUnavailableError
from taxcite.models import Provision, ProvisionStatus, SourceType
from taxcite.sources.http import HttpClient
from taxcite.sources.notes import StatutoryNote, extract_notes

logger: Final = logging.getLogger("taxcite.uscode")

USLM_NS: Final = "http://xml.house.gov/schemas/uslm/1.0"

#: Elements that are themselves provisions, in hierarchy order (SPEC 4.1).
SUBDIVISION_TAGS: Final[tuple[str, ...]] = (
    "subsection",
    "paragraph",
    "subparagraph",
    "clause",
    "subclause",
    "item",
    "subitem",
    "subsubitem",
)

#: Elements whose text is editorial apparatus, never statutory text.
SKIP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "notes",
        "note",
        "sourceCredit",
        "toc",
        "meta",
        "editorialNote",
        "statutoryNote",
        "elipsis",
    }
)

#: Status values used by the OLRC, mapped onto TaxCite's vocabulary.
_STATUS_MAP: Final[dict[str, ProvisionStatus]] = {
    "repealed": "repealed",
    "reserved": "reserved",
    "omitted": "omitted",
    "renumbered": "transferred",
    "transferred": "transferred",
}

_LINK_RE: Final = re.compile(r'href="(xml_usc26@[^"]+\.zip|[^"]*xml_usc26@[^"]+\.zip)"')
_RELEASE_RE: Final = re.compile(r"xml_usc26@(?P<release>[^./\"]+)\.zip")
_WS_RE: Final = re.compile(r"\s+")

#: The OLRC writes hyphenated section numbers with an en dash (``1400Z–2``) in both
#: identifiers and ``num`` values, while every citation style in the wild uses an ASCII
#: hyphen. TaxCite normalises to the hyphen; see docs/architecture.md.
_DASHES: Final = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2212": "-"})


def normalize_identifier(identifier: str) -> str:
    """Return a USLM identifier with en dashes folded to ASCII hyphens."""
    return identifier.translate(_DASHES)


def _local(tag: object) -> str:
    """Return the local name of an lxml tag, ignoring the namespace."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


# --------------------------------------------------------------------------------------
# Release point discovery and download
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleasePoint:
    """A published U.S. Code release point for Title 26.

    A release point is the state of the Code immediately after one public law was
    incorporated. ``enacted`` is the date of that law, which is what makes a release
    point addressable by date: the law in force on any given day is the state at the
    latest release point on or before it.
    """

    release: str
    url: str
    enacted: date | None = None
    titles: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        """Return the local file name for this release point's zip."""
        return f"xml_usc26@{self.release}.zip"

    @property
    def affects_title_26(self) -> bool:
        """Return ``True`` if this release point changed Title 26.

        A release point with no title list is treated as affecting every title: the
        OLRC omits the list for the general revisions, and assuming otherwise would
        silently skip real changes.
        """
        return not self.titles or "26" in self.titles


def discover_release_point(client: HttpClient) -> ReleasePoint:
    """Scrape the OLRC download page for the current Title 26 XML release point.

    Raises:
        SourceUnavailableError: if the page has no Title 26 XML link.
    """
    html = client.get_text(USCODE_DOWNLOAD_PAGE, ttl=60 * 60 * 6)
    match = _LINK_RE.search(html)
    if match is None:
        raise SourceUnavailableError(
            "no Title 26 XML link found on the U.S. Code download page",
            hint=f"Check {USCODE_DOWNLOAD_PAGE} by hand; the page layout may have changed.",
        )
    href = match.group(1)
    release_match = _RELEASE_RE.search(href)
    if release_match is None:  # pragma: no cover - guarded by the link regex
        raise SourceUnavailableError(f"could not read a release point from {href!r}")
    url = href if href.startswith("http") else f"{USCODE_BASE}/download/{href.lstrip('/')}"
    release = release_match.group("release")
    logger.info("current Title 26 release point: %s", release)
    return ReleasePoint(release=release, url=url)


#: One row of the prior-release-points page:
#: ``Public Law 119-110 (09/16/2026), affecting titles 18, 28.``
_PRIOR_RP_RE: Final = re.compile(
    r'href="(?P<href>releasepoints/[^"]+?/usc-rp@(?P<release>[^"/]+)\.htm)"\s*>\s*'
    r"Public Law\s+(?P<law>[0-9A-Za-z-]+)\s*"
    r"\((?P<month>\d\d)/(?P<day>\d\d)/(?P<year>\d{4})\)"
    r"(?:,\s*affecting titles?\s*(?P<titles>[^.<]*))?"
)


def _parse_titles(raw: str | None) -> tuple[str, ...]:
    """Split the OLRC's "affecting titles 1, 5, 26, and 42" into title numbers."""
    if not raw:
        return ()
    cleaned = raw.replace(" and ", ", ")
    return tuple(part.strip() for part in cleaned.split(",") if part.strip())


def discover_release_points(
    client: HttpClient, *, title_26_only: bool = True
) -> list[ReleasePoint]:
    """Return every published release point, newest first.

    The current release point comes from the download page; the historical ones from
    the prior-release-points page, which also carries each public law's enactment date
    and the titles it changed. Filtering to Title 26 matters: of the 378 release points
    published at the time of writing, 59 touched the tax code, and indexing the rest
    would download gigabytes to no purpose.

    Args:
        client: An HTTP client.
        title_26_only: Drop release points that did not change Title 26.
    """
    points: dict[str, ReleasePoint] = {}
    current = discover_release_point(client)
    points[current.release] = current

    html = client.get_text(USCODE_PRIOR_RELEASE_POINTS, ttl=60 * 60 * 24)
    for match in _PRIOR_RP_RE.finditer(html):
        release = match.group("release")
        known = points.get(release)
        if known is not None and known.enacted is not None:
            continue
        try:
            enacted = date(
                int(match.group("year")), int(match.group("month")), int(match.group("day"))
            )
        except ValueError:  # pragma: no cover - the page is machine-generated
            continue
        directory = match.group("href").rsplit("/", 1)[0]
        titles = _parse_titles(match.group("titles"))
        if known is not None:
            # The page lists the current release point too, commented out, and that
            # comment is the only place its enactment date appears. Without the date
            # the current point cannot be ordered against the rest, and any date after
            # the newest historical release point would resolve to the wrong version.
            points[release] = ReleasePoint(
                release=known.release, url=known.url, enacted=enacted, titles=titles
            )
            continue
        points[release] = ReleasePoint(
            release=release,
            url=f"{USCODE_BASE}/download/{directory}/xml_usc26@{release}.zip",
            enacted=enacted,
            titles=titles,
        )

    found = list(points.values())
    if title_26_only:
        found = [point for point in found if point.affects_title_26]
    found.sort(key=lambda point: (point.enacted or date.max, point.release), reverse=True)
    logger.info("%d Title 26 release points discovered", len(found))
    return found


def release_point_for(points: Sequence[ReleasePoint], as_of: date) -> ReleasePoint | None:
    """Return the release point that states the law in force on ``as_of``.

    That is the latest release point enacted on or before the date. The current release
    point has no enactment date recorded and is only used when nothing else fits, which
    happens when ``as_of`` is in the future.
    """
    dated = [point for point in points if point.enacted is not None]
    eligible = [point for point in dated if point.enacted and point.enacted <= as_of]
    if eligible:
        return max(eligible, key=lambda point: (point.enacted or date.min, point.release))
    undated = [point for point in points if point.enacted is None]
    return undated[0] if undated else None


def download_title26(
    client: HttpClient, *, force: bool = False, point: ReleasePoint | None = None
) -> tuple[Path, str]:
    """Download and unpack the Title 26 USLM XML, returning the path and release point.

    Args:
        client: An HTTP client.
        force: Re-download and re-unpack even if the files are already on disk.
        point: A specific release point; the current one is discovered if omitted.

    Raises:
        SourceParseError: if the zip does not contain the expected XML member.
    """
    release_point = point or discover_release_point(client)
    target = raw_dir() / release_point.filename
    client.download(release_point.url, target, force=force)
    xml_path = raw_dir() / f"usc26@{release_point.release}.xml"
    if xml_path.exists() and not force:
        return xml_path, release_point.release
    with zipfile.ZipFile(target) as archive:
        members = [name for name in archive.namelist() if name.endswith(".xml")]
        if not members:
            raise SourceParseError(f"{target} contains no XML file")
        with archive.open(members[0]) as source, xml_path.open("wb") as handle:
            while chunk := source.read(1 << 20):
                handle.write(chunk)
    logger.info("unpacked %s (%d bytes)", xml_path, xml_path.stat().st_size)
    return xml_path, release_point.release


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Ref:
    """One cross-reference found in statutory text."""

    from_id: str
    to_id: str
    kind: str
    external: bool
    raw: str


@dataclass(slots=True)
class ParsedSection:
    """A section and every subdivision under it, ready for insertion.

    Identifiers are unique within a section. They are not always unique in the published
    XML: § 7701(p), for example, reproduces a table of cross-references whose rows are
    numbered ``(1)``–``(9)`` and are given the same identifiers as the real paragraphs
    beside them. Where that happens, the provision carrying a heading wins, because the
    converter only gives headings to the genuine subdivisions.
    """

    provisions: list[Provision] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    notes: list[StatutoryNote] = field(default_factory=list)
    _by_id: dict[str, int] = field(default_factory=dict, repr=False)

    def add(self, provision: Provision) -> Provision:
        """Add a provision, resolving an identifier collision in favour of a heading.

        Returns:
            The provision now stored under that identifier, which may be an earlier one.
        """
        existing_at = self._by_id.get(provision.id)
        if existing_at is None:
            self._by_id[provision.id] = len(self.provisions)
            self.provisions.append(provision)
            return provision
        existing = self.provisions[existing_at]
        logger.debug("duplicate USLM identifier %s", provision.id)
        if provision.heading and not existing.heading:
            self.provisions[existing_at] = provision
            return provision
        return existing


def _collapse(text: str) -> str:
    """Collapse runs of whitespace and trim, the way the index stores text."""
    return _WS_RE.sub(" ", text).strip()


def _all_text(element: etree._Element) -> str:
    """Return every descendant text node of ``element`` as one string."""
    return "".join(part for part in element.itertext() if isinstance(part, str))


def _own_text(element: etree._Element, *, root: bool = True) -> str:
    """Return an element's own text: no child subdivisions, no editorial notes."""
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        tag = _local(child.tag)
        skip = tag in SKIP_TAGS or tag in SUBDIVISION_TAGS or (root and tag in {"num", "heading"})
        if not skip:
            parts.append(_own_text(child, root=False))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _child_text(element: etree._Element, tag: str) -> str | None:
    """Return the collapsed text of the first direct child with local name ``tag``."""
    for child in element:
        if _local(child.tag) == tag:
            text = _collapse(_all_text(child))
            # Repealed and reserved headings are bracketed: "[§ 4. Repealed. ...]".
            return text.removesuffix("]") if tag == "heading" else text
    return None


def _num_value(element: etree._Element) -> str:
    """Return the ``value`` of an element's ``<num>`` child, or its rendered text."""
    for child in element:
        if _local(child.tag) == "num":
            value = child.get("value")
            if value:
                return normalize_identifier(value)
            return normalize_identifier(_collapse(_all_text(child)).strip("\u00a7[]. "))
    return ""


def _status_of(element: etree._Element) -> ProvisionStatus:
    """Map the USLM ``status`` attribute onto a TaxCite provision status."""
    raw = (element.get("status") or "").strip().lower()
    return _STATUS_MAP.get(raw, "active")


def _refs_in(element: etree._Element, from_id: str) -> list[Ref]:
    """Collect ``<ref href>`` targets from an element's own text region."""
    found: list[Ref] = []
    for child in element:
        tag = _local(child.tag)
        if tag in SKIP_TAGS or tag in SUBDIVISION_TAGS:
            continue
        nodes = [child, *child.iter()] if len(child) else [child]
        for node in nodes:
            if _local(node.tag) != "ref":
                continue
            href = node.get("href")
            if not href:
                continue
            external = not href.startswith("/us/usc/t26/")
            found.append(
                Ref(
                    from_id=from_id,
                    to_id=href,
                    kind="explicit",
                    external=external,
                    raw=_collapse(_all_text(node)),
                )
            )
    return found


def _walk(
    element: etree._Element,
    *,
    section: str,
    path: list[str],
    parent_id: str | None,
    source_version: str,
    inherited_status: ProvisionStatus,
    out: ParsedSection,
    identifier_override: str | None = None,
) -> str:
    """Emit a provision for ``element`` and recurse into its subdivisions.

    Returns:
        The element's ``full_text``, so parents can compose theirs bottom-up.
    """
    raw_identifier = element.get("identifier")
    if not raw_identifier:
        raise SourceParseError(f"USLM element {_local(element.tag)} has no identifier")
    identifier = identifier_override or normalize_identifier(raw_identifier)
    status = _status_of(element)
    if status == "active":
        status = inherited_status
    own = _collapse(_own_text(element))
    heading = _child_text(element, "heading")
    level = "section" if not path else _local(element.tag)
    num = _num_value(element)

    provision = Provision(
        id=identifier,
        source=SourceType.IRC,
        section=section,
        path=list(path),
        level=level,
        num=f"({num})" if path else section,
        heading=heading,
        text=own,
        full_text=own,
        parent_id=parent_id,
        status=status,
        source_version=source_version,
    )
    provision = out.add(provision)
    out.refs.extend(_refs_in(element, identifier))

    # Compose full_text in document order: a subsection's flush "continuation" text
    # legally follows its paragraphs, so it must follow them in the composed text too.
    ordered: list[str] = []
    for child in element:
        tag = _local(child.tag)
        if tag in SUBDIVISION_TAGS:
            ordered.append(
                _walk(
                    child,
                    section=section,
                    path=[*path, _num_value(child)],
                    parent_id=identifier,
                    source_version=source_version,
                    inherited_status=status,
                    out=out,
                )
            )
        elif not (tag in SKIP_TAGS or tag in {"num", "heading"}):
            ordered.append(_collapse(_own_text(child, root=False)))

    provision.full_text = _collapse(" ".join(part for part in ordered if part)) or own
    return provision.full_text


def section_identifiers(element: etree._Element) -> list[str]:
    """Return every section identifier an element carries.

    A handful of repealed stubs cover two sections at once and carry both identifiers in
    one attribute, e.g. ``"/us/usc/t26/s50A /us/usc/t26/s50B"``.
    """
    raw = element.get("identifier") or ""
    return [normalize_identifier(part) for part in raw.split() if part]


def parse_section(element: etree._Element, source_version: str) -> ParsedSection:
    """Parse one USLM ``<section>`` element into provisions and cross-references."""
    out = ParsedSection()
    for identifier in section_identifiers(element):
        out.notes.extend(extract_notes(element, identifier))
        _walk(
            element,
            section=identifier.rsplit("/s", 1)[-1],
            path=[],
            parent_id=None,
            source_version=source_version,
            inherited_status="active",
            out=out,
            identifier_override=identifier,
        )
    return out


def iter_sections(xml_path: Path, source_version: str) -> Iterator[ParsedSection]:
    """Stream every Title 26 section from a USLM XML document.

    Uses ``iterparse`` so that memory stays bounded on the 50+ MB Title 26 document, and
    clears each section subtree once it has been converted.
    """
    with xml_path.open("rb") as handle:
        yield from _iter_sections(handle, source_version)


def _iter_sections(handle: BinaryIO, source_version: str) -> Iterator[ParsedSection]:
    """Stream sections from an open USLM byte stream."""
    context = etree.iterparse(
        handle,
        events=("end",),
        tag=f"{{{USLM_NS}}}section",
        huge_tree=True,
    )
    for _event, element in context:
        identifiers = section_identifiers(element)
        if identifiers and all(
            i.startswith("/us/usc/t26/s") and i.count("/") == 4 for i in identifiers
        ):
            yield parse_section(element, source_version)
        element.clear()
        # Drop already-processed siblings so the document does not accumulate in memory.
        while element.getprevious() is not None:
            parent = element.getparent()
            if parent is None:  # pragma: no cover - the root has no parent
                break
            del parent[0]


def ordinal_of(provisions: list[Provision]) -> dict[str, int]:
    """Return each provision's document order within its parent."""
    counters: dict[str | None, int] = {}
    ordinals: dict[str, int] = {}
    for provision in provisions:
        counters.setdefault(provision.parent_id, 0)
        ordinals[provision.id] = counters[provision.parent_id]
        counters[provision.parent_id] += 1
    return ordinals
