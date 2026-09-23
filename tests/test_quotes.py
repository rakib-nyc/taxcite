"""Tests for quotation extraction, association, and matching (SPEC 6.7)."""

from __future__ import annotations

import sqlite3

import pytest

from taxcite.citations import extract_citations
from taxcite.models import SourceType, Status
from taxcite.verify import textnorm
from taxcite.verify.quotes import (
    ExtractedQuote,
    QuoteVerifier,
    associate,
    extract_quotes,
    find_exact,
    find_fuzzy,
    prepare,
    split_sentences,
    strip_editorial,
    word_diff,
)

ORDINARY = (
    "There shall be allowed as a deduction all the ordinary and necessary "
    "expenses paid or incurred during the taxable year in carrying on any "
    "trade or business, including—"
)


# --------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("“Curly quotes” here", '"curly quotes" here'),
        ("en–dash and em—dash", "en-dash and em-dash"),
        ("soft­hyphen", "softhyphen"),
        ("zero​width", "zerowidth"),
        ("  collapsed   \n  whitespace  ", "collapsed whitespace"),
        ("MiXeD CaSe", "mixed case"),
        ("non breaking", "non breaking"),
        ("ellipsis…here", "ellipsis...here"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert textnorm.normalize(raw) == expected


def test_normalize_keeps_punctuation() -> None:
    assert textnorm.normalize("a, b; c.") == "a, b; c."


def test_loose_drops_punctuation() -> None:
    assert textnorm.loose("a, b; c.") == "a b c"


def test_normalized_maps_back_to_the_source() -> None:
    mapped = textnorm.normalize_mapped("The  “QUICK” brown fox")
    start = mapped.text.index("quick")
    assert mapped.source_slice(start, start + 5) == "QUICK"


def test_word_count() -> None:
    assert textnorm.word_count("one two three") == 3
    assert textnorm.word_count("   ") == 0


# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------


def test_extracts_straight_curly_and_block_quotes() -> None:
    text = (
        'He said "this is a straight quotation here".\n\n'
        "She wrote “this is a curly quotation here”.\n\n"
        "> this is a block quotation here\n> continuing on\n"
    )
    kinds = [quote.kind for quote in extract_quotes(text)]
    assert sorted(kinds) == ["blockquote", "curly", "straight"]


def test_block_quote_markers_are_stripped() -> None:
    (quote,) = extract_quotes("> first line of the quotation\n> and the second line\n")
    assert quote.text == "first line of the quotation\nand the second line"


def test_short_quotations_are_skipped() -> None:
    assert extract_quotes('He said "too short".') == []


def test_quotations_inside_code_are_skipped() -> None:
    assert extract_quotes('Use `"a quoted string in code"` here.') == []
    assert extract_quotes('```\n"a quoted string in a block"\n```') == []


def test_quotation_may_wrap_across_lines() -> None:
    (quote,) = extract_quotes('He said "this quotation wraps\nacross two lines here".')
    assert "wraps\nacross" in quote.text


def test_quotation_does_not_cross_a_blank_line() -> None:
    assert extract_quotes('An opening " mark with no close.\n\nA later paragraph.') == []


def test_span_points_at_the_quoted_text() -> None:
    text = 'As stated, "the quick brown fox jumps" and so on.'
    (quote,) = extract_quotes(text)
    start, end = quote.span
    assert text[start:end] == "the quick brown fox jumps"


@pytest.mark.parametrize(
    "suffix",
    [
        " (emphasis added)",
        " (citation omitted)",
        " (internal quotation marks omitted)",
        " (emphasis supplied)",
    ],
)
def test_strip_editorial(suffix: str) -> None:
    assert strip_editorial(f"the quoted words{suffix}") == "the quoted words"


# --------------------------------------------------------------------------------------
# Sentences
# --------------------------------------------------------------------------------------


def test_sentences_split_on_a_full_stop() -> None:
    text = "First sentence here. Second sentence here."
    spans = split_sentences(text)
    assert [text[a:b].strip() for a, b in spans] == [
        "First sentence here.",
        "Second sentence here.",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "See 26 U.S.C. § 162 for the rule.",
        "See Treas. Reg. § 1.162-1 for the rule.",
        "See Rev. Rul. 2019-24 for the rule.",
        "See I.R.C. § 61 for the rule.",
        "See Notice No. 7 for the rule.",
        "Welch v. Helvering held otherwise.",
        "Acme Inc. filed late.",
        "Widgets Co. filed late.",
        "Costs, e.g. rent, are deductible.",
        "Costs, i.e. rent, are deductible.",
    ],
)
def test_abbreviations_do_not_end_a_sentence(text: str) -> None:
    assert len(split_sentences(text)) == 1


