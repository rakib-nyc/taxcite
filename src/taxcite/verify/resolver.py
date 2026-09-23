"""Resolving citations against the index (SPEC 6.6).

Resolution answers one question: does the cited provision exist, and is it still good
law? It never asks whether the provision supports the proposition it is cited for.
When something is wrong, the result carries a suggestion, because "§ 162(z) does not
exist" is much less useful than "§ 162 has subsections (a) through (r)".
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Final, Protocol

from rapidfuzz.distance import Levenshtein

from taxcite.citations import normalize as norm
from taxcite.citations.levels import level_at
from taxcite.config import (
    SUGGESTION_LIMIT,
    SUGGESTION_MAX_CHILDREN,
    SUGGESTION_MAX_EDIT_DISTANCE,
    SUGGESTION_NUMERIC_WINDOW,
)
from taxcite.errors import SourceUnavailableError
from taxcite.index import db
from taxcite.models import (
    SEVERITY_ORDER,
    Citation,
    CitationResult,
    Provision,
    RegKind,
    Severity,
    SourceType,
    Status,
    severity_for,
)

logger: Final = logging.getLogger("taxcite.resolver")

_LEADING_DIGITS: Final = re.compile(r"^(\d+)")


def _covered_years(connection: sqlite3.Connection) -> set[int]:
    """Return the years the Internal Revenue Bulletin was indexed for, in full."""
    recorded = db.get_meta(connection, "irb_years") or ""
    years: set[int] = set()
    for part in recorded.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            if low.isdigit() and high.isdigit():
                years.update(range(int(low), int(high) + 1))
        elif part.isdigit():
            years.add(int(part))
    return years


#: Relations that end a document's life, as opposed to merely qualifying it.
_SUPERSEDING: Final[frozenset[str]] = frozenset({"supersedes", "obsoletes", "revokes"})

#: Source statuses that make a provision no longer operative, and how to describe them.
_INOPERATIVE: Final[dict[str, tuple[Status, str]]] = {
    "repealed": (Status.REPEALED, "has been repealed"),
    "omitted": (Status.REPEALED, "has been omitted from the Code"),
    "transferred": (Status.REPEALED, "has been renumbered or transferred"),
    "reserved": (Status.RESERVED, "is reserved and has no text"),
}


class CaseFetcher(Protocol):
    """Looks a decision up by reporter citation and caches it."""

    def __call__(self, connection: sqlite3.Connection, reporter_cite: str) -> Any:
        """Return a case record, or ``None`` if no such decision is reported."""
        ...


class RegFetcher(Protocol):
    """Fetches a regulation section on demand and writes it into the index."""

    def __call__(self, connection: sqlite3.Connection, section: str) -> bool:
        """Fetch ``section``; return ``True`` if it is now in the index."""
        ...


class Resolver:
    """Resolves parsed citations against an open index."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        offline: bool = False,
        reg_fetcher: RegFetcher | None = None,
        case_fetcher: CaseFetcher | None = None,
        tax_year: int | None = None,
    ) -> None:
        """Create a resolver.

        Args:
            connection: An open index connection.
            offline: Never reach the network, even for an unindexed regulation.
            reg_fetcher: Callback that fetches a missing regulation section.
            case_fetcher: Callback that looks a decision up at CourtListener.
            tax_year: Check each provision against the year it is cited for, and
                report one that did not govern that year.
        """
        self.connection = connection
        self.offline = offline
        self.tax_year = tax_year
        self.reg_fetcher = reg_fetcher
        self.case_fetcher = case_fetcher
        self._sections: dict[SourceType, list[str]] | None = None
        self._unreachable: set[str] = set()

    # -- public ------------------------------------------------------------------

    def resolve(self, citation: Citation) -> CitationResult:
        """Resolve one citation into a :class:`CitationResult`."""
        if citation.source is SourceType.CASE and citation.reporter_cite:
            resolved = self._case(citation)
            if resolved is not None:
                return resolved
        if citation.source is SourceType.IRS_GUIDANCE and citation.guidance_type:
            resolved = self._guidance(citation)
            if resolved is not None:
                return resolved
        if citation.source in {
            SourceType.IRS_GUIDANCE,
            SourceType.CASE,
            SourceType.SECONDARY,
            SourceType.OTHER,
        }:
            return self._unverifiable(citation)
        if citation.canonical_id is None or citation.section is None:
            return self._result(
                citation,
                Status.MALFORMED,
                "could not be read as a citation to the Code or the regulations",
            )

        provision = self._get(citation)
        if provision is None:
            return self._missing(citation)
        return self._found(citation, provision)

    # -- lookup ------------------------------------------------------------------

    def _get(self, citation: Citation) -> Provision | None:
        """Fetch the cited provision, fetching the section on demand if allowed."""
        if citation.canonical_id is None:  # pragma: no cover - guarded by the caller
            return None
        provision = db.get_provision(self.connection, citation.canonical_id)
        if provision is not None:
            return provision
        fetchable = (
            citation.source is SourceType.REG
            and bool(citation.section)
            and not self.offline
            and self.reg_fetcher is not None
        )
        if fetchable and self.reg_fetcher is not None and citation.section is not None:
            try:
                if self.reg_fetcher(self.connection, citation.section):
                    return db.get_provision(self.connection, citation.canonical_id)
            except SourceUnavailableError as exc:
                # The eCFR could not be reached. Reporting the citation as nonexistent
                # would be a false accusation; say the source was unavailable instead.
                logger.info("eCFR unavailable for %s: %s", citation.section, exc)
                self._unreachable.add(citation.section)
        return None

    def _section_provision(self, citation: Citation) -> Provision | None:
        """Fetch the section-level provision that should contain the citation."""
        if citation.section is None:  # pragma: no cover - guarded by the caller
            return None
        return db.get_provision(
            self.connection, norm.canonical_id(citation.source, citation.section)
        )

    # -- outcomes ----------------------------------------------------------------

    def _guidance(self, citation: Citation) -> CitationResult | None:
        """Resolve a Revenue Ruling or similar against the indexed Bulletin.

        Returns ``None`` when the Bulletin has not been indexed, so the caller falls
        back to reporting the citation as unverifiable rather than as nonexistent.
        """
        from taxcite.sources.irb import KIND_SLUGS
        from taxcite.sources.irb import canonical_id as guidance_id

        kind = citation.guidance_type
        if kind is None or kind not in KIND_SLUGS or not citation.guidance_number:
            return None
        if db.count_guidance(self.connection) == 0:
            return None
        identifier = guidance_id(kind, citation.guidance_number)
        row = db.get_guidance(self.connection, identifier)
        if row is None:
            return self._unindexed_guidance(citation, identifier)
        summary = _guidance_summary(citation.display, str(row["title"]), str(row["text"]))
        acting = db.guidance_acting_on(self.connection, identifier)
        superseding = [r for r in acting if str(r["relation"]) in _SUPERSEDING]
        if superseding:
            latest = superseding[0]
            return self._result(
                citation,
                Status.SUPERSEDED,
                summary,
                heading=_heading_of(citation.display, str(row["title"])),
                suggestion=(
                    f"{latest['kind']} {latest['number']} states that it "
                    f"{latest['relation']} this document"
                ),
            )
        modifying = [r for r in acting if str(r["relation"]) not in _SUPERSEDING]
        suggestion = None
        if modifying:
            names = ", ".join(f"{r['kind']} {r['number']} ({r['relation']})" for r in modifying[:3])
            suggestion = f"later guidance acts on this document: {names}"
        return self._result(
            citation,
            Status.VERIFIED,
            summary,
            heading=_heading_of(citation.display, str(row["title"])),
            suggestion=suggestion,
        )

    def _case(self, citation: Citation) -> CitationResult | None:
        """Resolve a court decision against CourtListener.

        Returns ``None`` when no lookup is possible, so the caller falls back to
        reporting the citation as unverifiable. Existence and the case name can be
        checked; a pincite and a quotation cannot, and the result says so rather than
        letting a reader assume the whole citation was verified.
        """
        from taxcite.sources import courtlistener

        reporter = citation.reporter_cite
        if reporter is None:  # pragma: no cover - guarded by the caller
            return None
        record = courtlistener.cached(self.connection, reporter)
        if record is None:
            if self.offline or self.case_fetcher is None:
                return None
            try:
                record = self.case_fetcher(self.connection, reporter)
            except SourceUnavailableError as exc:
                logger.info("CourtListener unavailable for %s: %s", reporter, exc)
                return None
        if record is None:
            future = _dated_in_the_future(citation)
            if future is not None:
                # Certain, whatever the source's coverage: a decision cannot have been
                # handed down in a year that has not happened. Fabricated citations
                # carry implausible years often enough to be worth saying outright.
                return self._result(
                    citation,
                    Status.NOT_FOUND,
                    f"{reporter} is dated {future}, which is in the future",
                    suggestion="check the year; this citation cannot be right",
                )
            if courtlistener.coverage_is_partial(reporter):
                # Tax Court coverage in the citation database is demonstrably
                # incomplete: many clusters carry no citation at all, and T.C. Memo.
                # numbers are indexed for some years and not others. A miss here is not
                # evidence of nonexistence, and reporting one as fabricated would be a
                # false accusation on the citations tax practice uses most.
                return self._result(
                    citation,
                    Status.UNVERIFIABLE,
                    f"{reporter} was not found, but Tax Court citations are only "
                    f"partly indexed by the source, so this is not evidence that it "
                    f"does not exist",
                    suggestion=(
                        "check it against the Tax Court's own opinion search at "
                        "dawson.ustaxcourt.gov"
                    ),
                )
            return self._result(
                citation,
                Status.NOT_FOUND,
                f"no decision is reported at {reporter}",
                suggestion="check the volume, reporter, and page",
            )
        caveats: list[str] = []
        if not record.text:
            # Say exactly what was and was not checked. Quotations are still checked,
            # by asking the search backend whether their words occur in this decision;
            # what that cannot do is compare punctuation or confirm a page number.
            caveats.append(
                "the opinion text is not held locally, so the pinpoint page could not "
                "be checked and any quotation was matched on words alone"
                if citation.pin_page is not None
                else "the opinion text is not held locally, so any quotation was "
                "matched on words alone, without punctuation"
            )
        if citation.case_name:
            matched, score = courtlistener.names_match(citation.case_name, record.case_name)
            if not matched:
                return self._result(
                    citation,
                    Status.NOT_FOUND,
                    f"{reporter} is {record.case_name}, not {citation.case_name}",
                    heading=record.case_name,
                    suggestion=(
                        f"the citation and the case name disagree "
                        f"(similarity {score:.2f}); one of them is wrong"
                    ),
                    url=record.absolute_url,
                )
        described = courtlistener.describe_history(record)
        if described:
            caveats.append(described)
        # The standing boundary — that treatment is not checked — is printed once in
        # the report footer rather than repeated on every case, which made the table
        # unreadable and so made the warning easier to skip, not harder.
        return self._result(
            citation,
            Status.VERIFIED,
            f"{record.case_name}, {reporter} ({record.court})",
            heading=record.case_name,
            suggestion="; ".join(caveats),
            url=record.absolute_url,
        )

    def _unindexed_guidance(self, citation: Citation, identifier: str) -> CitationResult | None:
        """Handle a guidance citation that is not itself in the index.

        A document can be known to be dead without being indexed: a later Bulletin
        says it supersedes it. That is worth reporting. Otherwise the answer depends
        on whether the year was indexed in full — if it was not, TaxCite does not know
        that the document is missing, only that it has not looked, and must say so.
        """
        acting = db.guidance_acting_on(self.connection, identifier)
        superseding = [r for r in acting if str(r["relation"]) in _SUPERSEDING]
        if superseding:
            latest = superseding[0]
            return self._result(
                citation,
                Status.SUPERSEDED,
                f"{citation.display} has been {latest['relation'][:-1]}ed",
                suggestion=(
                    f"{latest['kind']} {latest['number']} states that it "
                    f"{latest['relation']} this document"
                ),
            )
        if not self._guidance_year_is_covered(citation.guidance_number or ""):
            return None
        return self._result(
            citation,
            Status.NOT_FOUND,
            f"{citation.display} was not published in the indexed Bulletins",
            suggestion="check the number, or widen the indexed years",
        )

    def _guidance_year_is_covered(self, number: str) -> bool:
        """Return ``True`` if the Bulletin was indexed for that whole year.

        Indexing one week of 2019 says nothing about whether a given 2019 ruling
        exists, so coverage is read from what the build recorded, not from what
        happens to be in the table.
        """
        head = number.split("-", 1)[0]
        if not head.isdigit() or len(head) != 4:
            return False
        return int(head) in _covered_years(self.connection)

    def _unverifiable(self, citation: Citation) -> CitationResult:
        """Build the result for a citation TaxCite recognises but cannot check."""
        what = {
            SourceType.CASE: "Court decisions are",
            SourceType.IRS_GUIDANCE: "IRS sub-regulatory guidance is",
            SourceType.SECONDARY: "Commentary is",
            SourceType.OTHER: "Taxpayer-specific guidance is",
        }[citation.source]
        return self._result(
            citation,
            Status.UNVERIFIABLE,
            f"recognised but not verified: {what} outside TaxCite's sources",
        )

    def _missing(self, citation: Citation) -> CitationResult:
        """Build the result for a citation whose target is not in the index."""
        section_provision = self._section_provision(citation)
        if section_provision is None:
            if citation.source is SourceType.REG:
                return self._reg_unavailable(citation)
            return self._result(
                citation,
                Status.NOT_FOUND,
                f"{citation.display} does not exist",
                suggestion=self._nearest_sections_message(citation),
            )
        return self._pinpoint_not_found(citation, section_provision)

    def _reg_unavailable(self, citation: Citation) -> CitationResult:
        """Build the result for a regulation that is neither cached nor fetchable."""
        if self.offline:
            return self._result(
                citation,
                Status.SOURCE_UNAVAILABLE,
                f"{citation.display} is not in the local index and TaxCite is offline",
                heading=None,
            )
        if self.reg_fetcher is None or citation.section in self._unreachable:
            return self._result(
                citation,
                Status.SOURCE_UNAVAILABLE,
                f"{citation.display} could not be retrieved from the eCFR",
            )
        return self._result(
            citation,
            Status.NOT_FOUND,
            f"{citation.display} does not exist in the current eCFR",
            suggestion=self._nearest_sections_message(citation),
        )

    def _pinpoint_not_found(
        self, citation: Citation, section_provision: Provision
    ) -> CitationResult:
        """Build the result for an existing section with a subdivision that is not."""
        ancestor, children = self._deepest_existing_ancestor(citation, section_provision)
        label = norm.display_for(citation.source, ancestor.section, ancestor.path)
        missing = (
            citation.path[len(ancestor.path)] if len(citation.path) > len(ancestor.path) else ""
        )
        parts: list[str] = []
        if children:
            level = _child_level(citation.source, len(ancestor.path))
            parts.append(f"{label} has {level} {_summarize_nums(children)}")
            if missing:
                parts.append(f"no ({missing})")
        else:
            parts.append(f"{label} has no subdivisions")
        sibling = self._letter_suffix_sibling(citation)
        if sibling is not None:
            parts.append(f"did you mean {sibling}?")
        return self._result(
            citation,
            Status.PINPOINT_NOT_FOUND,
            f"{citation.display} does not exist; "
            f"{norm.display_for(citation.source, section_provision.section)} does",
            heading=section_provision.heading,
            suggestion="; ".join(parts),
        )

    def _found(self, citation: Citation, provision: Provision) -> CitationResult:
        """Build the result for a provision that exists."""
        inoperative = _INOPERATIVE.get(provision.status)
        if inoperative is not None:
            status, description = inoperative
            message = f"{citation.display} {description}"
            if provision.heading:
                message = f"{message} ({provision.heading})"
            return self._result(citation, status, message, heading=provision.heading)
        if self.tax_year is not None:
            timing = self._timing(citation, provision)
            if timing is not None:
                return timing
        sunset = self._temporary_sunset(citation, provision)
        if sunset is not None:
            return sunset
        message = f"{citation.display} exists"
        suggestion = None
        if citation.reg_kind is RegKind.PROPOSED:
            message = f"{message}; cited as proposed, but a final regulation exists"
        if citation.warnings:
            suggestion = "; ".join(citation.warnings)
        return self._result(
            citation,
            Status.VERIFIED,
            message,
            heading=provision.heading,
            suggestion=suggestion,
        )

    def _temporary_sunset(self, citation: Citation, provision: Provision) -> CitationResult | None:
        """Report a temporary regulation whose three-year clock has run."""
        from taxcite.verify import effective

        temporary = effective.temporary_sunset(self.connection, provision.id)
        if temporary is None or not temporary.expired:
            return None
        if not temporary.sunset_applies:
            return self._result(
                citation,
                Status.VERIFIED,
                f"{citation.display} exists",
                heading=provision.heading,
                suggestion=(
                    f"issued {temporary.issued.isoformat()}, before I.R.C. \u00a7 7805(e) "
                    "applied, so the three-year sunset does not reach it"
                ),
            )
        return self._result(
            citation,
            Status.SUPERSEDED,
            f"{citation.display} was issued {temporary.issued.isoformat()} and a "
            f"temporary regulation expires three years after issuance "
            f"(I.R.C. \u00a7 7805(e))",
            heading=provision.heading,
            suggestion=(
                f"it appears to have expired on {temporary.expires.isoformat()}; "
                "check whether a final regulation replaced it"
            ),
        )

    def _timing(self, citation: Citation, provision: Provision) -> CitationResult | None:
        """Report a provision cited for a tax year it did not govern."""
        from taxcite.verify import effective

        if self.tax_year is None:  # pragma: no cover - guarded by the caller
            return None
        status, explanation, pending = effective.summarize_for_year(
            self.connection, provision.id, self.tax_year
        )
        if status != "not_yet":
            if pending:
                nearest = pending[0]
                return self._result(
                    citation,
                    Status.VERIFIED,
                    f"{citation.display} exists",
                    heading=provision.heading,
                    suggestion=(
                        f"note: an amendment applies only to years after "
                        f"{nearest.applies_after.isoformat()}"
                        f"{f' ({nearest.heading})' if nearest.heading else ''}, so the "
                        f"current text may not be the text for {self.tax_year}"
                    ),
                )
            return None
        return self._result(
            citation,
            Status.NOT_YET_EFFECTIVE,
            explanation or f"{citation.display} did not govern tax year {self.tax_year}",
            heading=provision.heading,
            suggestion=f"verify the law in force for tax year {self.tax_year}",
        )

    def _result(
        self,
        citation: Citation,
        status: Status,
        message: str,
        *,
        heading: str | None = None,
        suggestion: str | None = None,
        url: str | None = None,
    ) -> CitationResult:
        """Assemble a :class:`CitationResult` with the right severity and URL."""
        return CitationResult(
            citation=citation,
            status=status,
            severity=severity_for(status),
            message=message,
            provision_heading=heading,
            source_url=url or self._source_url(citation),
            suggestion=suggestion,
        )

    def _source_url(self, citation: Citation) -> str | None:
        """Return the official URL for a citation, including indexed guidance."""
        if citation.source is SourceType.IRS_GUIDANCE and citation.guidance_type:
            from taxcite.sources.irb import KIND_SLUGS
            from taxcite.sources.irb import canonical_id as guidance_id

            if citation.guidance_type in KIND_SLUGS and citation.guidance_number:
                row = db.get_guidance(
                    self.connection,
                    guidance_id(citation.guidance_type, citation.guidance_number),
                )
                if row is not None:
                    return str(row["url"])
        return norm.source_url_for(citation.source, citation.section, citation.path)

    # -- suggestions -------------------------------------------------------------

    def _deepest_existing_ancestor(
        self, citation: Citation, section_provision: Provision
    ) -> tuple[Provision, list[Provision]]:
        """Walk down the cited path as far as the index goes."""
        current = section_provision
        for depth in range(1, len(citation.path)):
            candidate = db.get_provision(
                self.connection,
                norm.canonical_id(citation.source, current.section, citation.path[:depth]),
            )
            if candidate is None:
                break
            current = candidate
        return current, db.get_children(self.connection, current.id)

    def _letter_suffix_sibling(self, citation: Citation) -> str | None:
        """Spot the ``§ 263(a)`` / ``§ 263A`` confusion and name the other section."""
        if not citation.path or citation.section is None:
            return None
        token = citation.path[0]
        if len(token) != 1 or not token.isalpha():
            return None
        sibling = f"{citation.section}{token.upper()}"
        if sibling in self._known_sections(citation.source):
            return norm.display_for(citation.source, sibling)
        return None

    def _known_sections(self, source: SourceType) -> list[str]:
        """Return, and cache, every indexed section number for a source."""
        if self._sections is None:
            self._sections = {}
        if source not in self._sections:
            self._sections[source] = db.section_numbers(self.connection, source)
        return self._sections[source]

    def nearest_sections(self, citation: Citation) -> list[str]:
        """Return up to three plausible section numbers for a section that is missing.

        Candidates are sections within a small edit distance, sections numerically
        adjacent to the one cited, and the letter-suffix variants that produce the
        classic ``263(a)`` / ``263A`` mix-up. They are ranked by edit distance, then by
        numeric distance.
        """
        if citation.section is None:
            return []
        wanted = citation.section
        known = self._known_sections(citation.source)
        target_number = _leading_number(wanted)

        candidates: dict[str, tuple[int, int]] = {}
        for section in known:
            distance = Levenshtein.distance(
                wanted.casefold(),
                section.casefold(),
                score_cutoff=SUGGESTION_MAX_EDIT_DISTANCE,
            )
            numeric_gap = _numeric_gap(target_number, section)
            if distance <= SUGGESTION_MAX_EDIT_DISTANCE:
                candidates[section] = (distance, numeric_gap)
            elif numeric_gap <= SUGGESTION_NUMERIC_WINDOW:
                candidates[section] = (SUGGESTION_MAX_EDIT_DISTANCE + 1, numeric_gap)
        for variant in _variants(wanted):
            if variant in known and variant not in candidates:
                candidates[variant] = (1, _numeric_gap(target_number, variant))

        ranked = sorted(candidates, key=lambda s: (*candidates[s], len(s), s))
        return ranked[:SUGGESTION_LIMIT]

    def _nearest_sections_message(self, citation: Citation) -> str | None:
        """Render the nearest-section suggestions as a sentence."""
        nearest = self.nearest_sections(citation)
        if not nearest:
            return None
        rendered = ", ".join(norm.display_for(citation.source, s) for s in nearest)
        return f"did you mean {rendered}?"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _guidance_summary(display: str, title: str, text: str, limit: int = 140) -> str:
    """Describe a guidance document without repeating its own citation back."""
    cleaned = " ".join(title.split())
    if cleaned and display not in cleaned and cleaned not in display:
        return f"{display} \u2014 {cleaned}"
    blurb = " ".join(text.split())
    # A long procedure opens with its own table of contents, which describes nothing.
    if not blurb or blurb.upper().startswith("TABLE OF CONTENTS"):
        return f"{display} exists"
    if len(blurb) > limit:
        blurb = blurb[:limit].rsplit(" ", 1)[0] + "\u2026"
    return f"{display} \u2014 {blurb}"


