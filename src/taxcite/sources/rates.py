"""Published rates from the Internal Revenue Bulletin.

Some tax computations turn on a rate the government publishes monthly rather than on
anything in the Code. The most consequential of these in deal work is the
**long-term tax-exempt rate** of I.R.C. § 382(f): multiply it by the value of a loss
corporation and you have the annual ceiling on how much of that corporation's
pre-change losses a buyer may ever use.

The rate is published in Table 3 of the monthly applicable-federal-rate Revenue
Ruling, which appears in the Bulletin — a source TaxCite already ingests. Parsing it
here means a § 382 limitation can be computed from published authority and every
figure in the answer can name the ruling it came from, rather than a practitioner
pasting a number out of a spreadsheet whose provenance nobody remembers.

Nothing here is a projection. A rate is read out of a ruling or it is absent, and an
absent rate is reported as unknown rather than guessed at or interpolated.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Final

from lxml import html as lxml_html
from lxml.html import HtmlElement

from taxcite.errors import SourceParseError
from taxcite.sources.http import HttpClient
from taxcite.sources.irb import bulletin_url

logger: Final = logging.getLogger("taxcite.rates")

#: The row of Table 3 that carries the § 382 rate. The IRS wording has been stable for
#: years but the hyphen in "tax-exempt" is not, so it is optional here.
_LTTER_RE: Final = re.compile(
    r"long[-\s]term\s+tax[-\s]?exempt\s+rate\s+for\s+ownership\s+changes", re.IGNORECASE
)

#: The adjusted federal long-term rate, the input § 382(f) takes the highest of.
_ADJUSTED_RE: Final = re.compile(
    r"adjusted\s+federal\s+long[-\s]term\s+rate\s+for\s+the\s+current\s+month", re.IGNORECASE
)

_PERCENT_RE: Final = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*%")

#: The caption the IRS prints directly above the § 382 table, which names both the
#: publishing ruling and the month the rates govern:
#:
#:     REV. RUL. 2026-13 TABLE 3  Rates Under Section 382 for August 2026
#:
#: Reading month and ruling from this caption rather than from the document as a whole
#: is what makes the result trustworthy. A Bulletin also carries a cumulative findings
#: list and cross-references to earlier rulings, and a document-wide search picks those
#: up instead — which silently attributed August's rates to June, and named a ruling
#: from 2003 as the source. A rate filed under the wrong month is worse than no rate at
#: all, because § 382(f) keys on the month and the error is invisible in the answer.
_CAPTION_RE: Final = re.compile(
    r"REV\.\s*RUL\.\s*(?P<ruling>\d{4}-\d{1,3})\s*TABLE\s*3\b"
    r".{0,80}?Rates\s+Under\s+Section\s+382\s+for\s+"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(?P<year>\d{4})",
    re.IGNORECASE | re.DOTALL,
)

_MONTHS: Final[dict[str, int]] = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}


@dataclass(frozen=True, slots=True)
class PublishedRate:
    """One month's § 382 rates, as one Revenue Ruling states them."""

    month: date
    """The first day of the month the rates govern."""
    long_term_tax_exempt: float
    """The § 382(f) rate, as a percentage — 3.88 means 3.88%."""
    adjusted_federal_long_term: float | None
    """The § 1274(d) adjusted federal long-term rate for the same month."""
    ruling: str
    """The Revenue Ruling that published it, e.g. "Rev. Rul. 2026-17"."""
    bulletin: str
    """The Bulletin it appeared in, e.g. "2026-37"."""
    url: str

    @property
    def rate(self) -> float:
        """The § 382(f) rate as a decimal fraction, for arithmetic."""
        return self.long_term_tax_exempt / 100.0


def _percent(text: str) -> float | None:
    """Read a percentage out of a table cell."""
    found = _PERCENT_RE.search(text)
    return float(found.group(1)) if found else None


def _text_of(element: HtmlElement) -> str:
    """Return an element's visible text, whitespace collapsed.

    Comments and processing instructions carry a callable ``tag`` and raise rather
    than yielding text, and IRS bulletins contain both.
    """
    if not isinstance(element.tag, str):
        return ""
    return " ".join(str(piece) for piece in element.itertext()).strip()


def _row_text(row: HtmlElement) -> list[str]:
    """Return the non-empty cell texts of a table row."""
    cells = []
    for cell in row.iter("td", "th"):
        text = _text_of(cell)
        if text:
            cells.append(text)
    return cells


