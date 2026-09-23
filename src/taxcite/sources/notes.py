"""Statutory notes, and the effective dates hidden in them (roadmap 1.2).

A note is not statutory text and TaxCite never lets one be quoted as though it were.
But notes are where tax law keeps its dates. Congress routinely leaves "applies to
taxable years beginning after December 31, 2017" out of the section it is amending and
puts it in an uncodified provision of the public law, which reaches the Code only as an
editorial note. Without those notes there is no way to tell whether the provision you
are reading governed the year you are asking about — which, in tax, is most of the
question.

The OLRC marks dates up explicitly (``<date date="2017-12-31">``), so the extraction is
reliable rather than a guess at prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from lxml import etree

#: Note topics worth keeping. The rest are editorial apparatus with no operative
#: content — cross-headings, references in text, codification history.
KEPT_TOPICS: Final[frozenset[str]] = frozenset(
    {
        "effectiveDate",
        "effectiveDateOfAmendment",
        "prospectiveAmendment",
        "amendments",
        "priorProvisions",
        "savings",
        "shortTitleOfAmendment",
        "repeals",
        "termination",
        "construction",
        "transferOfFunctions",
        "miscellaneous",
    }
)

#: Topics that can carry an applicability date.
DATED_TOPICS: Final[frozenset[str]] = frozenset(
    {"effectiveDate", "effectiveDateOfAmendment", "prospectiveAmendment", "termination"}
)

_WS_RE: Final = re.compile(r"\s+")

#: The standard applicability formulas. Tax legislation is formulaic here, which is
#: what makes this worth automating: the phrasing varies far less than the substance.
_APPLICABILITY_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?:applicable|shall apply|apply|applies)\s+to\s+taxable\s+years?\s+"
        r"beginning\s+after\s+(?P<date>[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:applicable|shall apply|apply|applies)\s+to\s+taxable\s+years?\s+"
        r"ending\s+after\s+(?P<date>[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:applicable|shall apply|apply|applies)\s+to\s+"
        r"(?:amounts|property|payments|transfers|distributions|contributions|"
        r"expenditures|dispositions)\s+[^.;]{0,60}?after\s+"
        r"(?P<date>[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:effective|Effective)\s+(?:on\s+and\s+after\s+|after\s+|)"
        r"(?P<date>[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})"
    ),
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

_TEXT_DATE_RE: Final = re.compile(r"([A-Z][a-z]+)\.?\s+(\d{1,2}),\s+(\d{4})")


def parse_text_date(text: str) -> date | None:
    """Parse "Dec. 31, 2017" or "December 31, 2017" into a date."""
    match = _TEXT_DATE_RE.match(text.strip())
    if match is None:
        return None
    month = _MONTHS.get(match.group(1)[:3].lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:  # pragma: no cover - the corpus is well formed
        return None


@dataclass(slots=True)
class StatutoryNote:
    """One note attached to a section."""

    provision_id: str
    ordinal: int
    topic: str
    heading: str | None
    text: str
    dates: list[date] = field(default_factory=list)
    applies_after: date | None = None


def _local(tag: object) -> str:
    """Return the local name of an lxml tag."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _flatten(element: etree._Element) -> str:
    """Return an element's whole text content, whitespace collapsed."""
    parts = [part for part in element.itertext() if isinstance(part, str)]
    return _WS_RE.sub(" ", "".join(parts)).strip()


def _marked_dates(element: etree._Element) -> list[date]:
    """Return every date the OLRC marked up inside an element."""
    found: list[date] = []
    for node in element.iter():
        if _local(node.tag) != "date":
            continue
        raw = node.get("date")
        if not raw:
            continue
        try:
            found.append(date.fromisoformat(raw))
        except ValueError:  # pragma: no cover - the attribute is machine-generated
            continue
    return found


def applicability_date(text: str) -> date | None:
    """Extract the date a provision starts to apply, if the note states one.

    Returns the date *named* in the formula, not the first day it bites: "taxable
    years beginning after December 31, 2017" returns 2017-12-31, and the caller
    compares a tax year's start against it.
    """
    for pattern in _APPLICABILITY_RES:
        match = pattern.search(text)
        if match is None:
            continue
        parsed = parse_text_date(match.group("date"))
        if parsed is not None:
            return parsed
    return None


def extract_notes(section: etree._Element, provision_id: str) -> list[StatutoryNote]:
    """Collect the notes attached to a USLM ``<section>``."""
    found: list[StatutoryNote] = []
    ordinal = 0
    for notes_block in section:
        if _local(notes_block.tag) != "notes":
            continue
        for note in notes_block:
            if _local(note.tag) != "note":
                continue
            topic = note.get("topic") or ""
            if topic not in KEPT_TOPICS or note.get("role") == "crossHeading":
                continue
            heading = None
            for child in note:
                if _local(child.tag) == "heading":
                    heading = _flatten(child)
                    break
            text = _flatten(note)
            if heading and text.startswith(heading):
                text = text[len(heading) :].strip()
            if not text:
                continue
            dates = _marked_dates(note)
            found.append(
                StatutoryNote(
                    provision_id=provision_id,
                    ordinal=ordinal,
                    topic=topic,
                    heading=heading,
                    text=text,
                    dates=dates,
                    applies_after=(applicability_date(text) if topic in DATED_TOPICS else None),
                )
            )
            ordinal += 1
    return found


def to_rows(
    notes: list[StatutoryNote],
) -> list[tuple[str, int, str, str | None, str, str, str | None]]:
    """Render notes as database rows."""
    return [
        (
            note.provision_id,
            note.ordinal,
            note.topic,
            note.heading,
            note.text,
            json.dumps([value.isoformat() for value in note.dates]),
            note.applies_after.isoformat() if note.applies_after else None,
        )
        for note in notes
    ]