def _heading_of(display: str, title: str) -> str | None:
    """Return a title worth showing, or nothing when it just repeats the citation."""
    cleaned = " ".join(title.split())
    return cleaned if cleaned and cleaned not in display else None


def _plural(count: int, noun: str) -> str:
    """Return ``noun`` pluralised for ``count``."""
    return noun if count == 1 else f"{noun}s"


def _child_level(source: SourceType, parent_depth: int) -> str:
    """Return the plural level name of a provision's children."""
    return _plural(2, level_at(parent_depth, reg=source is SourceType.REG))


def _summarize_nums(children: list[Provision]) -> str:
    """Render sibling numbers compactly: a contiguous run collapses to a range."""
    nums = [child.num for child in children]
    if len(nums) > 2 and _is_contiguous(nums):
        return f"{nums[0]}\u2013{nums[-1]}"
    if len(nums) <= SUGGESTION_MAX_CHILDREN:
        return ", ".join(nums)
    shown = ", ".join(nums[:SUGGESTION_MAX_CHILDREN])
    return f"{shown}, \u2026 {nums[-1]}"


def _is_contiguous(nums: list[str]) -> bool:
    """Return ``True`` if ``(a)``..``(s)`` or ``(1)``..``(9)`` with nothing missing."""
    tokens = [num.strip("()") for num in nums]
    if all(token.isdigit() for token in tokens):
        values = [int(token) for token in tokens]
        return values == list(range(values[0], values[0] + len(values)))
    if all(len(token) == 1 and token.isalpha() for token in tokens):
        codes = [ord(token) for token in tokens]
        return codes == list(range(codes[0], codes[0] + len(codes)))
    return False