def parse_rates(payload: bytes, bulletin: str, url: str) -> list[PublishedRate]:
    """Extract every § 382 rate stated in one Bulletin.

    A rate is emitted only when the table is directly captioned as the § 382 table for
    a named month. Without that caption nothing is emitted, because the alternative —
    inferring the month from elsewhere in the document — attributes rates to the wrong
    month, and a § 382 limitation computed from the wrong month's rate is wrong in a
    way that no reader can see.

    Raises:
        SourceParseError: if the Bulletin cannot be parsed as HTML at all.
    """
    try:
        root = lxml_html.fromstring(payload)
    except Exception as exc:  # pragma: no cover - lxml is permissive
        raise SourceParseError(f"could not parse bulletin {bulletin}") from exc

    found: list[PublishedRate] = []
    seen: set[date] = set()
    for caption, table in _captioned_tables(root):
        exempt, adjusted = _read_rates(table)
        if exempt is None:
            continue
        month = date(int(caption["year"]), _MONTHS[caption["month"].lower()], 1)
        if month in seen:
            continue
        seen.add(month)
        found.append(
            PublishedRate(
                month=month,
                long_term_tax_exempt=exempt,
                adjusted_federal_long_term=adjusted,
                ruling=f"Rev. Rul. {caption['ruling']}",
                bulletin=bulletin,
                url=url,
            )
        )
    return found


def _captioned_tables(root: HtmlElement) -> list[tuple[re.Match[str], HtmlElement]]:
    """Pair each § 382 caption with the table that follows it.

    The caption may sit in its own heading or be folded into the table's own markup,
    so both are handled: an element whose text carries the caption is matched either
    against the next table in document order, or against itself.
    """
    elements = list(root.iter())
    pairs: list[tuple[re.Match[str], HtmlElement]] = []
    for position, element in enumerate(elements):
        if not isinstance(element.tag, str) or element.tag == "table":
            continue
        text = _text_of(element)
        if len(text) > 4000:
            # A container holding the whole page; its caption tells us nothing about
            # which table is which.
            continue
        caption = _CAPTION_RE.search(text)
        if caption is None:
            continue
        for candidate in elements[position:]:
            if candidate.tag == "table":
                pairs.append((caption, candidate))
                break
    return pairs


def _read_rates(table: HtmlElement) -> tuple[float | None, float | None]:
    """Return the long-term tax-exempt and adjusted federal long-term rates."""
    exempt: float | None = None
    adjusted: float | None = None
    for row in table.iter("tr"):
        cells = _row_text(row)
        if len(cells) < 2:
            continue
        label, value = cells[0], cells[-1]
        if _LTTER_RE.search(label):
            exempt = _percent(value)
        elif _ADJUSTED_RE.search(label):
            adjusted = _percent(value)
    return exempt, adjusted


def fetch_rates(client: HttpClient, year: int, week: int) -> list[PublishedRate]:
    """Download one Bulletin and read any § 382 rates out of it."""
    url = bulletin_url(year, week)
    payload = client.get_bytes(url)
    return parse_rates(payload, f"{year}-{week:02d}", url)


def index_rates(connection: sqlite3.Connection, rates: list[PublishedRate]) -> int:
    """Write published rates into the index, replacing any earlier copy."""
    from taxcite.index import db

    rows = [
        (
            rate.month.isoformat(),
            rate.long_term_tax_exempt,
            rate.adjusted_federal_long_term,
            rate.ruling,
            rate.bulletin,
            rate.url,
        )
        for rate in rates
    ]
    with db.transaction(connection):
        db.insert_rates(connection, rows)
    return len(rows)


def rate_for(connection: sqlite3.Connection, when: date) -> PublishedRate | None:
    """Return the § 382 rate governing an ownership change on ``when``.

    § 382(f) fixes the rate by the month of the ownership change, so this is an exact
    month lookup and never the nearest available one. A month that has not been
    indexed returns ``None``, which callers must report as unknown — using a
    neighbouring month's rate would silently change the answer.
    """
    from taxcite.index import db

    row = db.get_rate(connection, date(when.year, when.month, 1).isoformat())
    if row is None:
        return None
    return PublishedRate(
        month=date.fromisoformat(str(row["month"])),
        long_term_tax_exempt=float(row["long_term_tax_exempt"]),
        adjusted_federal_long_term=(
            float(row["adjusted_federal_long_term"])
            if row["adjusted_federal_long_term"] is not None
            else None
        ),
        ruling=str(row["ruling"]),
        bulletin=str(row["bulletin"]),
        url=str(row["url"]),
    )


__all__ = [
    "PublishedRate",
    "fetch_rates",
    "index_rates",
    "parse_rates",
    "rate_for",
]
