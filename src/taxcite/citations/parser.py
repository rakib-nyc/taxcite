"""Extraction of citations from free text (SPEC 6.2).

The pipeline masks regions that can never hold a citation, runs every pattern, resolves
overlapping candidates, expands ``§§`` lists and ranges into individual citations, and
finally normalises each one into a canonical identifier.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from taxcite.citations import normalize
from taxcite.citations.levels import validate_path
from taxcite.citations.patterns import (
    CASE_RE,
    COMMENTARY_RE,
    FENCED_CODE_RE,
    GUIDANCE_RE,
    INLINE_CODE_RE,
    IRB_RE,
    IRC_SECTION_NUM,
    LAW_REVIEW_RE,
    LIST_SEP,
    MARKDOWN_LINK_TARGET_RE,
    NON_TAX_AFTER_RE,
    NON_TAX_BEFORE_RE,
    PINPOINT_TOKEN,
    RANGE_SEP,
    REG_SECTION_NUM,
    SECTION_CITE_RE,
    TREATISE_RE,
    URL_RE,
)
from taxcite.config import FALSE_POSITIVE_CONTEXT_CHARS, MAX_RANGE_EXPANSION
from taxcite.errors import MalformedCitationError
from taxcite.models import Citation, RegKind, SourceType

logger: Final = logging.getLogger("taxcite.citations")

_REG_NUM_RE: Final = re.compile(rf"^(?:{REG_SECTION_NUM})$")
_IRC_NUM_RE: Final = re.compile(rf"^(?:{IRC_SECTION_NUM})$")
_TOKEN_RE: Final = re.compile(r"\(([0-9A-Za-z]{1,6})\)")
_ELEMENT_RE: Final = re.compile(
    rf"(?:(?P<num>{REG_SECTION_NUM}|{IRC_SECTION_NUM}))?(?P<path>(?:\s?{PINPOINT_TOKEN})*)"
)
_RANGE_SEP_RE: Final = re.compile(RANGE_SEP)
_LIST_SEP_RE: Final = re.compile(LIST_SEP)

#: Guidance types that are taxpayer-specific and never published as precedent.
_UNPUBLISHED_GUIDANCE: Final = (
    "PLR",
    "PRIV",
    "TAM",
    "CCA",
    "GCM",
    "FSA",
    "AOD",
    "ILM",
)

#: Relative specificity used to break ties between overlapping candidate matches.
_SOURCE_PRIORITY: Final[dict[SourceType, int]] = {
    SourceType.REG: 3,
    SourceType.IRC: 2,
    SourceType.IRS_GUIDANCE: 1,
    SourceType.CASE: 1,
    SourceType.SECONDARY: 1,
    SourceType.OTHER: 0,
}


# --------------------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------------------


def mask_text(text: str) -> str:
    """Blank out code spans, fenced blocks, URLs, and link targets, preserving offsets.

    Replacement is space-for-character so that every match offset in the masked string is
    also valid in the original.
    """
    chars = list(text)
    for pattern in (
        FENCED_CODE_RE,
        INLINE_CODE_RE,
        URL_RE,
        MARKDOWN_LINK_TARGET_RE,
    ):
        for match in pattern.finditer(text):
            for index in range(match.start(), match.end()):
                if chars[index] != "\n":
                    chars[index] = " "
    return "".join(chars)


# --------------------------------------------------------------------------------------
# Candidate collection
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Candidate:
    """An unexpanded regex hit, before overlap resolution."""

    start: int
    end: int
    source: SourceType
    match: re.Match[str]
    kind: str


def _is_false_positive(text: str, match: re.Match[str]) -> bool:
    """Return ``True`` for "Section 3 of the Agreement"-style non-tax references."""
    before = text[max(0, match.start() - FALSE_POSITIVE_CONTEXT_CHARS) : match.start()]
    if NON_TAX_BEFORE_RE.search(before):
        return True
    after = text[match.end() : match.end() + FALSE_POSITIVE_CONTEXT_CHARS + 20]
    return bool(NON_TAX_AFTER_RE.match(after))


def _is_markerless_authority(match: re.Match[str]) -> bool:
    """Return ``True`` for a weak authority reference such as "Code 162" with no marker."""
    if not match.group("auth") or match.group("mark1"):
        return False
    return not (match.group("usc") or match.group("cfr") or match.group("reg"))


def _collect(text: str) -> list[_Candidate]:
    """Run every pattern over ``text`` and return the raw candidates."""
    candidates: list[_Candidate] = []
    for match in SECTION_CITE_RE.finditer(text):
        if _is_false_positive(text, match):
            logger.debug("skipped non-tax section reference: %r", match.group(0))
            continue
        if _is_markerless_authority(match):
            # "Code 162" without a section marker is too weak a signal; "26 USC 162"
            # and "Treas. Reg. 1.162-1" are not.
            logger.debug("skipped markerless authority reference: %r", match.group(0))
            continue
        source = SourceType.REG if _looks_like_reg(match) else SourceType.IRC
        candidates.append(_Candidate(match.start(), match.end(), source, match, "section"))
    for match in GUIDANCE_RE.finditer(text):
        gtype = match.group("gtype") or match.group("mtype") or ""
        source = (
            SourceType.OTHER
            if gtype.strip().upper().startswith(_UNPUBLISHED_GUIDANCE)
            else SourceType.IRS_GUIDANCE
        )
        candidates.append(_Candidate(match.start(), match.end(), source, match, "guidance"))
    for match in IRB_RE.finditer(text):
        candidates.append(
            _Candidate(match.start(), match.end(), SourceType.IRS_GUIDANCE, match, "irb")
        )
    for match in CASE_RE.finditer(text):
        candidates.append(_Candidate(match.start(), match.end(), SourceType.CASE, match, "case"))
    for pattern, label in (
        (LAW_REVIEW_RE, "law review"),
        (TREATISE_RE, "treatise"),
        (COMMENTARY_RE, "commentary"),
    ):
        for match in pattern.finditer(text):
            candidates.append(
                _Candidate(
                    match.start(), match.end(), SourceType.SECONDARY, match, f"secondary:{label}"
                )
            )
    return candidates


def _looks_like_reg(match: re.Match[str]) -> bool:
    """Return ``True`` if a section-citation match points at the regulations."""
    if match.group("reg") or match.group("cfr"):
        return True
    elements = _split_body(match.group("body"))
    num = elements[0].num if elements else None
    return bool(num and _REG_NUM_RE.match(num))


def _resolve_overlaps(candidates: list[_Candidate]) -> list[_Candidate]:
    """Keep the longest candidate per overlapping group; break ties by source specificity."""
    ordered = sorted(
        candidates,
        key=lambda c: (c.start, -(c.end - c.start), -_SOURCE_PRIORITY[c.source]),
    )
    kept: list[_Candidate] = []
    for candidate in ordered:
        clash = next((k for k in kept if candidate.start < k.end and k.start < candidate.end), None)
        if clash is None:
            kept.append(candidate)
            continue
        better = (candidate.end - candidate.start, _SOURCE_PRIORITY[candidate.source]) > (
            clash.end - clash.start,
            _SOURCE_PRIORITY[clash.source],
        )
        if better:
            kept.remove(clash)
            kept.append(candidate)
    return sorted(kept, key=lambda c: c.start)


# --------------------------------------------------------------------------------------
# Expansion
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Element:
    """One section-or-pinpoint element parsed out of a citation body."""

    num: str | None
    path: list[str]
    preceded_by_range: bool


def _split_body(body: str) -> list[_Element]:
    """Scan a citation body into elements, remembering which follow a range separator.

    Scanning rather than splitting matters: a hyphen is both a range separator and part
    of ``1.162-1`` and ``1400Z-2``, so separators may only be recognised at positions
    where no element starts.
    """
    elements: list[_Element] = []
    pending_range = False
    position = 0
    while position < len(body):
        element = _ELEMENT_RE.match(body, position)
        if element is not None and element.end() > position:
            path = _TOKEN_RE.findall(element.group("path") or "")
            elements.append(_Element(element.group("num"), path, pending_range))
            pending_range = False
            position = element.end()
            continue
        separator = _RANGE_SEP_RE.match(body, position)
        if separator is not None and separator.end() > position:
            pending_range = True
            position = separator.end()
            continue
        separator = _LIST_SEP_RE.match(body, position)
        if separator is not None and separator.end() > position:
            pending_range = False
            position = separator.end()
            continue
        break
    return elements


def _letter_range(start: str, end: str) -> list[str] | None:
    """Expand ``a``..``c`` style ranges; return ``None`` if not a simple letter range."""
    if len(start) != 1 or len(end) != 1 or not start.isalpha() or not end.isalpha():
        return None
    if start.isupper() != end.isupper():
        return None
    first, last = ord(start), ord(end)
    if not 0 < last - first < MAX_RANGE_EXPANSION:
        return None
    return [chr(code) for code in range(first, last + 1)]


def _numeric_range(start: str, end: str) -> list[str] | None:
    """Expand ``1401``..``1403`` style ranges; return ``None`` if not simple numbers."""
    if not (start.isdigit() and end.isdigit()):
        return None
    first, last = int(start), int(end)
    if not 0 < last - first < MAX_RANGE_EXPANSION:
        return None
    return [str(value) for value in range(first, last + 1)]


@dataclass(slots=True)
class _Target:
    """A concrete (section, path) pair to be turned into a Citation."""

    num: str
    path: list[str]
    warnings: list[str]


def _expand(elements: list[_Element]) -> list[_Target]:
    """Turn parsed elements into concrete targets, expanding lists and ranges."""
    targets: list[_Target] = []
    current_num: str | None = None
    for element in elements:
        if element.num is None:
            # A bare pinpoint such as "(b)" in "§ 162(a) and (b)": it only makes sense
            # when the previous element carried a section number.
            if current_num is None or not element.path:
                continue
            if element.preceded_by_range and targets and targets[-1].path:
                expanded = _expand_pinpoint_range(targets[-1], element.path, current_num)
                if expanded is not None:
                    targets.extend(expanded)
                    continue
            targets.append(_Target(current_num, list(element.path), []))
            continue
        current_num = element.num
        if element.preceded_by_range and targets:
            expanded = _expand_section_range(targets[-1], element)
            if expanded is not None:
                targets.extend(expanded)
                continue
            targets.append(
                _Target(element.num, list(element.path), ["range endpoints not expanded"])
            )
            continue
        targets.append(_Target(element.num, list(element.path), []))
    return targets


def _expand_section_range(previous: _Target, element: _Element) -> list[_Target] | None:
    """Expand ``§§ 1401-1403`` into the intervening sections, or return ``None``."""
    if previous.path or element.path or element.num is None:
        return None
    values = _numeric_range(previous.num, element.num)
    if values is None:
        return None
    return [_Target(value, [], []) for value in values[1:]]


def _expand_pinpoint_range(
    previous: _Target, path: list[str], section: str
) -> list[_Target] | None:
    """Expand ``§ 162(a)-(c)`` into one target per subdivision, or return ``None``."""
    if len(path) != 1 or len(previous.path) != 1:
        return None
    values = _letter_range(previous.path[0], path[0]) or _numeric_range(previous.path[0], path[0])
    if values is None:
        return None
    return [_Target(section, [value], []) for value in values[1:]]


# --------------------------------------------------------------------------------------
# Citation construction
# --------------------------------------------------------------------------------------


def _reg_kind(match: re.Match[str], section: str) -> RegKind:
    """Classify a regulation citation as final, temporary, or proposed."""
    qualifier = (match.group("regqual") or "").lower()
    if "prop" in qualifier:
        return RegKind.PROPOSED
    if "temp" in qualifier or re.search(r"-\d+T$", section):
        return RegKind.TEMPORARY
    return RegKind.FINAL


def _citation_from_target(candidate: _Candidate, target: _Target, raw: str) -> Citation:
    """Build a :class:`Citation` from one expanded target."""
    source = candidate.source
    if _REG_NUM_RE.match(target.num):
        source = SourceType.REG
    elif source is SourceType.REG and _IRC_NUM_RE.match(target.num):
        # "Treas. Reg. § 162" — the authority says reg but the number is statutory.
        source = SourceType.IRC
    reg = source is SourceType.REG
    reg_kind = _reg_kind(candidate.match, target.num) if reg else None
    warnings = [*target.warnings, *validate_path(target.path, reg=reg)]
    return Citation(
        raw=raw,
        span=(candidate.start, candidate.end),
        source=source,
        section=target.num,
        path=target.path,
        reg_kind=reg_kind,
        canonical_id=normalize.canonical_id(source, target.num, target.path),
        display=normalize.display_for(source, target.num, target.path, reg_kind),
        warnings=warnings,
    )


def _guidance_citation(candidate: _Candidate, text: str) -> Citation:
    """Build a :class:`Citation` for recognised IRS guidance."""
    match = candidate.match
    raw = text[candidate.start : candidate.end]
    if candidate.kind == "irb":
        gtype, gnum = "I.R.B.", f"{match.group('vol')} I.R.B. {match.group('page')}"
    elif match.group("tdtype"):
        gtype, gnum = "T.D.", match.group("tdnum")
    elif match.group("mtype"):
        gtype = _canonical_guidance_type(match.group("mtype"))
        gnum = match.group("mnum")
    else:
        gtype = _canonical_guidance_type(match.group("gtype"))
        gnum = match.group("gnum")
    # Older Bulletins number with an en dash ("Rev. Proc. 2015-1" appears as
    # "2015\u20131"); the canonical form uses a hyphen so the two agree.
    gnum = gnum.replace("\u2013", "-").replace("\u2014", "-")
    return Citation(
        raw=raw,
        span=(candidate.start, candidate.end),
        source=candidate.source,
        display=f"{gtype} {gnum}" if candidate.kind != "irb" else gnum,
        guidance_type=gtype,
        guidance_number=gnum,
    )


_GUIDANCE_TYPE_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("revrul", "Rev. Rul."),
    ("revenueruling", "Rev. Rul."),
    ("revproc", "Rev. Proc."),
    ("revenueprocedure", "Rev. Proc."),
    ("notice", "Notice"),
    ("announcement", "Ann."),
    ("ann", "Ann."),
    ("td", "T.D."),
    ("treasurydecision", "T.D."),
    ("plr", "PLR"),
    ("privltrrul", "PLR"),
    ("tam", "TAM"),
    ("cca", "CCA"),
    ("gcm", "GCM"),
    ("fsa", "FSA"),
    ("aod", "AOD"),
    ("ilm", "ILM"),
)


def _canonical_guidance_type(raw: str) -> str:
    """Normalise the many spellings of a guidance type to a canonical abbreviation."""
    key = re.sub(r"[^a-z]", "", raw.lower())
    for prefix, canonical in _GUIDANCE_TYPE_MAP:
        if key == prefix:
            return canonical
    return raw.strip()


def _secondary_citation(candidate: _Candidate, text: str) -> Citation:
    """Build a :class:`Citation` for a treatise, journal, or practitioner source."""
    raw = text[candidate.start : candidate.end].strip()
    kind = candidate.kind.split(":", 1)[1]
    return Citation(
        raw=raw,
        span=(candidate.start, candidate.start + len(raw)),
        source=SourceType.SECONDARY,
        display=raw,
        guidance_type=kind,
    )


def _case_citation(candidate: _Candidate, text: str) -> Citation:
    """Build a :class:`Citation` for a recognised court decision.

    The case name and the reporter citation are kept apart, because they fail
    independently: a reporter citation can be real while the name attached to it
    belongs to a different case entirely.
    """
    raw = text[candidate.start : candidate.end].strip()
    match = candidate.match
    reporter = match.groupdict().get("reporter_cite")
    name = match.groupdict().get("case_name")
    pin = match.groupdict().get("case_pin")
    year = match.groupdict().get("case_year")
    return Citation(
        raw=raw,
        span=(candidate.start, candidate.start + len(raw)),
        source=SourceType.CASE,
        display=raw,
        case_name=" ".join(name.split()) if name else None,
        reporter_cite=" ".join(reporter.split()) if reporter else None,
        pin_page=int(pin) if pin else None,
        case_year=int(year) if year else _memo_year(reporter),
    )


def _memo_year(reporter: str | None) -> int | None:
    """Read the year out of a memorandum citation, which carries it in the number."""
    if not reporter:
        return None
    match = re.search(r"Memo\.?\s+(\d{4})-", reporter)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def extract_citations(text: str) -> list[Citation]:
    """Extract every tax citation in ``text``, in document order.

    Lists and ranges are expanded into one :class:`~taxcite.models.Citation` per cited
    provision. Expanded citations share the span and ``raw`` text of the phrase they came
    from, but each carries its own canonical id and display string.
    """
    masked = mask_text(text)
    citations: list[Citation] = []
    for candidate in _resolve_overlaps(_collect(masked)):
        raw = text[candidate.start : candidate.end]
        if candidate.kind in {"guidance", "irb"}:
            citations.append(_guidance_citation(candidate, text))
            continue
        if candidate.kind == "case":
            citations.append(_case_citation(candidate, text))
            continue
        if candidate.kind.startswith("secondary:"):
            citations.append(_secondary_citation(candidate, text))
            continue
        targets = _expand(_split_body(candidate.match.group("body")))
        if not targets:
            continue
        citations.extend(_citation_from_target(candidate, target, raw) for target in targets)
    citations.sort(key=lambda c: (c.span[0], c.canonical_id or ""))
    return citations


def parse_citation(text: str) -> Citation:
    """Parse a single citation string.

    Raises:
        MalformedCitationError: if ``text`` holds no citation, or more than one.
    """
    found = extract_citations(text)
    if not found:
        raise MalformedCitationError(
            f"could not parse a tax citation from {text.strip()!r}",
            hint='Try a form like "I.R.C. § 162(a)" or "Treas. Reg. § 1.162-1(a)".',
        )
    if len({c.canonical_id or c.display for c in found}) > 1:
        raise MalformedCitationError(
            f"{text.strip()!r} contains more than one citation; use extract_citations()"
        )
    return found[0]
