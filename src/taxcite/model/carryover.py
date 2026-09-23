"""Attribute carryover under I.R.C. § 381, and the limitations that follow it.

When one corporation acquires another's assets in a qualifying transaction, § 381(a)
makes the acquirer succeed to the target's tax attributes — but only the attributes
§ 381(c) enumerates, and only subject to the limitations elsewhere in the Code. Two
questions follow every deal of this shape: *which attributes come across*, and *what
limits their use afterwards*.

Both are answerable from the statute, which is what this module does. § 381(c) is a
**closed enumerated list**, the same structural fact that makes the authority
classification of Treas. Reg. § 1.6662-4(d)(3)(iii) checkable: what is on the list is
on it, and what is not does not carry over by virtue of this section.

The list is read out of the indexed Code rather than typed in here. That matters for
two reasons. It stays current as Congress amends § 381 — items (7), (15) and (21) are
repealed, and the repeals are visible in the statutory headings, not in anyone's
memory. And it means this module is a derivation rather than an editorial checklist
that happens to be written in Python.

What it does not do is decide whether your transaction qualifies. § 381(a) turns on
whether § 332 applies, or whether a transfer is in connection with a reorganization
described in § 368(a)(1)(A), (C), (D), (F) or (G) — questions of fact and of
characterisation that this cannot answer and does not attempt.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Final

from taxcite.citations import normalize as cite_norm
from taxcite.index import db
from taxcite.models import Provision

#: The section that enumerates the attributes.
CARRYOVER_SECTION: Final = "/us/usc/t26/s381"
ITEMS_SUBSECTION: Final = "/us/usc/t26/s381/c"
QUALIFYING_SUBSECTION: Final = "/us/usc/t26/s381/a"
OPERATING_RULES: Final = "/us/usc/t26/s381/b"

#: How the Code marks a paragraph that no longer operates.
_REPEALED_RE: Final = re.compile(r"^\s*repealed\b", re.IGNORECASE)

#: Provisions that limit the *use* of attributes § 381 carries over. Each is keyed to
#: the § 381(c) items it bears on, by the item's own subject matter. These are
#: pointers to further reading, not determinations that a limitation applies.
LIMITATIONS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    (
        "I.R.C. § 382",
        "limits the use of pre-change losses after an ownership change",
        ("net operating loss", "capital loss", "business interest"),
    ),
    (
        "I.R.C. § 383",
        "applies § 382-style limits to credits and capital loss carryovers",
        ("capital loss", "credit"),
    ),
    (
        "I.R.C. § 384",
        "limits the use of preacquisition losses against built-in gains",
        ("net operating loss", "capital loss"),
    ),
    (
        "I.R.C. § 269",
        "disallows attributes acquired for the principal purpose of tax avoidance",
        (),
    ),
    (
        "Treas. Reg. § 1.1502-21(c)",
        "applies the SRLY limitation where the target joins a consolidated group",
        ("net operating loss",),
    ),
)


@dataclass(frozen=True, slots=True)
class Item:
    """One attribute § 381(c) carries over."""

    number: str
    heading: str
    provision_id: str
    display: str
    repealed: bool

    @property
    def subject(self) -> str:
        """The heading lowercased, for matching against limitation keywords."""
        return self.heading.lower()


@dataclass(frozen=True, slots=True)
class Limitation:
    """A provision that limits the use of carried attributes."""

    citation: str
    effect: str
    items: list[Item] = field(default_factory=list)


@dataclass(slots=True)
class Checklist:
    """The attributes that carry over, and what constrains them."""

    items: list[Item] = field(default_factory=list)
    repealed: list[Item] = field(default_factory=list)
    limitations: list[Limitation] = field(default_factory=list)
    qualifying: list[str] = field(default_factory=list)
    operating_rule: str | None = None

    @property
    def live(self) -> int:
        """How many attributes still carry over."""
        return len(self.items)


def build(connection: sqlite3.Connection) -> Checklist | None:
    """Read the § 381(c) checklist out of the indexed Code.

    Returns:
        A :class:`Checklist`, or ``None`` if § 381 is not indexed.
    """
    subsection = db.get_provision(connection, ITEMS_SUBSECTION)
    if subsection is None:
        return None

    items: list[Item] = []
    repealed: list[Item] = []
    for provision in db.get_descendants(connection, ITEMS_SUBSECTION):
        if len(provision.path) != 2 or not provision.heading:
            continue
        entry = Item(
            number=provision.num or "",
            heading=provision.heading.strip(),
            provision_id=provision.id,
            display=cite_norm.display_for(provision.source, provision.section, provision.path),
            repealed=bool(_REPEALED_RE.match(provision.heading)),
        )
        (repealed if entry.repealed else items).append(entry)

    return Checklist(
        items=items,
        repealed=repealed,
        limitations=_limitations(items),
        qualifying=_qualifying(connection),
        operating_rule=_operating_rule(connection),
    )


def _limitations(items: list[Item]) -> list[Limitation]:
    """Match each limiting provision to the carried attributes it bears on."""
    out: list[Limitation] = []
    for citation, effect, keywords in LIMITATIONS:
        if not keywords:
            out.append(Limitation(citation=citation, effect=effect, items=[]))
            continue
        matched = [item for item in items if any(keyword in item.subject for keyword in keywords)]
        if matched:
            out.append(Limitation(citation=citation, effect=effect, items=matched))
    return out


def _qualifying(connection: sqlite3.Connection) -> list[str]:
    """Return § 381(a)'s conditions, in the statute's own words."""
    out: list[str] = []
    for provision in db.get_descendants(connection, QUALIFYING_SUBSECTION):
        if len(provision.path) != 2:
            continue
        text = " ".join(provision.text.split())
        if text:
            out.append(text.rstrip(" ;,"))
    return out


def _operating_rule(connection: sqlite3.Connection) -> str | None:
    """Return the opening of § 381(b), which carries the taxable-year rules."""
    provision: Provision | None = db.get_provision(connection, OPERATING_RULES)
    if provision is None:
        return None
    text = " ".join(provision.text.split())
    return text or None


def to_markdown(checklist: Checklist) -> str:
    """Render the checklist."""
    out: list[str] = ["# I.R.C. § 381 — attribute carryover", ""]
    out.append(
        f"§ 381(c) enumerates **{checklist.live} attributes** that an acquiring "
        f"corporation succeeds to, plus {len(checklist.repealed)} repealed items. "
        f"The list is closed: an attribute not on it does not carry over by virtue "
        f"of this section."
    )
    out.append("")

    if checklist.qualifying:
        out += ["## When § 381 applies at all", ""]
        out.append("§ 381(a) applies to the acquisition of assets of a corporation —")
        out.append("")
        for condition in checklist.qualifying:
            out.append(f"- {condition}")
        out.append("")
        out.append(
            "> Whether your transaction meets either condition is a question of "
            "characterisation. This does not answer it."
        )
        out.append("")

    if checklist.operating_rule:
        out += ["## Operating rules", "", f"§ 381(b): {checklist.operating_rule}", ""]

    out += ["## Attributes that carry over", "", "| § 381(c) | Attribute |", "|---|---|"]
    for item in checklist.items:
        out.append(f"| {item.number} | {item.heading} |")
    out.append("")

    if checklist.repealed:
        out += ["## Repealed, and therefore not carried", "", "| § 381(c) | Status |", "|---|---|"]
        for item in checklist.repealed:
            out.append(f"| {item.number} | {item.heading} |")
        out.append("")

    if checklist.limitations:
        out += [
            "## What limits the attributes once they arrive",
            "",
            "Carrying over is not the same as being usable. Each of these may restrict "
            "an attribute § 381 hands to the acquirer.",
            "",
            "| Provision | Effect | Bears on |",
            "|---|---|---|",
        ]
        for limitation in checklist.limitations:
            bears = (
                ", ".join(item.number for item in limitation.items)
                if limitation.items
                else "all attributes"
            )
            out.append(f"| {limitation.citation} | {limitation.effect} | {bears} |")
        out.append("")
        out.append(
            "These are pointers to further reading, not findings that a limitation "
            "applies. Whether § 382 bites depends on whether an ownership change "
            "occurred — test that with `taxcite owner-shift`."
        )
        out.append("")

    out.append(
        "> Read out of the indexed Code, not curated. The enumeration, the headings "
        "and the repeals are the statute's own; the limitation cross-references are "
        "signposts, and none of this decides whether your transaction qualifies."
    )
    return "\n".join(out)


__all__ = [
    "CARRYOVER_SECTION",
    "ITEMS_SUBSECTION",
    "LIMITATIONS",
    "Checklist",
    "Item",
    "Limitation",
    "build",
    "to_markdown",
]
