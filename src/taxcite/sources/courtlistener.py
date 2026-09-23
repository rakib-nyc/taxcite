"""Court decisions from CourtListener (roadmap 1.4).

The Free Law Project publishes a keyless, open API over several million opinions,
including the Tax Court, the Court of Federal Claims and the circuits. That closes the
other large gap in TaxCite's coverage: a case citation used to be reported as
unverifiable whether it was real or invented.

**What can and cannot be checked keylessly matters here.** The keyless search endpoint
returns metadata — case name, reporter citations, court, date — but not opinion text.
So TaxCite can answer "does a decision exist at this citation" and "is the name
attached to it the right one", which are two of the five error types the literature
identifies. It cannot check a pincite or a quotation, and it says so rather than
staying quiet, because a reader who sees a case pass has to know what passed.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Final

from rapidfuzz import fuzz

from taxcite.config import (
    CASE_DIVERGENCE_MAX_PROBES,
    CASE_NAME_MATCH_THRESHOLD,
    CASE_PHRASE_MIN_WORDS,
    CASE_PROBE_BUDGET,
    COURTLISTENER_MAX_RESULTS,
    COURTLISTENER_OPINIONS_URL,
    COURTLISTENER_SEARCH_URL,
    COURTLISTENER_TOKEN_ENV,
)
from taxcite.errors import SourceNotFoundError, SourceUnavailableError
from taxcite.sources.http import HttpClient

logger: Final = logging.getLogger("taxcite.courtlistener")

_WS_RE: Final = re.compile(r"\s+")

#: Words that carry no signal when comparing two case names.
_NAME_NOISE: Final = re.compile(
    r"\b(?:the|of|et al\.?|inc\.?|corp\.?|co\.?|llc|l\.l\.c\.|ltd\.?|"
    r"estate of|in re|ex rel\.?)\b",
    re.IGNORECASE,
)

#: How the Commissioner is named varies; treat the variants as one party.
_COMMISSIONER: Final = re.compile(
    r"\bcomm(?:issioner|'?r)\b(?:\s+of\s+internal\s+revenue)?", re.IGNORECASE
)


def canonical_id(reporter_cite: str) -> str:
    """Return a canonical identifier for a reported decision."""
    slug = re.sub(r"[^0-9a-z]+", "-", reporter_cite.lower()).strip("-")
    return f"/us/case/{slug}"


def normalize_name(name: str) -> str:
    """Reduce a case name to the part worth comparing."""
    lowered = _COMMISSIONER.sub("commissioner", name.lower())
    lowered = _NAME_NOISE.sub(" ", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return _WS_RE.sub(" ", lowered).strip()


def names_match(cited: str, reported: str) -> tuple[bool, float]:
    """Compare a cited case name with the one the reporter actually carries.

    Returns the verdict and the score, so a near miss can be shown rather than
    silently accepted or silently rejected.
    """
    left, right = normalize_name(cited), normalize_name(reported)
    if not left or not right:
        return True, 1.0
    score = (
        max(
            fuzz.token_set_ratio(left, right),
            fuzz.partial_ratio(left, right),
        )
        / 100.0
    )
    return score >= CASE_NAME_MATCH_THRESHOLD, score


@dataclass(slots=True)
class CaseRecord:
    """One reported decision, as CourtListener describes it."""

    id: str
    reporter_cite: str
    case_name: str
    court: str
    date_filed: str | None
    url: str
    citations: list[str] = field(default_factory=list)
    cite_count: int = 0
    cluster_id: int | None = None
    opinion_id: int | None = None
    text: str | None = None
    excerpt: str | None = None
    citing_count: int | None = None
    last_cited: str | None = None
    last_citing: str | None = None

    @property
    def has_text(self) -> bool:
        """Return ``True`` if the opinion text is available locally."""
        return bool(self.text)

    @property
    def absolute_url(self) -> str:
        """Return the full CourtListener URL."""
        if self.url.startswith("http"):
            return self.url
        return f"https://www.courtlistener.com{self.url}"


#: "T.C. Memo. 2020-12" is how practitioners write it; CourtListener stores the same
#: decision as "2020 T.C. Memo. 12". Neither form finds the other, so a citation in the
#: ordinary form found nothing and was reported as a decision that does not exist.
_TC_MEMO_RE: Final = re.compile(
    r"^T\.\s?C\.\s?Memo\.?\s+(?P<year>\d{4})-(?P<number>\d+)$", re.IGNORECASE
)

#: Reporters whose coverage in the citation database is demonstrably incomplete: many
#: Tax Court clusters carry no citation at all, and T.C. Memo. numbers are present for
#: some years and absent for others. A miss in these reporters is therefore not evidence
#: that the decision does not exist, and must not be reported as one.
_PARTIAL_COVERAGE_RE: Final = re.compile(r"\bT\.\s?C\.|\bB\.\s?T\.\s?A\.", re.IGNORECASE)


def coverage_is_partial(reporter_cite: str) -> bool:
    """Return ``True`` where a miss is not good evidence of nonexistence."""
    return bool(_PARTIAL_COVERAGE_RE.search(reporter_cite))


def query_forms(reporter_cite: str) -> list[str]:
    """Return the citation as written, plus any form the source stores it under."""
    forms = [reporter_cite]
    memo = _TC_MEMO_RE.match(reporter_cite.strip())
    if memo is not None:
        forms.append(f"{memo['year']} T.C. Memo. {int(memo['number'])}")
    return forms


def _matches_cite(record: dict[str, Any], wanted: str) -> bool:
    """Return ``True`` if a search result actually carries the citation asked for."""
    target = _WS_RE.sub(" ", wanted).strip().lower()
    for citation in record.get("citation") or []:
        if _WS_RE.sub(" ", str(citation)).strip().lower() == target:
            return True
    return False


def lookup(client: HttpClient, reporter_cite: str) -> CaseRecord | None:
    """Look up a decision by its reporter citation.

    Returns ``None`` when CourtListener has no decision at that citation, which for a
    published reporter cite is good evidence that it does not exist.

    Raises:
        SourceUnavailableError: if CourtListener cannot be reached.
    """
    for form in query_forms(reporter_cite):
        found = _lookup_one(client, reporter_cite, form)
        if found is not None:
            return found
    return None


def _lookup_one(client: HttpClient, reporter_cite: str, form: str) -> CaseRecord | None:
    """Look the decision up under one spelling of its citation."""
    try:
        payload = client.get_json(
            COURTLISTENER_SEARCH_URL,
            # A bare phrase query returns everything that *cites* the case, ranked by
            # relevance, and the case itself may not be in the first page. The
            # citation field with a quoted value is an exact match on the reporter
            # cite, which is the question actually being asked.
            params={"q": f'citation:"{form}"', "type": "o"},
        )
    except SourceNotFoundError:
        return None
    results = payload.get("results") or []
    for record in results[:COURTLISTENER_MAX_RESULTS]:
        if not _matches_cite(record, form):
            continue
        citations = [str(c) for c in (record.get("citation") or [])]
        opinions = record.get("opinions") or []
        first = opinions[0] if opinions else {}
        return CaseRecord(
            id=canonical_id(reporter_cite),
            reporter_cite=reporter_cite,
            case_name=str(record.get("caseName") or ""),
            court=str(record.get("court") or ""),
            date_filed=record.get("dateFiled"),
            url=str(record.get("absolute_url") or ""),
            citations=citations,
            cite_count=int(record.get("citeCount") or 0),
            cluster_id=_as_int(record.get("cluster_id")),
            opinion_id=_as_int(first.get("id")),
            excerpt=_clean_excerpt(first.get("snippet")),
        )
    return None


def _as_int(value: Any) -> int | None:
    """Coerce a JSON value to an int, or ``None``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_excerpt(value: Any) -> str | None:
    """Tidy the short opinion excerpt the keyless search returns."""
    if not value:
        return None
    return _strip_markup(str(value)) or None


