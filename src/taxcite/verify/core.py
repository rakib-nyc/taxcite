"""The public verification entry point (SPEC 8).

``verify_text`` is the whole product in one function: extract every citation from a
document, resolve each one against the official sources, check any quoted language,
and return a structured report.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from taxcite import __version__
from taxcite.api import open_index
from taxcite.citations import extract_citations
from taxcite.index import db
from taxcite.models import CitationResult, QuoteCheck, Severity, VerificationReport, severity_for
from taxcite.verify.quotes import CaseProber, verify_quotes
from taxcite.verify.resolver import CaseFetcher, RegFetcher, Resolver

logger = logging.getLogger("taxcite.verify")


def default_case_fetcher(offline: bool) -> CaseFetcher | None:
    """Return the CourtListener lookup, or ``None`` when offline."""
    if offline:
        return None
    from taxcite.sources.courtlistener import make_fetcher
    from taxcite.sources.http import HttpClient

    return cast("CaseFetcher", make_fetcher(HttpClient()))


def default_case_prober(offline: bool) -> CaseProber | None:
    """Return the keyless case-quotation prober, or ``None`` when offline.

    Checking a quotation from a decision costs a handful of small search requests, so
    it happens only for quotations actually attributed to a case, and only when the
    opinion text is not already held locally.
    """
    if offline:
        return None
    from taxcite.errors import SourceParseError, SourceUnavailableError
    from taxcite.sources.courtlistener import ProbeBudget, longest_matching_prefix
    from taxcite.sources.http import HttpClient

    client = HttpClient()
    # One budget per run, so a long memo cannot exhaust the source's quota and turn
    # every later case quotation into a reported outage.
    budget = ProbeBudget()

    def probe(reporter_cite: str, passage: str) -> int | None:
        try:
            return longest_matching_prefix(client, reporter_cite, passage, budget)
        except (SourceUnavailableError, SourceParseError):
            # An unreachable source is not evidence that a quotation is wrong.
            logger.debug("case quotation probe failed for %s", reporter_cite, exc_info=True)
            return None

    return probe


def default_reg_fetcher(offline: bool) -> RegFetcher | None:
    """Return the on-demand regulation fetcher, or ``None`` when offline.

    A regulation citation that is not in the index triggers a single-section fetch from
    the eCFR, which is parsed and written through to the index (SPEC 4.2). Offline mode
    skips it, and such citations come back as ``SOURCE_UNAVAILABLE``.
    """
    if offline:
        return None
    from taxcite.sources.ecfr import make_fetcher
    from taxcite.sources.http import HttpClient

    return cast("RegFetcher", make_fetcher(HttpClient()))


def verify_text(
    text: str,
    *,
    offline: bool = False,
    as_of: date | None = None,
    tax_year: int | None = None,
    connection: sqlite3.Connection | None = None,
    path: Path | None = None,
    reg_fetcher: RegFetcher | None = None,
    check_case_quotes: bool = True,
) -> VerificationReport:
    """Verify every citation and quotation in ``text``.

    Args:
        text: The document to check, as Markdown or plain text.
        offline: Never reach the network; regulations that are not cached are reported
            as ``SOURCE_UNAVAILABLE`` rather than fetched.
        tax_year: Report any provision that did not govern this tax year.
        as_of: Verify against the law as it stood on this date rather than current law.
            Regulations are not fetched on demand in this mode, because the eCFR
            snapshot for a past date is a separate question from the Code's.
        connection: An open index connection; one is opened if omitted.
        path: An index path, when opening a connection.
        reg_fetcher: Callback used to fetch a regulation section on demand. Defaults to
            the eCFR client unless ``offline``.
        check_case_quotes: Check quotations attributed to court decisions. Unless the
            opinion text is held locally, this is the one operation that sends text
            from the document — the quoted passage itself — to an outside service, so
            it can be declined without giving up the rest of verification (see
            docs/privacy.md). Such quotations are then reported as unverifiable.

    Returns:
        A :class:`~taxcite.models.VerificationReport`.
    """
    historical = as_of is not None
    fetcher = reg_fetcher if reg_fetcher is not None else default_reg_fetcher(offline or historical)
    prober = default_case_prober(offline or historical or not check_case_quotes)
    if connection is not None:
        return _verify(
            text,
            connection,
            offline=offline,
            reg_fetcher=fetcher,
            case_prober=prober,
            as_of=as_of,
            tax_year=tax_year,
        )
    with open_index(path, as_of=as_of) as conn:
        return _verify(
            text,
            conn,
            offline=offline,
            reg_fetcher=fetcher,
            case_prober=prober,
            as_of=as_of,
            tax_year=tax_year,
        )


def _verify(
    text: str,
    connection: sqlite3.Connection,
    *,
    offline: bool,
    reg_fetcher: RegFetcher | None,
    case_prober: CaseProber | None = None,
    as_of: date | None = None,
    tax_year: int | None = None,
) -> VerificationReport:
    """Verify ``text`` against an open index."""
    resolver = Resolver(
        connection,
        offline=offline,
        reg_fetcher=reg_fetcher,
        case_fetcher=default_case_fetcher(offline or as_of is not None),
        tax_year=tax_year,
    )
    citations = extract_citations(text)
    results = [resolver.resolve(citation) for citation in citations]
    orphans: list[QuoteCheck] = verify_quotes(
        connection,
        text,
        citations,
        results,
        case_prober=case_prober,
    )
    versions = db.source_versions(connection)
    if as_of is not None:
        versions["as_of"] = as_of.isoformat()
    if tax_year is not None:
        versions["tax_year"] = str(tax_year)
    return VerificationReport(
        taxcite_version=__version__,
        source_versions=versions,
        generated_at=datetime.now(UTC),
        results=results,
        orphan_quotes=orphans,
        summary=summarize(results, orphans),
    )


def summarize(results: list[CitationResult], orphans: list[QuoteCheck]) -> dict[str, int]:
    """Count findings by status and by severity."""
    summary: dict[str, int] = {"citations": len(results), "quotes": 0}
    for result in results:
        summary[result.status.value] = summary.get(result.status.value, 0) + 1
        summary[result.severity.value] = summary.get(result.severity.value, 0) + 1
        for quote in result.quotes:
            summary["quotes"] += 1
            summary[quote.status.value] = summary.get(quote.status.value, 0) + 1
            severity = severity_for(quote.status)
            summary[severity.value] = summary.get(severity.value, 0) + 1
    for quote in orphans:
        summary["quotes"] += 1
        summary["orphan_quotes"] = summary.get("orphan_quotes", 0) + 1
        summary[quote.status.value] = summary.get(quote.status.value, 0) + 1
        severity = severity_for(quote.status)
        summary[severity.value] = summary.get(severity.value, 0) + 1
    for severity_name in Severity:
        summary.setdefault(severity_name.value, 0)
    return summary
