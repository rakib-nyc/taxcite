"""Quotation verification (SPEC 6.7).

Finding a quotation is easy; deciding what a mismatch *means* is the job. A quoted
passage can be verbatim, lightly altered, correct but attributed to the wrong
subdivision, correct but attributed to an entirely different section, or simply
invented. Those five outcomes call for very different responses from a reader, so each
gets its own status.

The pipeline is: pull quotations out of the document, attach each to the citation it
belongs to, normalise both sides, and then try progressively wider searches — the cited
provision, then the whole section, then the entire corpus.
"""

from __future__ import annotations

import difflib
import itertools
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rapidfuzz import fuzz

from taxcite.citations import normalize as cite_norm
from taxcite.citations.parser import mask_text
from taxcite.config import (
    BRACKET_WILDCARD_MAX_WORDS,
    CASE_PHRASE_MIN_WORDS,
    LONG_SECTION_CHARS,
    LONG_SECTION_WINDOW_CHARS,
    QUOTE_CLOSE_THRESHOLD,
    QUOTE_EXACT_THRESHOLD,
    QUOTE_GLOBAL_SEARCH_CANDIDATES,
    QUOTE_GLOBAL_SEARCH_TOKENS,
    QUOTE_MIN_WORDS,
    QUOTE_MISATTRIBUTION_THRESHOLD,
    QUOTE_NEAR_DISTANCE,
)
from taxcite.index import db
from taxcite.index import search as fts
from taxcite.models import (
    Citation,
    CitationResult,
    Provision,
    QuoteCheck,
    SourceType,
    Status,
)
from taxcite.verify import textnorm
from taxcite.verify.textnorm import Normalized

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from taxcite.sources.courtlistener import CaseRecord

logger: Final = logging.getLogger("taxcite.quotes")

#: Given a reporter citation and a passage, returns how many leading words of the
#: passage occur in that decision, or ``None`` if the decision could not be searched.
#: This is how a quotation from a case is checked when the opinion text is not held
#: locally, which is the ordinary situation.
CaseProber = Callable[[str, str], int | None]

# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------

#: A quotation may wrap across lines but never across a blank line: a paragraph break
#: means a closing mark is missing, and the "quotation" would swallow the document.
_INNER: Final = r"(?:[^{marks}\n]|\n(?![ \t]*\n))"
_CURLY_RE: Final = re.compile(
    "\u201c(?P<body>" + _INNER.format(marks="\u201c\u201d") + "{8,}?)\u201d"
)
_STRAIGHT_RE: Final = re.compile('"(?P<body>' + _INNER.format(marks='"') + '{8,}?)"')
_BLOCKQUOTE_RE: Final = re.compile(r"(?:^[ \t]*>[ \t]?.*(?:\n|$))+", re.MULTILINE)
_BLOCKQUOTE_MARKER_RE: Final = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)

#: Editorial parentheticals that sit at the end of a quotation without being part of it.
_TRAILING_PARENTHETICAL_RE: Final = re.compile(
    r"\s*\((?:emphasis\s+(?:added|supplied|in\s+the\s+original)"
    r"|citations?\s+omitted|internal\s+(?:quotation\s+marks|citations?)\s+omitted"
    r"|footnotes?\s+omitted|alterations?\s+in\s+the\s+original)\)\s*[.,;]?\s*$",
    re.IGNORECASE,
)

_ELLIPSIS_RE: Final = re.compile(r"\s*(?:\.\s?\.\s?\.|…)\s*")
_BRACKET_RE: Final = re.compile(r"\[([^\][]{1,60})\]")


@dataclass(frozen=True, slots=True)
class ExtractedQuote:
    """A quoted passage located in a document."""

    text: str
    span: tuple[int, int]
    kind: str


def extract_quotes(text: str) -> list[ExtractedQuote]:
    """Find every quotation in ``text`` that is long enough to be checkable.

    Quotations inside code spans and fenced blocks are ignored, and anything shorter
    than :data:`~taxcite.config.QUOTE_MIN_WORDS` words is dropped as too ambiguous to
    attribute.
    """
    masked = mask_text(text)
    found: list[ExtractedQuote] = []
    taken: list[tuple[int, int]] = []

    for match in _BLOCKQUOTE_RE.finditer(masked):
        body = _BLOCKQUOTE_MARKER_RE.sub("", text[match.start() : match.end()]).strip()
        if body:
            found.append(ExtractedQuote(body, (match.start(), match.end()), "blockquote"))
            taken.append((match.start(), match.end()))

    for pattern, kind in ((_CURLY_RE, "curly"), (_STRAIGHT_RE, "straight")):
        for match in pattern.finditer(masked):
            start, end = match.span("body")
            if any(low <= start < high for low, high in taken):
                continue
            found.append(ExtractedQuote(text[start:end].strip(), (start, end), kind))
            taken.append(match.span())

    checkable: list[ExtractedQuote] = []
    for quote in sorted(found, key=lambda q: q.span):
        if textnorm.word_count(strip_editorial(quote.text)) < QUOTE_MIN_WORDS:
            logger.debug("quotation too short to check: %r", quote.text)
            continue
        checkable.append(quote)
    return checkable


