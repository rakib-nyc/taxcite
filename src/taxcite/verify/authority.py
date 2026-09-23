"""Which of these citations actually count? (roadmap 2.1).

Treas. Reg. § 1.6662-4(d)(3)(iii) sets out a **closed, enumerated list** of what counts
as authority for the substantial-authority standard, and says in terms that conclusions
reached in treatises, legal periodicals and practitioners' opinions are *not* authority.
It also imposes two date cutoffs — private rulings must post-date 31 October 1976, and
actions on decisions and general counsel memoranda 12 March 1981 — and provides that an
authority stops being one once it is overruled, revoked or superseded.

That is a specification, not a standard, which is what makes it automatable. It matters
because substantial authority is what keeps the § 6662 accuracy-related penalty off a
return position: a memo whose support rests on a treatise has a problem that is
completely invisible in the prose.

What this does **not** do is weigh the authorities. The regulation asks whether the
weight of authority supporting the position is substantial in relation to the weight
against it, and that is a judgment about relevance and persuasiveness that no program
should pretend to make. This answers the mechanical half: is the thing cited on the
list at all, and is it still good.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Final

from taxcite.models import Citation, RegKind, SourceType, Status

#: The regulation this module implements.
AUTHORITY_REGULATION: Final = "Treas. Reg. \u00a7 1.6662-4(d)(3)(iii)"

#: Private rulings issued on or before this date are not authority.
PRIVATE_RULING_CUTOFF: Final = date(1976, 10, 31)

#: Actions on decisions and general counsel memoranda on or before this are not.
INTERNAL_MEMO_CUTOFF: Final = date(1981, 3, 12)


class AuthorityKind(StrEnum):
    """The categories the regulation enumerates, plus the two it excludes."""

    STATUTE = "statute"
    REGULATION = "regulation"
    PUBLISHED_RULING = "published_ruling"
    TREATY = "treaty"
    CASE = "case"
    LEGISLATIVE_HISTORY = "legislative_history"
    BLUE_BOOK = "blue_book"
    PRIVATE_RULING = "private_ruling"
    INTERNAL_MEMORANDUM = "internal_memorandum"
    IRS_RELEASE = "irs_release"
    IRB_PRONOUNCEMENT = "irb_pronouncement"
    SECONDARY = "secondary"
    UNCLASSIFIED = "unclassified"


#: Every kind the regulation lists as authority.
AUTHORITY_KINDS: Final[frozenset[AuthorityKind]] = frozenset(
    {
        AuthorityKind.STATUTE,
        AuthorityKind.REGULATION,
        AuthorityKind.PUBLISHED_RULING,
        AuthorityKind.TREATY,
        AuthorityKind.CASE,
        AuthorityKind.LEGISLATIVE_HISTORY,
        AuthorityKind.BLUE_BOOK,
        AuthorityKind.PRIVATE_RULING,
        AuthorityKind.INTERNAL_MEMORANDUM,
        AuthorityKind.IRS_RELEASE,
        AuthorityKind.IRB_PRONOUNCEMENT,
    }
)

#: How each kind reads in a report.
KIND_LABELS: Final[dict[AuthorityKind, str]] = {
    AuthorityKind.STATUTE: "statute",
    AuthorityKind.REGULATION: "regulation",
    AuthorityKind.PUBLISHED_RULING: "published ruling",
    AuthorityKind.TREATY: "treaty",
    AuthorityKind.CASE: "court decision",
    AuthorityKind.LEGISLATIVE_HISTORY: "legislative history",
    AuthorityKind.BLUE_BOOK: "Blue Book",
    AuthorityKind.PRIVATE_RULING: "private ruling",
    AuthorityKind.INTERNAL_MEMORANDUM: "internal memorandum",
    AuthorityKind.IRS_RELEASE: "IRS release",
    AuthorityKind.IRB_PRONOUNCEMENT: "I.R.B. pronouncement",
    AuthorityKind.SECONDARY: "commentary",
    AuthorityKind.UNCLASSIFIED: "unclassified",
}

#: Guidance abbreviations mapped onto the regulation's categories.
_GUIDANCE_KINDS: Final[dict[str, AuthorityKind]] = {
    "Rev. Rul.": AuthorityKind.PUBLISHED_RULING,
    "Rev. Proc.": AuthorityKind.PUBLISHED_RULING,
    "Notice": AuthorityKind.IRB_PRONOUNCEMENT,
    "Ann.": AuthorityKind.IRB_PRONOUNCEMENT,
    "I.R.B.": AuthorityKind.IRB_PRONOUNCEMENT,
    "T.D.": AuthorityKind.REGULATION,
    "PLR": AuthorityKind.PRIVATE_RULING,
    "TAM": AuthorityKind.PRIVATE_RULING,
    "CCA": AuthorityKind.INTERNAL_MEMORANDUM,
    "GCM": AuthorityKind.INTERNAL_MEMORANDUM,
    "AOD": AuthorityKind.INTERNAL_MEMORANDUM,
    "FSA": AuthorityKind.INTERNAL_MEMORANDUM,
}

#: Private rulings and memoranda are numbered three different ways, and the year has
#: to be read out of the shape: hyphenated ``2012-01`` (year first), nine or more
#: digits ``202301001`` (four-digit year), or seven to eight digits ``7512001``
#: (two-digit year, the format used before 1999). A short sequential number such as a
#: general counsel memorandum's ``39000`` carries no year at all.
_HYPHENATED_RE: Final = re.compile(r"^(?P<year>\d{4})-\d+$")
_DIGITS_RE: Final = re.compile(r"^\d+$")


@dataclass(slots=True)
class AuthorityAssessment:
    """What one citation is, and whether the regulation counts it."""

    citation: Citation
    kind: AuthorityKind
    is_authority: bool
    reason: str
    caveats: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Return the human-readable kind."""
        return KIND_LABELS[self.kind]