def index_case(connection: sqlite3.Connection, case: CaseRecord) -> None:
    """Write a decision into the local cache, so it is only looked up once."""
    import json

    from taxcite.index import db

    with db.transaction(connection):
        db.insert_cases(
            connection,
            [
                (
                    case.id,
                    case.reporter_cite,
                    case.case_name,
                    case.court,
                    case.date_filed,
                    case.absolute_url,
                    json.dumps(case.citations),
                    case.cite_count,
                    case.cluster_id,
                    case.opinion_id,
                    case.text,
                    case.excerpt,
                    case.citing_count,
                    case.last_cited,
                    case.last_citing,
                )
            ],
        )


def cached(connection: sqlite3.Connection, reporter_cite: str) -> CaseRecord | None:
    """Return a decision from the local cache, if it has been looked up before."""
    import json

    from taxcite.index import db

    row = db.get_case(connection, canonical_id(reporter_cite))
    if row is None:
        return None
    return CaseRecord(
        id=str(row["id"]),
        reporter_cite=str(row["reporter_cite"]),
        case_name=str(row["case_name"]),
        court=str(row["court"]),
        date_filed=row["date_filed"],
        url=str(row["url"]),
        citations=json.loads(row["citations"] or "[]"),
        cite_count=int(row["cite_count"] or 0),
        cluster_id=row["cluster_id"],
        opinion_id=row["opinion_id"],
        text=row["text"],
        excerpt=row["excerpt"],
        citing_count=row["citing_count"],
        last_cited=row["last_cited"],
        last_citing=row["last_citing"],
    )