def strip_editorial(text: str) -> str:
    """Remove a trailing "(emphasis added)"-style parenthetical from a quotation."""
    return _TRAILING_PARENTHETICAL_RE.sub("", text).strip()


# --------------------------------------------------------------------------------------
# Sentences and association
# --------------------------------------------------------------------------------------

#: Abbreviations that end in a period without ending a sentence (SPEC 6.7).
ABBREVIATIONS: Final[frozenset[str]] = frozenset(
    {
        "u.s.c.",
        "u.s.c.a.",
        "c.f.r.",
        "cfr.",
        "u.s.",
        "i.r.c.",
        "irc.",
        "treas.",
        "reg.",
        "regs.",
        "rev.",
        "rul.",
        "proc.",
        "temp.",
        "prop.",
        "no.",
        "nos.",
        "v.",
        "vs.",
        "inc.",
        "co.",
        "corp.",
        "ltd.",
        "llc.",
        "e.g.",
        "i.e.",
        "cf.",
        "id.",
        "ibid.",
        "etc.",
        "al.",
        "et.",
        "sec.",
        "secs.",
        "subsec.",
        "para.",
        "paras.",
        "art.",
        "ch.",
        "tit.",
        "pub.",
        "l.",
        "stat.",
        "ann.",
        "t.d.",
        "p.",
        "pp.",
        "fed.",
        "supp.",
        "ct.",
        "cir.",
        "dist.",
        "mr.",
        "ms.",
        "mrs.",
        "dr.",
        "jr.",
        "sr.",
        "st.",
        "t.c.",
        "b.t.a.",
        "s.ct.",
        "f.2d",
        "f.3d",
        "f.4th",
        "memo.",
    }
)