def _ruling_year(number: str | None) -> int | None:
    """Return the year a private ruling or memorandum number encodes, if it does."""
    if not number:
        return None
    number = number.strip()
    hyphenated = _HYPHENATED_RE.match(number)
    if hyphenated is not None:
        return _plausible(int(hyphenated.group("year")))
    if not _DIGITS_RE.match(number):
        return None
    if len(number) >= 9:
        return _plausible(int(number[:4]))
    if len(number) in {7, 8}:
        # Two-digit year, used until the numbering changed in 1999.
        return _plausible(1900 + int(number[:2]))
    return None


def _plausible(year: int) -> int | None:
    """Return a year only if it could be a real issue date."""
    return year if 1950 <= year <= 2100 else None


def classify(citation: Citation) -> AuthorityAssessment:
    """Classify one citation against the enumerated list.

    The judgment is about the *type* of thing cited, never about whether it supports
    the proposition it is cited for.
    """
    match citation.source:
        case SourceType.IRC:
            return AuthorityAssessment(
                citation,
                AuthorityKind.STATUTE,
                True,
                "applicable provisions of the Internal Revenue Code",
            )
        case SourceType.REG:
            caveats: list[str] = []
            if citation.reg_kind is RegKind.PROPOSED:
                caveats.append(
                    "proposed regulations are authority, but carry less weight than "
                    "a final regulation on the same point"
                )
            if citation.reg_kind is RegKind.TEMPORARY:
                caveats.append(
                    "temporary regulations expire three years after issuance "
                    "(I.R.C. \u00a7 7805(e)); check that this one is still in force"
                )
            return AuthorityAssessment(
                citation,
                AuthorityKind.REGULATION,
                True,
                "proposed, temporary and final regulations construing the statute",
                caveats,
            )
        case SourceType.CASE:
            return AuthorityAssessment(
                citation,
                AuthorityKind.CASE,
                True,
                "court cases",
                [
                    "a decision is no longer authority once overruled or reversed by a "
                    "court with power to do so"
                ],
            )
        case SourceType.SECONDARY:
            return AuthorityAssessment(
                citation,
                AuthorityKind.SECONDARY,
                False,
                "conclusions reached in treatises, legal periodicals, legal opinions "
                "or opinions rendered by tax professionals are not authority",
                [
                    "the authorities underlying the commentary may themselves be "
                    "authority; cite those instead"
                ],
            )
        case _:
            return _classify_guidance(citation)


