"""Was this provision in force for the year in question? (roadmap 1.2).

The commonest substantive citation error in tax writing is not a fabricated section.
It is a real section cited for a year it did not govern — § 199A in a 2017 memo, the
old § 163(j) earnings-stripping rule applied to 2019, a provision whose amendment does
not bite until 2026. The text reads correctly and the citation resolves; it is simply
the wrong law for the year.

The dates come from the statutory notes (:mod:`taxcite.sources.notes`), because that is
where Congress puts them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from taxcite.citations import normalize as norm
from taxcite.index import db
from taxcite.models import SourceType

Applicability = Literal["applies", "not_yet", "unknown"]

#: Topics that speak to when a provision itself began to apply, as opposed to when a
#: particular amendment to it did.
_COMMENCEMENT_TOPICS: Final[tuple[str, ...]] = ("effectiveDate",)


@dataclass(frozen=True, slots=True)
class EffectiveDate:
    """What the notes say about when a provision started to apply."""

    provision_id: str
    applies_after: date | None
    heading: str | None
    text: str

    def status_for(self, tax_year: int) -> Applicability:
        """Return whether the provision governed ``tax_year``.

        A calendar-year taxpayer's year begins on 1 January, and the standard formula
        is "taxable years beginning **after** <date>", so the year applies when its
        first day falls strictly after the stated date. Fiscal-year taxpayers are not
        modelled; the answer is deliberately coarse and the note is always shown so a
        reader can check the edge themselves.
        """
        if self.applies_after is None:
            return "unknown"
        return "applies" if date(tax_year, 1, 1) > self.applies_after else "not_yet"


def commencement(connection: sqlite3.Connection, provision_id: str) -> EffectiveDate | None:
    """Return the effective-date note for the section containing ``provision_id``.

    Notes attach at section level in the USLM, so a pinpoint citation inherits its
    section's commencement date.
    """
    try:
        section_id = norm.section_id(provision_id)
    except ValueError:  # pragma: no cover - callers pass canonical ids
        return None
    placeholders = ",".join("?" * len(_COMMENCEMENT_TOPICS))
    row = connection.execute(
        # The only interpolation is the placeholder list; the topics are bound.
        f"SELECT * FROM notes WHERE provision_id = ? AND topic IN ({placeholders}) "
        "AND applies_after IS NOT NULL ORDER BY ordinal LIMIT 1",
        (section_id, *_COMMENCEMENT_TOPICS),
    ).fetchone()
    if row is None:
        return None
    return EffectiveDate(
        provision_id=section_id,
        applies_after=date.fromisoformat(row["applies_after"]),
        heading=row["heading"],
        text=row["text"],
    )


@dataclass(frozen=True, slots=True)
class AmendmentWindow:
    """An amendment to a provision that takes effect after a given date."""

    heading: str | None
    applies_after: date
    text: str


def later_amendments(
    connection: sqlite3.Connection, provision_id: str, tax_year: int
) -> list[AmendmentWindow]:
    """Return amendments that do not bite until after ``tax_year``.

    Reading current text for an earlier year is the mirror image of citing a provision
    too early, and it is just as wrong: the words on the page include changes that had
    not happened yet.
    """
    try:
        section_id = norm.section_id(provision_id)
    except ValueError:  # pragma: no cover - callers pass canonical ids
        return []
    rows = connection.execute(
        "SELECT heading, text, applies_after FROM notes "
        "WHERE provision_id = ? AND topic IN ('effectiveDateOfAmendment', "
        "'prospectiveAmendment') AND applies_after IS NOT NULL "
        "AND applies_after >= ? ORDER BY applies_after",
        (section_id, date(tax_year, 1, 1).isoformat()),
    ).fetchall()
    return [
        AmendmentWindow(
            heading=row["heading"],
            applies_after=date.fromisoformat(row["applies_after"]),
            text=row["text"],
        )
        for row in rows
    ]


#: I.R.C. § 7805(e)(2): a temporary regulation expires three years after issuance.
TEMPORARY_REGULATION_YEARS: Final = 3

#: § 7805(e) applies only to regulations issued after this date, so an older
#: temporary regulation does not expire under it however old it is.
TEMPORARY_SUNSET_APPLIES_AFTER: Final = date(1988, 11, 20)


@dataclass(frozen=True, slots=True)
class TemporaryRegulation:
    """When a temporary regulation was issued, and whether it has expired."""

    provision_id: str
    issued: date
    expires: date
    credit: str

    @property
    def expired(self) -> bool:
        """Return ``True`` if the three-year clock has run."""
        return date.today() > self.expires

    @property
    def sunset_applies(self) -> bool:
        """Return ``True`` if § 7805(e) reaches a regulation issued this early."""
        return self.issued > TEMPORARY_SUNSET_APPLIES_AFTER


def temporary_sunset(
    connection: sqlite3.Connection, provision_id: str
) -> TemporaryRegulation | None:
    """Return the sunset position of a temporary regulation, if it is one.

    The issuance date comes from the source credit the eCFR prints at the foot of the
    section. Regulations issued before § 7805(e) took effect are reported with that
    fact rather than being treated as expired.
    """
    try:
        source, section, _ = norm.split_canonical_id(provision_id)
    except ValueError:  # pragma: no cover - callers pass canonical ids
        return None
    if source is not SourceType.REG or not _is_temporary(section):
        return None
    section_id = norm.canonical_id(source, section)
    row = connection.execute(
        "SELECT text, dates FROM notes WHERE provision_id = ? AND topic = 'sourceCredit'",
        (section_id,),
    ).fetchone()
    if row is None:
        return None
    dates = json.loads(row["dates"] or "[]")
    if not dates:
        return None
    issued = date.fromisoformat(dates[0])
    return TemporaryRegulation(
        provision_id=section_id,
        issued=issued,
        expires=_add_years(issued, TEMPORARY_REGULATION_YEARS),
        credit=str(row["text"]),
    )


def _is_temporary(section: str) -> bool:
    """Return ``True`` for a temporary regulation's section number, e.g. 1.469-5T."""
    return bool(re.search(r"-\d+T$", section))