_SENTENCE_END_RE: Final = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s|$)")
_PARAGRAPH_BREAK_RE: Final = re.compile(r"\n[ \t]*\n")
_TRAILING_TOKEN_RE: Final = re.compile(r"[\w.§'-]+$")


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Split ``text`` into sentence spans.

    A blank line always ends a sentence. A period does not, if the token it closes is a
    known legal abbreviation, a single initial, or a bare number, or if the next word
    starts in lower case.
    """
    boundaries: set[int] = {0, len(text)}
    for match in _PARAGRAPH_BREAK_RE.finditer(text):
        boundaries.add(match.start())
        boundaries.add(match.end())
    for match in _SENTENCE_END_RE.finditer(text):
        if _is_abbreviation(text, match.start()) or _next_word_is_lowercase(text, match.end()):
            continue
        boundaries.add(match.end())

    ordered = sorted(boundaries)
    spans = [(start, end) for start, end in itertools.pairwise(ordered) if text[start:end].strip()]
    return spans or [(0, len(text))]


def _is_abbreviation(text: str, position: int) -> bool:
    """Return ``True`` if the period at ``position`` closes an abbreviation."""
    token_match = _TRAILING_TOKEN_RE.search(text[:position])
    if token_match is None:
        return False
    token = f"{token_match.group(0)}.".lower()
    if token in ABBREVIATIONS:
        return True
    stem = token_match.group(0)
    if len(stem) == 1 and stem.isalpha():
        return True
    return bool(stem.isdigit())


def _next_word_is_lowercase(text: str, position: int) -> bool:
    """Return ``True`` if the next word after ``position`` starts in lower case."""
    rest = text[position : position + 80].lstrip()
    for char in rest:
        if char.isalpha():
            return char.islower()
        if char.isdigit() or char in "“\"'([":
            return False
    return False


def associate(
    quotes: list[ExtractedQuote],
    citations: list[Citation],
    sentences: list[tuple[int, int]],
    text: str = "",
) -> tuple[dict[int, list[ExtractedQuote]], list[ExtractedQuote]]:
    """Attach each quotation to the citation it belongs to (SPEC 6.7).

    Args:
        quotes: The quotations found in the document.
        citations: The citations found in the document.
        sentences: Sentence spans over the document.
        text: The document itself, needed to recognise a citation sentence.

    Returns:
        A mapping from citation index to its quotations, and the orphans.
    """
    attached: dict[int, list[ExtractedQuote]] = {}
    orphans: list[ExtractedQuote] = []
    for quote in quotes:
        index = _citation_for(quote, citations, sentences, text)
        if index is None:
            orphans.append(quote)
        else:
            attached.setdefault(index, []).append(quote)
    return attached, orphans


def _sentence_of(offset: int, sentences: list[tuple[int, int]]) -> int:
    """Return the index of the sentence containing ``offset``."""
    for index, (start, end) in enumerate(sentences):
        if start <= offset < end:
            return index
    return len(sentences) - 1


#: What may remain of a sentence once its citations are struck out and it is still a
#: citation sentence: punctuation, and the introductory signals that precede support.
_CITATION_RESIDUE_RE: Final = re.compile(
    r"\b(?:see|also|accord|but|cf|compare|with|contra|e\.?\s?g|i\.?\s?e|generally|"
    r"id|ibid|quoting|citing|quoted|in|aff'd|rev'd|cert|denied|per|curiam|supra|"
    r"slip|op|at|and|the|of)\b|[^\w]+",
    re.IGNORECASE,
)


def _is_citation_sentence(text: str, span: tuple[int, int], citations: list[Citation]) -> bool:
    """Return ``True`` if a sentence is nothing but citations.

    Legal writing puts the support for a quoted proposition in a sentence of its own::

        The Court explained that "the standard set up by the statute is not a rule of
        law." Welch v. Helvering, 290 U.S. 111, 115 (1933).

    That citation sentence belongs to the quotation in front of it. Without this rule
    the quotation is attributed to whatever citation happened to appear earlier in the
    paragraph — usually a statute, which does not contain the passage, so an accurate
    quotation is reported as missing.
    """
    start, end = span
    inside = [c for c in citations if start <= c.span[0] < end]
    if not inside:
        return False
    chars = list(text[start:end])
    for citation in inside:
        low = max(start, citation.span[0]) - start
        high = min(end, citation.span[1]) - start
        for position in range(low, high):
            chars[position] = " "
    return not _CITATION_RESIDUE_RE.sub("", "".join(chars)).strip()


def _citation_for(
    quote: ExtractedQuote,
    citations: list[Citation],
    sentences: list[tuple[int, int]],
    text: str = "",
) -> int | None:
    """Find the citation a quotation belongs to, or ``None`` for an orphan."""
    if not citations:
        return None
    quote_start, quote_end = quote.span
    sentence = _sentence_of(quote_start, sentences)
    start, end = sentences[sentence]

    outside = [
        index
        for index, citation in enumerate(citations)
        if not (citation.span[0] >= quote_start and citation.span[1] <= quote_end)
    ]

    in_sentence = [i for i in outside if start <= citations[i].span[0] < end]
    preceding = [i for i in in_sentence if citations[i].span[1] <= quote_start]
    if preceding:
        return max(preceding, key=lambda i: citations[i].span[1])
    following = [i for i in in_sentence if citations[i].span[0] >= quote_end]
    if following:
        return min(following, key=lambda i: citations[i].span[0])

    # The next sentence, when it is nothing but citations: that is the support for the
    # quotation just made, and it outranks anything earlier in the paragraph.
    if text and sentence + 1 < len(sentences):
        low, high = sentences[sentence + 1]
        if quote_end <= low and _is_citation_sentence(text, (low, high), citations):
            support = [i for i in outside if low <= citations[i].span[0] < high]
            if support:
                return min(support, key=lambda i: citations[i].span[0])

    # One sentence back, and no further: "Section 162(a) provides:" followed by a block
    # quotation is an attribution, but a citation two paragraphs up is not.
    if sentence > 0:
        low, high = sentences[sentence - 1]
        earlier = [i for i in outside if low <= citations[i].span[0] < high]
        if earlier:
            return max(earlier, key=lambda i: citations[i].span[1])
    return None


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    """One ellipsis-delimited piece of a quotation, compiled for searching."""

    literal: str
    pattern: re.Pattern[str]
    has_wildcard: bool


@dataclass(frozen=True, slots=True)
class PreparedQuote:
    """A quotation reduced to the pieces that have to be found in the source."""

    original: str
    segments: tuple[Segment, ...]

    @property
    def literal(self) -> str:
        """Return the quotation's literal text with ellipsis gaps closed up."""
        return " ".join(segment.literal for segment in self.segments if segment.literal)