def _with_cutoff(
    citation: Citation,
    kind: AuthorityKind,
    year: int | None,
    cutoff: date,
    description: str,
    caveats: list[str],
) -> AuthorityAssessment:
    """Apply one of the regulation's two date cutoffs, honestly.

    Some of these documents are numbered by year and some sequentially. Where the
    number does not carry a year the cutoff cannot be checked, and saying so is better
    than implying it was.
    """
    if year is not None and year < cutoff.year:
        return AuthorityAssessment(
            citation,
            kind,
            False,
            f"{description} are authority only if issued after "
            f"{cutoff.isoformat()}; this one appears to be from {year}",
        )
    if year is None:
        return AuthorityAssessment(
            citation,
            kind,
            True,
            f"{description} issued after {cutoff.isoformat()}",
            [
                f"the issue date could not be read from this number, so the "
                f"{cutoff.isoformat()} cutoff has not been checked",
                *caveats,
            ],
        )
    return AuthorityAssessment(
        citation,
        kind,
        True,
        f"{description} issued after {cutoff.isoformat()}",
        list(caveats),
    )


def _classify_guidance(citation: Citation) -> AuthorityAssessment:
    """Classify IRS guidance, applying the regulation's two date cutoffs."""
    kind = _GUIDANCE_KINDS.get(citation.guidance_type or "", AuthorityKind.UNCLASSIFIED)
    if kind is AuthorityKind.UNCLASSIFIED:
        return AuthorityAssessment(
            citation,
            kind,
            False,
            "not recognised as one of the enumerated types of authority",
            ["classify this one by hand against " + AUTHORITY_REGULATION],
        )

    year = _ruling_year(citation.guidance_number)
    if kind is AuthorityKind.PRIVATE_RULING:
        return _with_cutoff(
            citation,
            kind,
            year,
            PRIVATE_RULING_CUTOFF,
            "private letter rulings and technical advice memoranda",
            [
                "a private ruling binds only the taxpayer who obtained it, and is not "
                "authority if revoked or inconsistent with a later published pronouncement"
            ],
        )
    if kind is AuthorityKind.INTERNAL_MEMORANDUM:
        return _with_cutoff(
            citation,
            kind,
            year,
            INTERNAL_MEMO_CUTOFF,
            "actions on decisions and general counsel memoranda",
            [],
        )
    if kind is AuthorityKind.PUBLISHED_RULING:
        return AuthorityAssessment(citation, kind, True, "revenue rulings and revenue procedures")
    if kind is AuthorityKind.REGULATION:
        return AuthorityAssessment(
            citation,
            kind,
            True,
            "a Treasury Decision promulgates regulations, which are authority",
        )
    return AuthorityAssessment(
        citation,
        kind,
        True,
        "notices, announcements and other administrative pronouncements published in "
        "the Internal Revenue Bulletin",
    )


# --------------------------------------------------------------------------------------
# Whole-document analysis
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class AuthorityReport:
    """The authority profile of one document."""

    assessments: list[AuthorityAssessment] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def authority(self) -> list[AuthorityAssessment]:
        """Return the citations the regulation counts."""
        return [a for a in self.assessments if a.is_authority]

    @property
    def not_authority(self) -> list[AuthorityAssessment]:
        """Return the citations the regulation does not count."""
        return [a for a in self.assessments if not a.is_authority]

    @property
    def counts(self) -> dict[str, int]:
        """Return how many citations fall into each kind."""
        out: dict[str, int] = {}
        for assessment in self.assessments:
            out[assessment.label] = out.get(assessment.label, 0) + 1
        return out


