"""What you have to read, and what the words in it mean.

``closure`` answers one half of the question a reviewer faces: which other provisions
does this one send me to. It does not answer the other half, which in some corners of
the regulations is the harder one — *which of the words in this sentence are terms of
art, and where are they defined*.

The consolidated return regulations are the clearest case. A sentence of
Treas. Reg. § 1.1502-21 is nearly all defined terms: "member", "group", "consolidated
taxable income", "separate return limitation year". Each is defined somewhere else,
sometimes in § 1.1502-1, sometimes in § 1504, and reading the sentence without them
is reading it wrong. A reading list that gives only the cross-references leaves a
reviewer to notice the defined terms by eye.

This module puts both halves together, and adds the status flags that decide whether
what you are about to read is still live: a temporary regulation that has expired
under § 7805(e), a provision that did not govern the year in question, guidance a
later document says it supersedes.

Nothing here is curated. The cross-references come from the reference graph, the
defined terms from the definitions index, and the flags from the notes and source
credits — all built from the official text. There is no editorial list of "topics you
should read for SRLY", because that would be an opinion wearing the clothes of a
lookup.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Final

from taxcite.citations import normalize as cite_norm
from taxcite.graph import definitions as defs
from taxcite.graph import xrefs
from taxcite.index import db
from taxcite.models import Definition, Provision

#: Terms too common to be worth reporting as terms of art even where the corpus
#: defines them. Listing "person" on every reading list is noise that hides the
#: entries that matter.
COMMON_TERMS: Final[frozenset[str]] = frozenset(
    {
        "person",
        "taxpayer",
        "secretary",
        "state",
        "united states",
        "year",
        "taxable year",
        "corporation",
        "partnership",
        "individual",
        "property",
        "stock",
    }
)

#: How many defined terms to report. A long list is as unhelpful as none.
MAX_TERMS: Final = 12


@dataclass(slots=True)
class TermEntry:
    """A term of art used in a provision, and where it is defined."""

    term: str
    definitions: list[Definition] = field(default_factory=list)

    @property
    def defined(self) -> bool:
        """Whether a governing definition was found."""
        return bool(self.definitions)


@dataclass(slots=True)
class ReadingList:
    """Everything needed to read one provision with understanding."""

    provision: Provision
    display: str
    closure: list[xrefs.ClosureEntry] = field(default_factory=list)
    terms: list[TermEntry] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """How many further provisions the list names."""
        return len(self.closure)


def build(
    connection: sqlite3.Connection,
    provision_id: str,
    *,
    depth: int = 2,
    limit: int = 25,
    tax_year: int | None = None,
    max_terms: int = MAX_TERMS,
) -> ReadingList | None:
    """Assemble a reading list for one provision.

    Args:
        connection: An open index.
        provision_id: The canonical id to start from.
        depth: How far to walk the reference graph.
        limit: Maximum provisions in the closure.
        tax_year: Flag anything that did not govern this year.
        max_terms: Maximum defined terms to report.

    Returns:
        A :class:`ReadingList`, or ``None`` if the provision is not indexed.
    """
    provision = db.get_provision(connection, provision_id)
    if provision is None:
        return None

    entries = xrefs.closure(connection, provision_id, max_depth=depth, limit=limit)
    terms = _terms_used(connection, provision, max_terms=max_terms)
    flags = _flags(connection, provision, tax_year=tax_year)

    return ReadingList(
        provision=provision,
        display=cite_norm.display_for(provision.source, provision.section, provision.path),
        closure=entries,
        terms=terms,
        flags=flags,
    )


def _terms_used(
    connection: sqlite3.Connection, provision: Provision, *, max_terms: int
) -> list[TermEntry]:
    """Find the terms of art a provision uses, and the definitions that govern it.

    The candidate terms are the quoted phrases the corpus itself defines, matched
    against this provision's text. Searching the other way round — every phrase in the
    text against the definitions index — produces mostly noise, because ordinary words
    appear in definitions too.
    """
    haystack = provision.full_text.lower()
    found: list[TermEntry] = []
    for term in _candidate_terms(connection):
        if term in COMMON_TERMS:
            continue
        if not _uses_term(haystack, term):
            continue
        governing = defs.governing(connection, term, provision.id, limit=3)
        if not governing:
            continue
        # A provision that defines the term itself is not using someone else's.
        if any(d.provision_id == provision.id for d in governing):
            continue
        found.append(TermEntry(term=governing[0].term_display, definitions=governing))
    found.sort(key=lambda entry: (-_relevance(entry, provision), -len(entry.term), entry.term))
    return found[:max_terms]


def _family(provision_id: str) -> str:
    """Return the regulation family a provision belongs to.

    Treas. Reg. § 1.1502-21 and § 1.1502-1 are one body of rules that define terms for
    each other; § 1.162-1 is not part of it. The family is the section number up to
    the first hyphen, which is exactly how the CFR groups them.
    """
    try:
        _source, section, _path = cite_norm.split_canonical_id(provision_id)
    except ValueError:  # pragma: no cover - callers pass canonical ids
        return ""
    return section.split("-", 1)[0]


def _relevance(entry: TermEntry, provision: Provision) -> int:
    """Score a term of art by how likely it is to be the one that matters here.

    A definition from the same body of regulations outranks one from a distant part of
    the Code, however long the phrase. Without this, a reading list for the SRLY
    limitation reported "life insurance company" and omitted "member" and "separate
    return limitation year" — the two terms the provision is actually built out of.
    """
    definition = entry.definitions[0]
    score = 0
    if _family(definition.provision_id) == _family(provision.id):
        score += 100
    elif definition.provision_id.startswith("/us/cfr/") and provision.id.startswith("/us/cfr/"):
        score += 20
    # Only a scope that can actually be checked against the identifier is evidence.
    # "This part" cannot be — Code parts are not encoded in a canonical id — so
    # rewarding it promoted definitions from unrelated corners of the Code ("mines",
    # from § 611(a)) over the ones the provision is built out of.
    if definition.scope and definition.scope.lower() in {
        "this section",
        "this subsection",
        "this paragraph",
    }:
        score += 40
    if entry.term.lower() in provision.text.lower():
        score += 25
    return score


def _uses_term(haystack: str, term: str) -> bool:
    """Return ``True`` if a provision actually uses a term.

    Short terms are matched on word boundaries. The regulations abbreviate their
    terms of art — "SRLY", "CFC" — and a bare substring test would find those letters
    inside unrelated words.
    """
    if len(term) >= 6:
        return term in haystack
    return re.search(rf"\b{re.escape(term)}\b", haystack) is not None


def _candidate_terms(connection: sqlite3.Connection) -> list[str]:
    """Return defined terms, longest first so the specific ones are tried first.

    Three characters is the floor: shorter than that is not a term of art, and the
    abbreviations that matter — SRLY, CFC, ELA — are three or four.
    """
    rows = connection.execute(
        "SELECT DISTINCT term_norm FROM definitions "
        "WHERE LENGTH(term_norm) >= 3 ORDER BY LENGTH(term_norm) DESC, term_norm"
    ).fetchall()
    return [str(row["term_norm"]) for row in rows]


def _flags(
    connection: sqlite3.Connection, provision: Provision, *, tax_year: int | None
) -> list[str]:
    """Report anything that bears on whether this text is live."""
    from taxcite.verify import effective

    out: list[str] = []

    sunset = effective.temporary_sunset(connection, provision.id)
    if sunset is not None:
        if sunset.expired and sunset.sunset_applies:
            out.append(
                f"Temporary regulation: issued {sunset.issued}, expired "
                f"{sunset.expires} under I.R.C. § 7805(e)"
            )
        elif sunset.sunset_applies:
            out.append(
                f"Temporary regulation: issued {sunset.issued}, expires {sunset.expires} "
                f"under I.R.C. § 7805(e)"
            )
        else:
            out.append(
                f"Temporary regulation issued {sunset.issued}, before I.R.C. § 7805(e) "
                f"applied; it does not expire by operation of that section"
            )

    commencement = effective.commencement(connection, provision.id)
    if commencement is not None and commencement.applies_after is not None:
        out.append(f"Applies to periods after {commencement.applies_after.isoformat()}")
        if tax_year is not None and commencement.status_for(tax_year) == "not_yet":
            out.append(f"Did not govern tax year {tax_year}")

    if tax_year is not None:
        pending = effective.later_amendments(connection, provision.id, tax_year)
        for amendment in pending:
            if amendment.applies_after is not None:
                out.append(
                    f"Amended with effect after {amendment.applies_after.isoformat()}, "
                    f"later than tax year {tax_year}"
                )

    return out


def to_markdown(reading: ReadingList) -> str:
    """Render a reading list as something a reviewer can work through."""
    out: list[str] = [f"# Reading list — {reading.display}", ""]
    if reading.provision.heading:
        out += [f"**{reading.provision.heading}**", ""]

    if reading.flags:
        out += ["## Before you rely on this", ""]
        out += [f"- {flag}" for flag in reading.flags]
        out.append("")

    if reading.terms:
        out += [
            "## Terms of art used here, and where they are defined",
            "",
            "| Term | Defined at | Scope |",
            "|---|---|---|",
        ]
        for entry in reading.terms:
            for definition in entry.definitions[:1]:
                scope = definition.scope or "—"
                out.append(f"| {entry.term} | {definition.display} | {scope} |")
        out.append("")

    if reading.closure:
        out += [
            "## What this provision sends you to",
            "",
            "| Depth | Provision | Via |",
            "|---:|---|---|",
        ]
        for step in reading.closure:
            heading = f" — {step.heading}" if step.heading else ""
            out.append(f"| {step.depth} | {step.display}{heading} | {step.reached_via} |")
        out.append("")
        out.append(f"**{reading.total} further provisions.**")
        out.append("")
    else:
        out += ["This provision makes no outward cross-references.", ""]

    out.append(
        "> A reading list, not a summary. It names what to read and what the terms "
        "mean; it does not tell you what the provisions say or what they require."
    )
    return "\n".join(out)


__all__ = ["COMMON_TERMS", "MAX_TERMS", "ReadingList", "TermEntry", "build", "to_markdown"]
