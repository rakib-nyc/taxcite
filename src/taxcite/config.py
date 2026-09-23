"""Configuration: data locations, tunable thresholds, and shared constants.

Everything configurable lives here rather than inline at the point of use, so that
behaviour can be tuned in one place. Paths honour the ``TAXCITE_DATA_DIR`` environment
variable and are resolved lazily, so tests can point them at a temporary directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from taxcite import __version__

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------

DATA_DIR_ENV: Final = "TAXCITE_DATA_DIR"
DEFAULT_DATA_DIR: Final = Path.home() / ".taxcite"


def data_dir() -> Path:
    """Return the TaxCite data directory, creating it if necessary."""
    raw = os.environ.get(DATA_DIR_ENV)
    path = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """Return the path of the SQLite index."""
    return data_dir() / "taxcite.db"


def raw_dir() -> Path:
    """Return the directory holding downloaded source archives."""
    path = data_dir() / "raw"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Return the on-disk HTTP cache directory."""
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------------------
# HTTP politeness
# --------------------------------------------------------------------------------------

REPO_URL: Final = "https://github.com/rakib-nyc/taxcite"
USER_AGENT: Final = f"taxcite/{__version__} (+{REPO_URL})"
MAX_REQUESTS_PER_SECOND: Final = 4.0
HTTP_TIMEOUT_SECONDS: Final = 60.0
HTTP_MAX_RETRIES: Final = 5

#: How many attempts a rate-limited (HTTP 429) request gets. Fewer than the general
#: retry count on purpose: a 429 means a quota, and hammering it spends the quota that
#: the rest of the run still needs.
HTTP_RATE_LIMIT_RETRIES: Final = 2

#: The longest a ``Retry-After`` header will be honoured before giving up. A server
#: asking for ten minutes is telling us to come back later, not to block the user.
HTTP_RETRY_AFTER_MAX_SECONDS: Final = 30.0
HTTP_BACKOFF_BASE_SECONDS: Final = 1.0
HTTP_BACKOFF_MAX_SECONDS: Final = 60.0
HTTP_CACHE_TTL_SECONDS: Final = 60 * 60 * 24 * 30  # 30 days

# --------------------------------------------------------------------------------------
# Source endpoints (verified against the live docs; see docs/architecture.md)
# --------------------------------------------------------------------------------------

USCODE_DOWNLOAD_PAGE: Final = "https://uscode.house.gov/download/download.shtml"
USCODE_PRIOR_RELEASE_POINTS: Final = "https://uscode.house.gov/download/priorreleasepoints.htm"
USCODE_BASE: Final = "https://uscode.house.gov"
USCODE_VIEW_URL: Final = (
    "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section{section}"
    "&num=0&edition=prelim"
)

ECFR_BASE: Final = "https://www.ecfr.gov"
ECFR_TITLES_URL: Final = "https://www.ecfr.gov/api/versioner/v1/titles.json"
ECFR_STRUCTURE_URL: Final = "https://www.ecfr.gov/api/versioner/v1/structure/{date}/title-26.json"
ECFR_FULL_URL: Final = "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-26.xml"
ECFR_VIEW_URL: Final = "https://www.ecfr.gov/current/title-26/section-{section}"

IRB_INDEX_URL: Final = "https://www.irs.gov/irb"
IRB_BULLETIN_URL: Final = "https://www.irs.gov/irb/{year}-{week}_IRB"

#: CourtListener's keyless search endpoint. The detail endpoints need an API key;
#: search does not, and it carries the metadata needed to check that a decision
#: exists and that the name attached to it is right.
COURTLISTENER_SEARCH_URL: Final = "https://www.courtlistener.com/api/rest/v4/search/"
COURTLISTENER_OPINIONS_URL: Final = "https://www.courtlistener.com/api/rest/v4/opinions/"
COURTLISTENER_MAX_RESULTS: Final = 20

#: An optional CourtListener API token, read from the environment. Without one
#: TaxCite verifies a case's existence and name and checks quotations by phrase
#: search; with one it downloads the opinion and compares text directly, which is
#: also what makes pinpoint pages checkable.
COURTLISTENER_TOKEN_ENV: Final = "COURTLISTENER_TOKEN"