def prepare(quote: str) -> PreparedQuote:
    """Turn a quotation into ordered segments, handling ellipses and alterations.

    An ellipsis splits the quotation: each piece has to appear, in order. A
    single-letter bracket (``[t]he``) is an alteration of case and matches the letter
    itself; a longer bracket stands in for one to eight words of the original.
    """
    body = strip_editorial(quote)
    segments: list[Segment] = []
    for piece in _ELLIPSIS_RE.split(body):
        normalized = textnorm.normalize_mapped(piece).text
        if not normalized:
            continue
        segments.append(_compile_segment(piece, normalized))
    if not segments:
        segments.append(_compile_segment(body, textnorm.normalize(body)))
    return PreparedQuote(original=quote, segments=tuple(segments))


def _compile_segment(raw: str, normalized: str) -> Segment:
    """Compile one segment into a regex, expanding bracketed alterations."""
    pattern_parts: list[str] = []
    literal_parts: list[str] = []
    position = 0
    has_wildcard = False
    for match in _BRACKET_RE.finditer(normalized):
        pattern_parts.append(re.escape(normalized[position : match.start()]))
        literal_parts.append(normalized[position : match.start()])
        inner = match.group(1)
        if len(inner) == 1:
            pattern_parts.append(re.escape(inner))
            literal_parts.append(inner)
        else:
            pattern_parts.append(rf"\S+(?:\s+\S+){{0,{BRACKET_WILDCARD_MAX_WORDS - 1}}}")
            literal_parts.append(inner)
            has_wildcard = True
        position = match.end()
    pattern_parts.append(re.escape(normalized[position:]))
    literal_parts.append(normalized[position:])
    del raw
    return Segment(
        literal="".join(literal_parts),
        pattern=re.compile("".join(pattern_parts)),
        has_wildcard=has_wildcard,
    )


@dataclass(frozen=True, slots=True)
class Match:
    """Where a quotation was found and how well it matched."""

    score: float
    start: int
    end: int

    @property
    def exact(self) -> bool:
        """Return ``True`` for a verbatim match."""
        return self.score >= 1.0


def find_exact(prepared: PreparedQuote, haystack: str) -> Match | None:
    """Find every segment, in order and without overlapping, or return ``None``."""
    position = 0
    first: int | None = None
    last = 0
    for segment in prepared.segments:
        match = segment.pattern.search(haystack, position)
        if match is None:
            return None
        if first is None:
            first = match.start()
        last = match.end()
        position = match.end()
    if first is None:  # pragma: no cover - segments is never empty
        return None
    return Match(score=1.0, start=first, end=last)


def find_fuzzy(prepared: PreparedQuote, haystack: str) -> Match | None:
    """Score the best in-order approximate placement of every segment."""
    if not haystack:
        return None
    position = 0
    total_weight = 0.0
    total_score = 0.0
    first: int | None = None
    last = 0
    for segment in prepared.segments:
        needle = segment.literal
        if not needle:
            continue
        window_start, window_end = _window(needle, haystack, position)
        window = haystack[window_start:window_end]
        if not window:
            return None
        alignment = fuzz.partial_ratio_alignment(needle, window, score_cutoff=1)
        if alignment is None:
            return None
        weight = float(len(needle))
        total_weight += weight
        total_score += weight * alignment.score / 100.0
        start = window_start + alignment.dest_start
        end = window_start + alignment.dest_end
        if first is None:
            first = start
        last = max(last, end)
        position = end
    if first is None or total_weight == 0:
        return None
    return Match(score=total_score / total_weight, start=first, end=last)


def _window(needle: str, haystack: str, position: int) -> tuple[int, int]:
    """Narrow a long haystack to a window around the best anchor (SPEC 6.7)."""
    if len(haystack) - position <= LONG_SECTION_CHARS:
        return position, len(haystack)
    anchor = _anchor_position(needle, haystack, position)
    if anchor is None:
        return position, len(haystack)
    low = max(position, anchor - LONG_SECTION_WINDOW_CHARS)
    high = min(len(haystack), anchor + len(needle) + LONG_SECTION_WINDOW_CHARS)
    return low, high


def _anchor_position(needle: str, haystack: str, position: int) -> int | None:
    """Find where a distinctive three-word run of the needle occurs in the haystack."""
    tokens = needle.split()
    for index in range(len(tokens) - 2):
        trigram = " ".join(tokens[index : index + 3])
        found = haystack.find(trigram, position)
        if found != -1:
            return max(position, found - index * 8)
    return None


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Candidate:
    """A provision considered as the source of a quotation."""

    provision: Provision
    haystack: Normalized
    match: Match