def make_fetcher(client: HttpClient) -> Any:
    """Build a write-through lookup used by the resolver."""

    def fetch(connection: sqlite3.Connection, reporter_cite: str) -> CaseRecord | None:
        found = cached(connection, reporter_cite)
        if found is not None:
            return found
        try:
            record = lookup(client, reporter_cite)
        except SourceUnavailableError as exc:
            logger.info("CourtListener unavailable for %s: %s", reporter_cite, exc)
            raise
        if record is None:
            return None
        if record.cluster_id:
            try:
                history(client, record)
            except SourceUnavailableError as exc:
                logger.info("no citing history for %s: %s", reporter_cite, exc)
        if api_token() and record.opinion_id and not record.text:
            try:
                record.text = fetch_opinion_text(client, record.opinion_id)
            except SourceUnavailableError as exc:
                logger.info("could not fetch opinion %s: %s", record.opinion_id, exc)
        index_case(connection, record)
        return record

    return fetch


# --------------------------------------------------------------------------------------
# Citation history — emphatically not a citator
# --------------------------------------------------------------------------------------
#
# Whether a decision has been overruled, vacated or abrogated is a *treatment* signal,
# and no free source publishes one; Shepard's and KeyCite are proprietary editorial
# products. The obvious substitute — searching citing opinions for words like
# "overruled" — does not work: that query returns 119 opinions for Gregory v.
# Helvering, which has never been overruled, because the word appears somewhere in
# each of them. A tool that flagged a leading case as doubtful on that basis would be
# worse than one that said nothing.
#
# What *is* reliable is how heavily and how recently a decision is cited. A case cited
# a thousand times and again last month is alive; one last cited in 1954 deserves a
# look. That is context a reader can act on, and it is not a claim about treatment.


def history(client: HttpClient, record: CaseRecord) -> CaseRecord:
    """Fill in how often, and how recently, later decisions cite this one.

    Mutates and returns ``record``. Leaves the fields alone if the query fails, so a
    missing history is silence rather than a wrong number.
    """
    if not record.cluster_id:
        return record
    payload = client.get_json(
        COURTLISTENER_SEARCH_URL,
        params={
            "q": f"cites:({record.cluster_id})",
            "type": "o",
            "order_by": "dateFiled desc",
        },
    )
    count = _as_int(payload.get("count"))
    if count is None:
        return record
    record.citing_count = count
    results = payload.get("results") or []
    if results:
        record.last_cited = results[0].get("dateFiled")
        record.last_citing = str(results[0].get("caseName") or "") or None
    return record


def describe_history(record: CaseRecord) -> str | None:
    """Describe a decision's citation history in one clause, or ``None``."""
    if record.citing_count is None:
        return None
    if record.citing_count == 0:
        return "no later decision in CourtListener cites this one"
    plural = "s" if record.citing_count != 1 else ""
    described = f"cited in {record.citing_count:,} later decision{plural}"
    if record.last_cited:
        described += f", most recently {record.last_cited}"
    return described


# --------------------------------------------------------------------------------------
# Quotation checking without an API key
# --------------------------------------------------------------------------------------

#: Characters the search backend reads as query syntax.
_QUERY_UNSAFE: Final = re.compile(r'["\\/+\-!(){}\[\]^~*?:]')


def _phrase(text: str) -> str:
    """Render a passage as a safe quoted phrase for the search backend."""
    cleaned = _QUERY_UNSAFE.sub(" ", text)
    return _WS_RE.sub(" ", cleaned).strip()


@dataclass(slots=True)
class ProbeBudget:
    """How many search requests one run may spend checking case quotations.

    CourtListener throttles anonymous clients. Without a ceiling, a memo quoting
    twenty decisions would spend the quota partway through and then report outages for
    everything after that — turning one limit into a report full of noise. Spending the
    budget instead stops the requests cleanly, and the quotations that were not reached
    are reported as unverifiable.
    """

    remaining: int = CASE_PROBE_BUDGET

    def spend(self) -> None:
        """Account for one request.

        Raises:
            SourceUnavailableError: when the budget is gone.
        """
        if self.remaining <= 0:
            raise SourceUnavailableError(
                "the request budget for checking case quotations is spent",
                hint=(
                    "Set COURTLISTENER_TOKEN for a larger quota, or check the "
                    "remaining quotations in a second run."
                ),
            )
        self.remaining -= 1


def phrase_in_case(
    client: HttpClient,
    reporter_cite: str,
    phrase: str,
    budget: ProbeBudget | None = None,
) -> bool:
    """Return ``True`` if a phrase occurs in the decision at ``reporter_cite``.

    This is the keyless way to check a quotation. The search backend can answer
    "does this exact run of words appear in this specific opinion", which is a real
    verification even though the opinion text is never downloaded.

    It is a *presence* test, not a character-level comparison: case and punctuation
    are normalised away by the index, so a quotation that differs only in punctuation
    passes. Callers must not report it as a verbatim match.
    """
    words = _phrase(phrase)
    if len(words.split()) < CASE_PHRASE_MIN_WORDS:
        return False
    if budget is not None:
        budget.spend()
    payload = client.get_json(
        COURTLISTENER_SEARCH_URL,
        params={"q": f'citation:"{reporter_cite}" AND "{words}"', "type": "o"},
    )
    return bool(payload.get("count"))


