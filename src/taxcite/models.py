"""Pydantic models for every public TaxCite data structure (SPEC section 5)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

ProvisionStatus = Literal["active", "repealed", "reserved", "omitted", "transferred"]
RefKind = Literal["explicit", "implicit", "relative"]


class SourceType(StrEnum):
    """Where a citation points."""

    IRC = "irc"
    REG = "reg"
    IRS_GUIDANCE = "irs_guidance"
    CASE = "case"
    SECONDARY = "secondary"
    OTHER = "other"


class RegKind(StrEnum):
    """Whether a regulation citation is final, temporary, or proposed."""

    FINAL = "final"
    TEMPORARY = "temporary"
    PROPOSED = "proposed"


class Status(StrEnum):
    """Outcome of verifying one citation or one quote."""

    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    PINPOINT_NOT_FOUND = "pinpoint_not_found"
    REPEALED = "repealed"
    RESERVED = "reserved"
    QUOTE_EXACT = "quote_exact"
    QUOTE_CLOSE = "quote_close"
    QUOTE_WRONG_PINPOINT = "quote_wrong_pinpoint"
    QUOTE_MISATTRIBUTED = "quote_misattributed"
    QUOTE_NOT_FOUND = "quote_not_found"
    NOT_YET_EFFECTIVE = "not_yet_effective"
    SUPERSEDED = "superseded"
    UNVERIFIABLE = "unverifiable"
    MALFORMED = "malformed"
    SOURCE_UNAVAILABLE = "source_unavailable"


class Severity(StrEnum):
    """How much a finding should worry the reader."""

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


_SEVERITY_BY_STATUS: dict[Status, Severity] = {
    Status.VERIFIED: Severity.OK,
    Status.QUOTE_EXACT: Severity.OK,
    Status.QUOTE_CLOSE: Severity.INFO,
    Status.UNVERIFIABLE: Severity.INFO,
    Status.REPEALED: Severity.WARNING,
    # Citing a provision for a year it did not govern is a substantive error, not a
    # formatting one: the text reads correctly and is simply the wrong law.
    Status.NOT_YET_EFFECTIVE: Severity.ERROR,
    Status.SUPERSEDED: Severity.WARNING,
    Status.RESERVED: Severity.WARNING,
    Status.QUOTE_WRONG_PINPOINT: Severity.WARNING,
    Status.MALFORMED: Severity.WARNING,
    Status.SOURCE_UNAVAILABLE: Severity.WARNING,
    Status.NOT_FOUND: Severity.ERROR,
    Status.PINPOINT_NOT_FOUND: Severity.ERROR,
    Status.QUOTE_MISATTRIBUTED: Severity.ERROR,
    Status.QUOTE_NOT_FOUND: Severity.ERROR,
}

SEVERITY_ORDER: dict[Severity, int] = {
    Severity.OK: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
}


def severity_for(status: Status) -> Severity:
    """Return the severity that SPEC section 5 assigns to ``status``."""
    return _SEVERITY_BY_STATUS[status]


class Citation(BaseModel):
    """One citation found in a document, parsed into components."""

    raw: str
    span: tuple[int, int]
    source: SourceType
    section: str | None = None
    path: list[str] = Field(default_factory=list)
    reg_kind: RegKind | None = None
    canonical_id: str | None = None
    display: str
    guidance_type: str | None = None
    guidance_number: str | None = None
    case_name: str | None = None
    reporter_cite: str | None = None
    pin_page: int | None = None
    """The page a case was pinpointed to, when the citation gives one."""
    case_year: int | None = None
    """The year a decision was handed down, as the citation states it."""
    warnings: list[str] = Field(default_factory=list)


class Provision(BaseModel):
    """One provision (a section or any subdivision of one) from an official source."""

    id: str
    source: SourceType
    section: str
    path: list[str] = Field(default_factory=list)
    level: str
    num: str
    heading: str | None = None
    text: str
    full_text: str
    parent_id: str | None = None
    status: ProvisionStatus = "active"
    source_version: str


class SearchHit(BaseModel):
    """One full-text search result."""

    provision_id: str
    display: str
    section: str
    source: SourceType
    heading: str | None
    snippet: str
    score: float


class QuoteCheck(BaseModel):
    """The result of checking one quoted passage against the source."""

    quote: str
    span: tuple[int, int]
    status: Status
    score: float
    matched_text: str | None = None
    matched_provision_id: str | None = None
    diff: str | None = None


class CitationResult(BaseModel):
    """The verification outcome for one citation, including any quotes attached to it."""

    citation: Citation
    status: Status
    severity: Severity
    message: str
    provision_heading: str | None = None
    source_url: str | None = None
    quotes: list[QuoteCheck] = Field(default_factory=list)
    suggestion: str | None = None

    @property
    def worst_severity(self) -> Severity:
        """Return the most serious severity among this citation and its quotes."""
        worst = self.severity
        for quote in self.quotes:
            sev = severity_for(quote.status)
            if SEVERITY_ORDER[sev] > SEVERITY_ORDER[worst]:
                worst = sev
        return worst


class VerificationReport(BaseModel):
    """The full result of verifying a document."""

    taxcite_version: str
    source_versions: dict[str, str] = Field(default_factory=dict)
    generated_at: datetime
    results: list[CitationResult] = Field(default_factory=list)
    orphan_quotes: list[QuoteCheck] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class CrossReference(BaseModel):
    """One edge of the cross-reference graph."""

    from_id: str
    to_id: str
    kind: RefKind
    external: bool = False
    raw: str | None = None
    heading: str | None = None
    display: str | None = None


class Definition(BaseModel):
    """A defined term located in the corpus."""

    term_display: str
    term_norm: str
    provision_id: str
    display: str
    scope: str | None = None
    definition_text: str
    by_reference: str | None = None