class QuoteVerifier:
    """Checks quotations against the indexed text of the Code and the regulations."""

    def __init__(
        self, connection: sqlite3.Connection, *, case_prober: CaseProber | None = None
    ) -> None:
        """Create a verifier reading from ``connection``.

        Args:
            connection: An open index connection.
            case_prober: Checks how much of a passage occurs in a decision. Needed only
                for quotations from cases whose opinion text is not held locally;
                without it such quotations are reported as unverifiable, never as wrong.
        """
        self.connection = connection
        self.case_prober = case_prober
        self._normalized: dict[str, Normalized] = {}

    # -- public ------------------------------------------------------------------

    def check(self, quote: ExtractedQuote, result: CitationResult | None) -> QuoteCheck:
        """Check one quotation, against whatever kind of thing it was attributed to."""
        prepared = prepare(quote.text)

        guidance = self._cited_guidance(result)
        if guidance is not None:
            return self._check_guidance(quote, prepared, guidance)

        unreadable_case = False
        if result is not None and result.citation.source is SourceType.CASE:
            decided = self._check_case(quote, prepared, result)
            if decided is not None:
                return decided
            # Nothing is known about the decision. The corpus search below still runs,
            # because the passage may in fact come from the Code; but if it finds
            # nothing, the honest answer is that the quotation was not checked.
            unreadable_case = True

        provision = self._cited_provision(result)

        if provision is not None:
            direct = self._score(prepared, provision)
            if direct is not None and direct.match.score >= QUOTE_CLOSE_THRESHOLD:
                return self._from_candidate(quote, prepared, direct, cited=True)
            elsewhere = self._elsewhere_in_section(prepared, provision)
            if elsewhere is not None:
                return self._wrong_pinpoint(quote, prepared, elsewhere)

        found = self._global(prepared)
        if found is not None:
            if provision is None:
                return self._orphan(quote, prepared, found)
            return self._misattributed(quote, prepared, found)
        if unreadable_case:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.UNVERIFIABLE,
                score=0.0,
                diff=(
                    "this passage is not in the Code or the regulations, and the "
                    "decision it is attributed to could not be read"
                ),
            )
        return QuoteCheck(
            quote=quote.text,
            span=quote.span,
            status=Status.QUOTE_NOT_FOUND,
            score=0.0,
        )

    # -- candidate scoring -------------------------------------------------------

    def _check_case(
        self, quote: ExtractedQuote, prepared: PreparedQuote, result: CitationResult
    ) -> QuoteCheck | None:
        """Check a quotation against a court decision.

        Two routes, depending on what is available. With the opinion text held locally
        the ordinary matcher runs, and the answer is a real character-level comparison
        that can also check the pinpoint page. Without it — the usual situation, because
        the keyless source serves metadata only — the question put to the source is
        "does this run of words occur in this decision", and a binary search over the
        opening words finds where the quotation stops matching. That distinguishes a
        misquotation from an invention, which is what a reader actually needs to know.

        Returns ``None`` when neither route is open, so the caller falls through to the
        corpus search and, failing that, to reporting the quotation as not found.
        """
        from taxcite.sources import courtlistener

        reporter = result.citation.reporter_cite
        if reporter is None or result.status in _UNRESOLVED:
            return None
        record = courtlistener.cached(self.connection, reporter)
        if record is None:
            return None
        if record.text:
            return self._case_from_text(quote, prepared, record, result.citation.pin_page)
        if self.case_prober is None:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.UNVERIFIABLE,
                score=0.0,
                matched_provision_id=record.id,
                diff=(
                    "the text of this decision is not available, so the quotation "
                    "could not be checked"
                ),
            )
        return self._case_by_probe(quote, prepared, record)

    def _case_from_text(
        self,
        quote: ExtractedQuote,
        prepared: PreparedQuote,
        record: CaseRecord,
        pin_page: int | None,
    ) -> QuoteCheck:
        """Match a quotation against a locally held opinion, and check its pinpoint."""
        from taxcite.sources.courtlistener import page_of

        text = record.text or ""
        cached = self._normalized.get(record.id)
        if cached is None:
            cached = textnorm.normalize_mapped(text)
            self._normalized[record.id] = cached
        match = find_exact(prepared, cached.text) or find_fuzzy(prepared, cached.text)
        if match is None or match.score < QUOTE_CLOSE_THRESHOLD:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.QUOTE_NOT_FOUND,
                score=round(match.score, 4) if match else 0.0,
                matched_provision_id=record.id,
                diff="this passage does not appear in the decision",
            )
        matched = cached.source_slice(match.start, match.end)
        source_start, _ = cached.source_span(match.start, match.end)
        page = page_of(text, source_start)
        if match.score < QUOTE_EXACT_THRESHOLD:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.QUOTE_CLOSE,
                score=round(match.score, 4),
                matched_text=matched,
                matched_provision_id=record.id,
                diff=word_diff(prepared.literal, matched),
            )
        if pin_page is not None and page is not None and page != pin_page:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.QUOTE_WRONG_PINPOINT,
                score=round(match.score, 4),
                matched_text=matched,
                matched_provision_id=record.id,
                diff=(
                    f"the quotation is accurate, but it appears at page {page}, not page {pin_page}"
                ),
            )
        return QuoteCheck(
            quote=quote.text,
            span=quote.span,
            status=Status.QUOTE_EXACT,
            score=round(match.score, 4),
            matched_text=matched,
            matched_provision_id=record.id,
            diff=None if page is None else f"the quoted language appears at page {page}",
        )

    def _case_by_probe(
        self, quote: ExtractedQuote, prepared: PreparedQuote, record: CaseRecord
    ) -> QuoteCheck:
        """Check a quotation by asking how many of its words occur in the decision."""
        assert self.case_prober is not None  # guarded by the caller
        literal = prepared.literal
        total = len(literal.split())
        matched_words = self.case_prober(record.reporter_cite, literal)
        if matched_words is None:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.UNVERIFIABLE,
                score=0.0,
                matched_provision_id=record.id,
                diff="the decision could not be searched, so this quotation is unchecked",
            )
        if total and matched_words >= total:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.QUOTE_EXACT,
                score=1.0,
                matched_provision_id=record.id,
                diff=(
                    "every word of this passage occurs in the decision, in order; "
                    "punctuation and capitalisation were not compared, because the "
                    "opinion text is not held locally"
                ),
            )
        if matched_words >= CASE_PHRASE_MIN_WORDS:
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=Status.QUOTE_CLOSE,
                score=round(matched_words / total, 4) if total else 0.0,
                matched_provision_id=record.id,
                diff=(
                    f"the first {matched_words} words of this passage occur in the "
                    f"decision; it stops matching at \u201c{_word_at(literal, matched_words)}"
                    f"\u201d"
                ),
            )
        return QuoteCheck(
            quote=quote.text,
            span=quote.span,
            status=Status.QUOTE_NOT_FOUND,
            score=0.0,
            matched_provision_id=record.id,
            diff="no run of words from this passage occurs in the decision",
        )

    def _cited_guidance(self, result: CitationResult | None) -> sqlite3.Row | None:
        """Return the indexed guidance document a citation resolved to, if any."""
        if result is None or result.citation.source is not SourceType.IRS_GUIDANCE:
            return None
        kind = result.citation.guidance_type
        number = result.citation.guidance_number
        if kind is None or number is None:
            return None
        from taxcite.sources.irb import KIND_SLUGS
        from taxcite.sources.irb import canonical_id as guidance_id

        if kind not in KIND_SLUGS:
            return None
        return db.get_guidance(self.connection, guidance_id(kind, number))

    def _check_guidance(
        self, quote: ExtractedQuote, prepared: PreparedQuote, row: sqlite3.Row
    ) -> QuoteCheck:
        """Check a quotation against the text of a published ruling or notice.

        Guidance has no subdivision hierarchy worth speaking of, so there is no
        wrong-pinpoint case: the quotation is either in the document or it is not, and
        if it is not, the corpus-wide search still runs to find where it came from.
        """
        identifier = str(row["id"])
        cached = self._normalized.get(identifier)
        if cached is None:
            cached = textnorm.normalize_mapped(str(row["text"]))
            self._normalized[identifier] = cached
        match = find_exact(prepared, cached.text) or find_fuzzy(prepared, cached.text)
        if match is not None and match.score >= QUOTE_CLOSE_THRESHOLD:
            matched = cached.source_slice(match.start, match.end)
            status = (
                Status.QUOTE_EXACT if match.score >= QUOTE_EXACT_THRESHOLD else Status.QUOTE_CLOSE
            )
            return QuoteCheck(
                quote=quote.text,
                span=quote.span,
                status=status,
                score=round(match.score, 4),
                matched_text=matched,
                matched_provision_id=identifier,
                diff=(
                    None if status is Status.QUOTE_EXACT else word_diff(prepared.literal, matched)
                ),
            )
        found = self._global(prepared)
        if found is not None:
            return self._misattributed(quote, prepared, found)
        return QuoteCheck(
            quote=quote.text, span=quote.span, status=Status.QUOTE_NOT_FOUND, score=0.0
        )

    def _cited_provision(self, result: CitationResult | None) -> Provision | None:
        """Return the provision a citation resolved to, if it resolved at all."""
        if result is None or result.citation.canonical_id is None:
            return None
        if result.status in {Status.NOT_FOUND, Status.SOURCE_UNAVAILABLE, Status.MALFORMED}:
            return None
        return db.get_provision(self.connection, result.citation.canonical_id)

    def _haystack(self, provision: Provision) -> Normalized:
        """Return, and cache, the normalised full text of a provision."""
        cached = self._normalized.get(provision.id)
        if cached is None:
            cached = textnorm.normalize_mapped(provision.full_text)
            self._normalized[provision.id] = cached
        return cached

    def _score(self, prepared: PreparedQuote, provision: Provision) -> _Candidate | None:
        """Score a quotation against one provision, exact first then fuzzy."""
        haystack = self._haystack(provision)
        match = find_exact(prepared, haystack.text) or find_fuzzy(prepared, haystack.text)
        if match is None:
            return None
        return _Candidate(provision=provision, haystack=haystack, match=match)

    def _elsewhere_in_section(
        self, prepared: PreparedQuote, provision: Provision
    ) -> _Candidate | None:
        """Look for the quotation elsewhere in the section that was cited."""
        section_id = cite_norm.canonical_id(provision.source, provision.section)
        if section_id == provision.id and not provision.path:
            candidates = db.get_descendants(self.connection, provision.id)
        else:
            section = db.get_provision(self.connection, section_id)
            if section is None:  # pragma: no cover - the section must exist
                return None
            candidates = [section, *db.get_descendants(self.connection, section_id)]
        best: _Candidate | None = None
        for candidate_provision in candidates:
            if candidate_provision.id == provision.id:
                continue
            scored = self._score(prepared, candidate_provision)
            if scored is None or scored.match.score < QUOTE_MISATTRIBUTION_THRESHOLD:
                continue
            if best is None or _more_specific(scored, best):
                best = scored
        return best

    def _global(self, prepared: PreparedQuote) -> _Candidate | None:
        """Search the whole corpus for the quotation (SPEC 6.7 step 5)."""
        tokens = fts.rare_tokens(self.connection, prepared.literal, QUOTE_GLOBAL_SEARCH_TOKENS)
        if not tokens:
            return None
        for expression in _global_queries(tokens):
            rows = self.connection.execute(
                "SELECT p.id FROM provisions_fts "
                "JOIN provisions p ON p.rowid = provisions_fts.rowid "
                "WHERE provisions_fts MATCH ? ORDER BY bm25(provisions_fts) LIMIT ?",
                (expression, QUOTE_GLOBAL_SEARCH_CANDIDATES),
            ).fetchall()
            best: _Candidate | None = None
            for row in rows:
                provision = db.get_provision(self.connection, str(row["id"]))
                if provision is None:  # pragma: no cover - the row just came from there
                    continue
                scored = self._score(prepared, provision)
                if scored is None or scored.match.score < QUOTE_MISATTRIBUTION_THRESHOLD:
                    continue
                if best is None or _more_specific(scored, best):
                    best = scored
            if best is not None:
                return best
        return None

    # -- result construction -----------------------------------------------------

    def _from_candidate(
        self,
        quote: ExtractedQuote,
        prepared: PreparedQuote,
        candidate: _Candidate,
        *,
        cited: bool,
    ) -> QuoteCheck:
        """Build the result for a quotation found in the provision it was attributed to."""
        del cited
        score = candidate.match.score
        matched = candidate.haystack.source_slice(candidate.match.start, candidate.match.end)
        status = Status.QUOTE_EXACT if score >= QUOTE_EXACT_THRESHOLD else Status.QUOTE_CLOSE
        return QuoteCheck(
            quote=quote.text,
            span=quote.span,
            status=status,
            score=round(score, 4),
            matched_text=matched,
            matched_provision_id=candidate.provision.id,
            diff=None if status is Status.QUOTE_EXACT else word_diff(prepared.literal, matched),
        )

    def _wrong_pinpoint(
        self, quote: ExtractedQuote, prepared: PreparedQuote, candidate: _Candidate
    ) -> QuoteCheck:
        """Build the result for a quotation found in another part of the cited section."""
        return QuoteCheck(
            quote=quote.text,
            span=quote.span,
            status=Status.QUOTE_WRONG_PINPOINT,
            score=round(candidate.match.score, 4),
            matched_text=candidate.haystack.source_slice(
                candidate.match.start, candidate.match.end
            ),
            matched_provision_id=candidate.provision.id,
            diff=_relocation_note(candidate.provision),
        )

    def _misattributed(
        self, quote: ExtractedQuote, prepared: PreparedQuote, candidate: _Candidate
    ) -> QuoteCheck:
        """Build the result for a quotation that belongs to a different section."""
        del prepared
        return QuoteCheck(
            quote=quote.text,
            span=quote.span,
            status=Status.QUOTE_MISATTRIBUTED,
            score=round(candidate.match.score, 4),
            matched_text=candidate.haystack.source_slice(
                candidate.match.start, candidate.match.end
            ),
            matched_provision_id=candidate.provision.id,
            diff=_relocation_note(candidate.provision),
        )

    def _orphan(
        self, quote: ExtractedQuote, prepared: PreparedQuote, candidate: _Candidate
    ) -> QuoteCheck:
        """Build the result for a quotation with no citation attached to it."""
        del prepared
        return QuoteCheck(
            quote=quote.text,
            span=quote.span,
            status=Status.QUOTE_MISATTRIBUTED,
            score=round(candidate.match.score, 4),
            matched_text=candidate.haystack.source_slice(
                candidate.match.start, candidate.match.end
            ),
            matched_provision_id=candidate.provision.id,
            diff=_relocation_note(candidate.provision),
        )