def is_searchable(
    client: HttpClient, reporter_cite: str, budget: ProbeBudget | None = None
) -> bool:
    """Return ``True`` if the full text of this decision can be searched.

    A decision can be listed in the citation database without its text having been
    ingested. Asking first is what separates "this passage is not in the opinion" from
    "this opinion cannot be searched" — a distinction that decides whether a reader is
    told their quotation is wrong or merely told it is unchecked.
    """
    if budget is not None:
        budget.spend()
    payload = client.get_json(
        COURTLISTENER_SEARCH_URL,
        params={"q": f'citation:"{reporter_cite}"', "type": "o"},
    )
    return bool(payload.get("count"))


def longest_matching_prefix(
    client: HttpClient,
    reporter_cite: str,
    quote: str,
    budget: ProbeBudget | None = None,
) -> int | None:
    """Return how many words of a quotation actually appear in the decision.

    When a quotation fails, *where* it fails is the useful part: a passage that
    matches for twenty words and then diverges has been misquoted, while one that
    matches for none of them has been invented. Those need different corrections, and
    a binary search over the prefix separates them in a handful of requests.

    Returns ``None`` when the decision's text is not searchable at all, so that an
    opinion whose text was never ingested is not mistaken for a fabricated quotation.
    """
    words = _phrase(quote).split()
    if len(words) < CASE_PHRASE_MIN_WORDS:
        return None
    # Ask about the whole passage first. Most quotations a careful writer checks are
    # accurate, and that is the case one request settles; the binary search below is
    # for the ones that are not, and only they should pay for it.
    if phrase_in_case(client, reporter_cite, " ".join(words), budget):
        return len(words)
    low, high = CASE_PHRASE_MIN_WORDS, len(words) - 1
    if low > high or not phrase_in_case(client, reporter_cite, " ".join(words[:low]), budget):
        return 0 if is_searchable(client, reporter_cite, budget) else None
    best = low
    probes = 0
    while low < high and probes < CASE_DIVERGENCE_MAX_PROBES:
        probes += 1
        middle = (low + high + 1) // 2
        if phrase_in_case(client, reporter_cite, " ".join(words[:middle]), budget):
            best = low = middle
        else:
            high = middle - 1
    return best


# --------------------------------------------------------------------------------------
# Full opinion text, with an API token
# --------------------------------------------------------------------------------------

#: Star pagination, as the reporters print it: "*112" begins page 112.
STAR_PAGE_RE: Final = re.compile(r"\*(\d{1,5})\b")

_TAG_RE: Final = re.compile(r"<[^>]+>")


def api_token() -> str | None:
    """Return the configured CourtListener API token, if there is one."""
    token = os.environ.get(COURTLISTENER_TOKEN_ENV, "").strip()
    return token or None


def fetch_opinion_text(client: HttpClient, opinion_id: int) -> str | None:
    """Download one opinion's text. Requires a token.

    Returns ``None`` when no token is configured or the opinion carries no text.
    """
    token = api_token()
    if token is None:
        return None
    payload = client.get_json(
        f"{COURTLISTENER_OPINIONS_URL}{opinion_id}/",
        headers={"Authorization": f"Token {token}"},
    )
    # html_with_citations is the field CourtListener recommends; the plain-text
    # fields are missing for a good fraction of older opinions.
    for name in ("html_with_citations", "plain_text", "html", "xml_harvard", "html_lawbox"):
        value = payload.get(name)
        if value:
            return _strip_markup(str(value))
    return None


def _strip_markup(text: str) -> str:
    """Reduce opinion markup to text, keeping star pagination."""
    stripped = _TAG_RE.sub(" ", text)
    stripped = (
        stripped.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&#167;", "§")
    )
    return _WS_RE.sub(" ", stripped).strip()


def page_of(text: str, position: int) -> int | None:
    """Return the reporter page a character offset falls on.

    Star pagination marks where each printed page begins, so the page of any passage
    is the number on the last marker before it. This is what makes a pinpoint
    checkable.
    """
    page: int | None = None
    for match in STAR_PAGE_RE.finditer(text):
        if match.start() > position:
            break
        page = int(match.group(1))
    return page


def pages_in(text: str) -> list[int]:
    """Return every reporter page the opinion text is marked with."""
    return [int(m.group(1)) for m in STAR_PAGE_RE.finditer(text)]