def _leading_number(section: str) -> int | None:
    """Return the leading integer of a section number, if it has one."""
    match = _LEADING_DIGITS.match(section)
    return int(match.group(1)) if match else None


def _numeric_gap(target: int | None, section: str) -> int:
    """Return how far a section is from ``target`` numerically."""
    other = _leading_number(section)
    if target is None or other is None:
        return SUGGESTION_NUMERIC_WINDOW + 1
    return abs(target - other)


def _variants(section: str) -> set[str]:
    """Generate the near-misses people actually make when citing a section number."""
    out: set[str] = set()
    digits = _LEADING_DIGITS.match(section)
    if digits:
        stem = digits.group(1)
        suffix = section[len(stem) :]
        if suffix:
            out.add(stem)  # 263A -> 263
        for letter in "ABCDT":
            out.add(f"{stem}{letter}")  # 45 -> 45A, 263 -> 263A
        if len(stem) > 1:
            for index in range(len(stem) - 1):
                swapped = list(stem)
                swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
                out.add("".join(swapped) + suffix)  # 1256 -> 2156
    out.discard(section)
    return out


def resolve_citations(
    connection: sqlite3.Connection,
    citations: list[Citation],
    *,
    offline: bool = False,
    reg_fetcher: RegFetcher | None = None,
) -> list[CitationResult]:
    """Resolve a list of citations against the index."""
    resolver = Resolver(connection, offline=offline, reg_fetcher=reg_fetcher)
    return [resolver.resolve(citation) for citation in citations]


def worst_severity(results: list[CitationResult]) -> Severity:
    """Return the most serious severity across a list of results."""
    worst = Severity.OK
    for result in results:
        if SEVERITY_ORDER[result.worst_severity] > SEVERITY_ORDER[worst]:
            worst = result.worst_severity
    return worst


def _dated_in_the_future(citation: Citation) -> int | None:
    """Return the year a citation claims, when that year has not arrived.

    This is the one thing that can be said about a decision with certainty without
    consulting any source, and it costs nothing. It matters because a fabricated
    citation often carries a year that has not happened.
    """
    from datetime import UTC, datetime

    year = citation.case_year
    if year is None:
        return None
    return year if year > datetime.now(UTC).year else None