#: Statuses meaning the citation itself did not resolve, so there is nothing to check
#: a quotation against.
_UNRESOLVED: Final[frozenset[Status]] = frozenset(
    {
        Status.NOT_FOUND,
        Status.PINPOINT_NOT_FOUND,
        Status.SOURCE_UNAVAILABLE,
        Status.MALFORMED,
        Status.UNVERIFIABLE,
    }
)


def _word_at(text: str, index: int) -> str:
    """Return the words a passage stops matching at, with a little context."""
    words = text.split()
    if index >= len(words):  # pragma: no cover - the caller checked
        return ""
    return " ".join(words[index : index + 4])


def _more_specific(candidate: _Candidate, incumbent: _Candidate) -> bool:
    """Prefer a better score, then the provision the quotation makes up more of.

    Between two provisions that both contain the passage verbatim, the one whose text
    *is* the passage is the source; the other merely repeats it in a longer sentence.
    """
    if candidate.match.score != incumbent.match.score:
        return candidate.match.score > incumbent.match.score
    if len(candidate.provision.full_text) != len(incumbent.provision.full_text):
        return len(candidate.provision.full_text) < len(incumbent.provision.full_text)
    if len(candidate.provision.path) != len(incumbent.provision.path):
        return len(candidate.provision.path) > len(incumbent.provision.path)
    return candidate.provision.id < incumbent.provision.id