def analyse(
    citations: list[Citation],
    *,
    statuses: dict[str, Status] | None = None,
) -> AuthorityReport:
    """Classify every citation in a document.

    Args:
        citations: The citations found in the document.
        statuses: Verification statuses by canonical id, so that an authority which
            does not exist or is no longer operative can be called out. An authority
            that was repealed is not authority for a current position.
    """
    report = AuthorityReport()
    seen: set[str] = set()
    for citation in citations:
        key = citation.canonical_id or citation.display
        if key in seen:
            continue
        seen.add(key)
        assessment = classify(citation)
        status = (statuses or {}).get(key)
        if status is not None:
            _apply_status(assessment, status, report)
        report.assessments.append(assessment)
    return report


def _apply_status(assessment: AuthorityAssessment, status: Status, report: AuthorityReport) -> None:
    """Fold a verification status into an authority assessment."""
    if status in {Status.NOT_FOUND, Status.PINPOINT_NOT_FOUND}:
        assessment.is_authority = False
        assessment.reason = "does not exist, so it is not authority for anything"
        report.unresolved.append(assessment.citation.display)
    elif status in {Status.REPEALED, Status.SUPERSEDED}:
        assessment.is_authority = False
        assessment.reason = (
            "no longer operative; an authority stops being authority once it is "
            "overruled, repealed or superseded"
        )
    elif status is Status.NOT_YET_EFFECTIVE:
        assessment.caveats.append(
            "did not govern the tax year cited, so it is not authority for that year"
        )
    elif status is Status.SOURCE_UNAVAILABLE:
        assessment.caveats.append("could not be verified against the official source")


def to_markdown(report: AuthorityReport, *, tax_year: int | None = None) -> str:
    """Render an authority report."""
    year = f" for tax year {tax_year}" if tax_year else ""
    out = [
        f"# Authority analysis{year}",
        "",
        f"Classified against {AUTHORITY_REGULATION}, which lists what counts as "
        "authority for the substantial-authority standard under I.R.C. \u00a7 6662.",
        "",
    ]
    if not report.assessments:
        out += ["No citations found.", ""]
        return "\n".join(out)

    out += [
        f"**{len(report.authority)} authority \u00b7 {len(report.not_authority)} not authority**",
        "",
        "| Citation | Type | Counts? | Why |",
        "|---|---|---|---|",
    ]
    for assessment in report.assessments:
        verdict = "yes" if assessment.is_authority else "**NO**"
        out.append(
            f"| {_escape(assessment.citation.display)} | {assessment.label} "
            f"| {verdict} | {_escape(assessment.reason)} |"
        )
    out.append("")

    caveated = [a for a in report.assessments if a.caveats]
    if caveated:
        out += ["## Caveats", ""]
        for assessment in caveated:
            out.append(f"**{assessment.citation.display}**")
            out += [f"- {caveat}" for caveat in assessment.caveats]
            out.append("")

    out += [
        "> This is a classification, not a weighing. "
        f"{AUTHORITY_REGULATION} asks whether the weight of authority supporting a "
        "position is substantial in relation to the weight against it, which turns on "
        "relevance and persuasiveness. TaxCite answers only the mechanical half: "
        "whether what you cited is on the list, and whether it still exists.",
        "",
    ]
    return "\n".join(out)


def _escape(text: str) -> str:
    """Escape a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def analyse_text(
    text: str, *, connection: sqlite3.Connection, tax_year: int | None = None
) -> tuple[AuthorityReport, int | None]:
    """Verify a document and classify every authority in it."""
    from taxcite.verify import verify_text

    report = verify_text(text, offline=True, tax_year=tax_year, connection=connection)
    statuses = {
        (result.citation.canonical_id or result.citation.display): result.status
        for result in report.results
    }
    citations = [result.citation for result in report.results]
    return analyse(citations, statuses=statuses), tax_year