def test_blank_line_ends_a_sentence() -> None:
    assert len(split_sentences("First paragraph\n\nSecond paragraph")) == 2


def test_lowercase_continuation_does_not_end_a_sentence() -> None:
    assert len(split_sentences("The rate is 3.8 percent of income.")) == 1


# --------------------------------------------------------------------------------------
# Association
# --------------------------------------------------------------------------------------


def _associate(text: str) -> tuple[dict[int, list[ExtractedQuote]], list[ExtractedQuote]]:
    return associate(extract_quotes(text), extract_citations(text), split_sentences(text), text)


def test_quote_attaches_to_the_preceding_citation_in_its_sentence() -> None:
    text = 'Section 162(a) provides that "the quoted words go right here".'
    attached, orphans = _associate(text)
    assert orphans == []
    assert list(attached) == [0]


def test_quote_attaches_to_a_following_citation_in_its_sentence() -> None:
    text = '"The quoted words go right here", as provided by section 162(a).'
    attached, orphans = _associate(text)
    assert orphans == []
    assert list(attached) == [0]


def test_block_quote_attaches_to_the_previous_sentence() -> None:
    text = "Section 162(a) provides:\n\n> the quoted words go right here\n"
    attached, orphans = _associate(text)
    assert orphans == []
    assert list(attached) == [0]


def test_quote_two_paragraphs_from_a_citation_is_an_orphan() -> None:
    text = "Section 162(a) is the rule.\n\n## A heading\n\n> the quoted words go here\n"
    attached, orphans = _associate(text)
    assert attached == {}
    assert len(orphans) == 1


def test_quote_with_no_citations_at_all_is_an_orphan() -> None:
    attached, orphans = _associate('Someone wrote "the quoted words go right here".')
    assert attached == {}
    assert len(orphans) == 1


def test_a_quote_attaches_to_the_citation_sentence_after_it() -> None:
    """Standard form: the quoted proposition, then its support in its own sentence."""
    text = (
        "An expense must be ordinary and necessary. The Court explained that "
        '"the quoted words go right here." Welch v. Helvering, 290 U.S. 111 (1933).'
    )
    citations = extract_citations(text)
    attached, orphans = _associate(text)
    assert orphans == []
    (index,) = attached
    assert citations[index].reporter_cite == "290 U.S. 111"


def test_the_earlier_statute_does_not_steal_the_quotation() -> None:
    """The bug this rule fixes: an accurate quotation reported as missing from § 162."""
    text = (
        "An expense is deductible under section 162(a) if it is necessary. The Court "
        'explained that "the quoted words go right here." Welch v. Helvering, 290 '
        "U.S. 111 (1933)."
    )
    citations = extract_citations(text)
    attached, _orphans = _associate(text)
    (index,) = attached
    assert citations[index].source is not SourceType.IRC


def test_a_following_sentence_that_is_prose_is_not_support() -> None:
    """Only a sentence that is *nothing but* citations counts as the attribution."""
    text = (
        "Section 162(a) provides the rule. The Court explained that "
        '"the quoted words go right here." The Commissioner disagreed in 290 U.S. 111.'
    )
    citations = extract_citations(text)
    attached, _orphans = _associate(text)
    (index,) = attached
    assert citations[index].source is SourceType.IRC


def test_a_signal_does_not_stop_a_sentence_being_a_citation_sentence() -> None:
    text = (
        "The rule is settled. The Court explained that "
        '"the quoted words go right here." See Welch v. Helvering, 290 U.S. 111 (1933).'
    )
    citations = extract_citations(text)
    attached, orphans = _associate(text)
    assert orphans == []
    (index,) = attached
    assert citations[index].reporter_cite == "290 U.S. 111"