#: The fewest words a phrase search will be trusted with. Below this a phrase is
#: common enough to appear in many opinions, and finding it proves nothing.
CASE_PHRASE_MIN_WORDS: Final = 5

#: How many probes the divergence search may make when a quotation does not match.
#: How many search requests one verification run may spend checking quotations from
#: decisions. CourtListener throttles anonymous clients, and a long memo full of case
#: quotations would otherwise exhaust the quota mid-run and start reporting outages.
#: When the budget runs out the remaining quotations are reported as unverifiable,
#: which is the honest answer, rather than as wrong.
CASE_PROBE_BUDGET: Final = 60

CASE_DIVERGENCE_MAX_PROBES: Final = 7

#: How close a cited case name must be to the reported one. Case names are written
#: many ways — "Commissioner", "Comm'r", "Estate of Smith v. Commissioner" — so the
#: comparison is deliberately forgiving, and a near miss is reported with its score
#: rather than being called a mismatch.
CASE_NAME_MATCH_THRESHOLD: Final = 0.75

# --------------------------------------------------------------------------------------
# Quote matching thresholds (SPEC 6.7)
# --------------------------------------------------------------------------------------

QUOTE_MIN_WORDS: Final = 4
QUOTE_EXACT_THRESHOLD: Final = 0.97
QUOTE_CLOSE_THRESHOLD: Final = 0.88
QUOTE_MISATTRIBUTION_THRESHOLD: Final = 0.95
QUOTE_GLOBAL_SEARCH_CANDIDATES: Final = 25
QUOTE_GLOBAL_SEARCH_TOKENS: Final = 4
QUOTE_NEAR_DISTANCE: Final = 12
LONG_SECTION_CHARS: Final = 50_000
LONG_SECTION_WINDOW_CHARS: Final = 2_000
BRACKET_WILDCARD_MAX_WORDS: Final = 8

# --------------------------------------------------------------------------------------
# Resolution / suggestions (SPEC 6.6)
# --------------------------------------------------------------------------------------

SUGGESTION_MAX_EDIT_DISTANCE: Final = 2
SUGGESTION_NUMERIC_WINDOW: Final = 5
SUGGESTION_LIMIT: Final = 3

#: How many sibling subdivisions to name before abbreviating. A contiguous run is
#: always collapsed to "(a)-(s)" first, so this only bites on gappy lists.
SUGGESTION_MAX_CHILDREN: Final = 12

# --------------------------------------------------------------------------------------
# Index build
# --------------------------------------------------------------------------------------

BUILD_BATCH_SECTIONS: Final = 500
DEFAULT_MAX_CHARS: Final = 12_000
DEFAULT_SEARCH_LIMIT: Final = 10
DEFINITION_TEXT_MAX_CHARS: Final = 1_000

# --------------------------------------------------------------------------------------
# Search ranking
# --------------------------------------------------------------------------------------
#
# BM25 alone ranks statute poorly: its length normalisation buries the long operative
# provision under short provisions elsewhere that merely borrow its language. Ranking
# therefore fuses four rankings of the BM25 candidate set with reciprocal rank fusion:
# relevance, how heavily the owning section is cross-referenced, how shallow the
# provision sits in its section, and how substantial its text is. docs/architecture.md
# records the measurements and the query sets these weights were tuned and validated
# on.

#: How many BM25 candidates to re-rank.
SEARCH_RERANK_CANDIDATES: Final = 200

#: RRF smoothing constant. Larger values flatten the effect of rank position.
SEARCH_RRF_K: Final = 10

#: Weight of the authority ranking (distinct provisions citing the owning section).
SEARCH_AUTHORITY_WEIGHT: Final = 1.0

#: Weight of the depth ranking (a subsection outranks a clause five levels down).
SEARCH_DEPTH_WEIGHT: Final = 1.5

#: Weight of the length ranking, which counteracts BM25's bias towards a one-line
#: provision that borrows a phrase over the substantive provision that establishes it.
SEARCH_LENGTH_WEIGHT: Final = 1.0

# --------------------------------------------------------------------------------------
# Citation parsing
# --------------------------------------------------------------------------------------

MAX_RANGE_EXPANSION: Final = 26
FALSE_POSITIVE_CONTEXT_CHARS: Final = 30