def _add_years(value: date, years: int) -> date:
    """Add whole years to a date, stepping back off 29 February."""
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # pragma: no cover - only 29 February reaches this
        return value.replace(year=value.year + years, day=28)


def describe(effective: EffectiveDate, tax_year: int) -> str:
    """Render a one-line explanation of an applicability finding."""
    status = effective.status_for(tax_year)
    label = norm.display_for(
        *_split(effective.provision_id),
    )
    if status == "not_yet":
        return (
            f"{label} applies to taxable years beginning after "
            f"{effective.applies_after.isoformat() if effective.applies_after else '?'}, "
            f"so it did not govern tax year {tax_year}"
        )
    if status == "applies":
        return (
            f"{label} applies to taxable years beginning after "
            f"{effective.applies_after.isoformat() if effective.applies_after else '?'}"
        )
    return f"{label} has no stated effective date"


def _split(provision_id: str) -> tuple[SourceType, str, list[str]]:
    """Split a canonical id for display."""
    return norm.split_canonical_id(provision_id)


def summarize_for_year(
    connection: sqlite3.Connection, provision_id: str, tax_year: int
) -> tuple[Applicability, str | None, list[AmendmentWindow]]:
    """Return the applicability of a provision to a tax year, with an explanation."""
    effective = commencement(connection, provision_id)
    pending = later_amendments(connection, provision_id, tax_year)
    if effective is None:
        return "unknown", None, pending
    return effective.status_for(tax_year), describe(effective, tax_year), pending


def notes_for(connection: sqlite3.Connection, provision_id: str) -> list[sqlite3.Row]:
    """Return every note attached to the section containing ``provision_id``."""
    try:
        section_id = norm.section_id(provision_id)
    except ValueError:  # pragma: no cover - callers pass canonical ids
        return []
    return db.get_notes(connection, section_id)