def test_a_citation_inside_the_quote_is_not_its_attribution() -> None:
    text = 'The court said "the rule in section 162(a) is clear enough" today.'
    _attached, orphans = _associate(text)
    assert len(orphans) == 1


# --------------------------------------------------------------------------------------
# Preparation and matching
# --------------------------------------------------------------------------------------


def test_ellipsis_splits_a_quote_into_segments() -> None:
    prepared = prepare("the first part ... the second part")
    assert len(prepared.segments) == 2


@pytest.mark.parametrize("ellipsis", ["...", ". . .", "…"])
def test_every_ellipsis_spelling(ellipsis: str) -> None:
    assert len(prepare(f"first part here {ellipsis} second part here").segments) == 2


def test_single_letter_bracket_is_a_case_alteration() -> None:
    prepared = prepare("[t]here shall be allowed")
    assert find_exact(prepared, textnorm.normalize(ORDINARY)) is not None


def test_multi_word_bracket_is_a_wildcard() -> None:
    prepared = prepare("all the [several kinds of] expenses paid or incurred")
    assert prepared.segments[0].has_wildcard
    assert find_exact(prepared, textnorm.normalize(ORDINARY)) is not None


def test_segments_must_match_in_order() -> None:
    haystack = textnorm.normalize(ORDINARY)
    assert find_exact(prepare("ordinary and necessary ... trade or business"), haystack)
    assert find_exact(prepare("trade or business ... ordinary and necessary"), haystack) is None


def test_exact_match_scores_one() -> None:
    match = find_exact(prepare("ordinary and necessary expenses"), textnorm.normalize(ORDINARY))
    assert match is not None
    assert match.score == 1.0


def test_fuzzy_match_scores_below_one() -> None:
    match = find_fuzzy(prepare("ordinary and reasonable expenses"), textnorm.normalize(ORDINARY))
    assert match is not None
    assert 0.7 < match.score < 1.0


def test_fuzzy_match_of_nonsense_scores_low() -> None:
    match = find_fuzzy(
        prepare("quarterly submarine inspection protocols"), textnorm.normalize(ORDINARY)
    )
    assert match is None or match.score < 0.7


def test_word_diff_marks_the_change() -> None:
    diff = word_diff("ordinary and reasonable expenses", "ordinary and necessary expenses")
    assert "~~necessary~~" in diff
    assert "**reasonable**" in diff


def test_word_diff_of_identical_text_has_no_markers() -> None:
    assert word_diff("same words here", "same words here") == "same words here"


# --------------------------------------------------------------------------------------
# End-to-end matching against the index
# --------------------------------------------------------------------------------------


def _check(conn: sqlite3.Connection, text: str) -> list[tuple[str, Status]]:
    from taxcite.verify import verify_text

    report = verify_text(text, connection=conn)
    out = [
        (result.citation.display, quote.status)
        for result in report.results
        for quote in result.quotes
    ]
    out += [("<orphan>", quote.status) for quote in report.orphan_quotes]
    return out


def test_exact_quotation(conn: sqlite3.Connection) -> None:
    text = 'Section 162(a) allows a deduction for "all the ordinary and necessary expenses".'
    assert _check(conn, text) == [("I.R.C. § 162(a)", Status.QUOTE_EXACT)]


def test_one_word_changed_is_close(conn: sqlite3.Connection) -> None:
    text = (
        'Section 162(a) allows a deduction for "all the ordinary and reasonable '
        'expenses paid or incurred during the taxable year".'
    )
    assert _check(conn, text) == [("I.R.C. § 162(a)", Status.QUOTE_CLOSE)]


def test_close_quotation_carries_a_diff(conn: sqlite3.Connection) -> None:
    from taxcite.verify import verify_text

    text = (
        'Section 162(a) allows a deduction for "all the ordinary and reasonable '
        'expenses paid or incurred during the taxable year".'
    )
    (quote,) = verify_text(text, connection=conn).results[0].quotes
    assert quote.diff is not None
    assert "~~necessary~~" in quote.diff