def _relocation_note(provision: Provision) -> str:
    """Describe where the quoted language actually appears."""
    display = cite_norm.display_for(provision.source, provision.section, provision.path)
    heading = f" — {provision.heading}" if provision.heading else ""
    return f"the quoted language appears at {display}{heading}"


def _global_queries(tokens: list[str]) -> list[str]:
    """Build the FTS expressions used for the global search, most precise first."""
    quoted = [f'"{token}"' for token in tokens]
    queries: list[str] = []
    if len(quoted) >= 2:
        # FTS5 spells proximity as a function, not as the FTS3-style NEAR/N operator.
        queries.append(f"NEAR({' '.join(quoted)}, {QUOTE_NEAR_DISTANCE})")
    queries.append(" ".join(quoted))
    return queries


def word_diff(quote: str, source: str) -> str:
    """Render a word-level diff as ``~~removed~~ **added**`` (SPEC 6.7)."""
    quote_words = textnorm.words(textnorm.normalize(quote))
    source_words = textnorm.words(textnorm.normalize(source))
    matcher = difflib.SequenceMatcher(a=source_words, b=quote_words, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append(" ".join(source_words[i1:i2]))
        else:
            if i1 != i2:
                out.append(f"~~{' '.join(source_words[i1:i2])}~~")
            if j1 != j2:
                out.append(f"**{' '.join(quote_words[j1:j2])}**")
    return " ".join(part for part in out if part)


def verify_quotes(
    connection: sqlite3.Connection,
    text: str,
    citations: list[Citation],
    results: list[CitationResult],
    *,
    case_prober: CaseProber | None = None,
) -> list[QuoteCheck]:
    """Check every quotation in ``text``, attaching each to its citation.

    Quotations that belong to a citation are appended to that citation's result; the
    orphans are returned.
    """
    quotes = extract_quotes(text)
    if not quotes:
        return []
    sentences = split_sentences(text)
    attached, orphans = associate(quotes, citations, sentences, text)
    verifier = QuoteVerifier(connection, case_prober=case_prober)
    for index, quoted in attached.items():
        result = results[index]
        for quote in quoted:
            result.quotes.append(verifier.check(quote, result))
    return [verifier.check(quote, None) for quote in orphans]


__all__ = [
    "ABBREVIATIONS",
    "CaseProber",
    "ExtractedQuote",
    "QuoteVerifier",
    "associate",
    "extract_quotes",
    "prepare",
    "split_sentences",
    "verify_quotes",
    "word_diff",
]