def test_right_section_wrong_subdivision(conn: sqlite3.Connection) -> None:
    text = (
        'Section 162(b) provides that "there shall be allowed as a deduction all '
        'the ordinary and necessary expenses paid or incurred".'
    )
    assert _check(conn, text) == [("I.R.C. § 162(b)", Status.QUOTE_WRONG_PINPOINT)]


def test_wrong_pinpoint_names_the_real_provision(conn: sqlite3.Connection) -> None:
    from taxcite.verify import verify_text

    text = (
        'Section 162(b) provides that "there shall be allowed as a deduction all '
        'the ordinary and necessary expenses paid or incurred".'
    )
    (quote,) = verify_text(text, connection=conn).results[0].quotes
    assert quote.matched_provision_id == "/us/usc/t26/s162/a"
    assert quote.diff is not None
    assert "§ 162(a)" in quote.diff


def test_wrong_section_entirely(conn: sqlite3.Connection) -> None:
    text = (
        'Section 263(a) provides that "gross income means all income from whatever source derived".'
    )
    assert _check(conn, text) == [("I.R.C. § 263(a)", Status.QUOTE_MISATTRIBUTED)]


def test_fabricated_quotation(conn: sqlite3.Connection) -> None:
    text = (
        'Section 61(a) states that "all receipts of a commercial character shall be '
        'included in the computation of taxable profit for the year of receipt".'
    )
    assert _check(conn, text) == [("I.R.C. § 61(a)", Status.QUOTE_NOT_FOUND)]


def test_orphan_quotation_is_traced_to_its_source(conn: sqlite3.Connection) -> None:
    from taxcite.verify import verify_text

    text = "A heading\n\n> a citizen or resident of the United States\n"
    report = verify_text(text, connection=conn)
    (orphan,) = report.orphan_quotes
    assert orphan.status is Status.QUOTE_MISATTRIBUTED
    assert orphan.matched_provision_id == "/us/usc/t26/s7701/a/30/A"


def test_quotation_with_an_ellipsis_verifies(conn: sqlite3.Connection) -> None:
    text = (
        'Section 162(a) allows a deduction for "all the ordinary and necessary '
        'expenses ... in carrying on any trade or business".'
    )
    assert _check(conn, text) == [("I.R.C. § 162(a)", Status.QUOTE_EXACT)]


def test_quotation_with_a_bracketed_alteration_verifies(conn: sqlite3.Connection) -> None:
    text = (
        'Section 162(a) provides that "[t]here shall be allowed as a deduction all '
        'the ordinary and necessary expenses".'
    )
    assert _check(conn, text) == [("I.R.C. § 162(a)", Status.QUOTE_EXACT)]


def test_emphasis_added_does_not_break_a_match(conn: sqlite3.Connection) -> None:
    text = (
        'Section 162(a) allows a deduction for "all the ordinary and necessary '
        'expenses" (emphasis added).'
    )
    assert _check(conn, text) == [("I.R.C. § 162(a)", Status.QUOTE_EXACT)]


def test_curly_quotation_matches_straight_source(conn: sqlite3.Connection) -> None:
    text = "Section 162(a) allows a deduction for “all the ordinary and necessary expenses”."
    assert _check(conn, text) == [("I.R.C. § 162(a)", Status.QUOTE_EXACT)]


def test_a_quote_on_an_unresolvable_citation_still_gets_a_global_search(
    conn: sqlite3.Connection,
) -> None:
    text = (
        'Section 162A provides that "all the ordinary and necessary expenses paid or '
        'incurred during the taxable year" are deductible.'
    )
    assert _check(conn, text) == [("I.R.C. § 162A", Status.QUOTE_MISATTRIBUTED)]


def test_verifier_caches_normalised_text(conn: sqlite3.Connection) -> None:
    verifier = QuoteVerifier(conn)
    quote = ExtractedQuote("ordinary and necessary expenses", (0, 30), "straight")
    verifier.check(quote, None)
    assert verifier._normalized
