"""Table-driven tests for citation extraction (SPEC 6.1, 6.2)."""

from __future__ import annotations

import pytest

from taxcite.citations import extract_citations, parse_citation
from taxcite.errors import MalformedCitationError
from taxcite.models import RegKind, SourceType

# (input, [(canonical_id or None, display)])
IRC_CASES: list[tuple[str, list[tuple[str | None, str]]]] = [
    ("§ 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("§162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("§ 162(a)", [("/us/usc/t26/s162/a", "I.R.C. § 162(a)")]),
    (
        "§ 162(a)(1)(A)(i)(I)",
        [("/us/usc/t26/s162/a/1/A/i/I", "I.R.C. § 162(a)(1)(A)(i)(I)")],
    ),
    ("Section 162(a)", [("/us/usc/t26/s162/a", "I.R.C. § 162(a)")]),
    ("section 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("Sec. 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("sec. 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("IRC § 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("I.R.C. § 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("IRC Section 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("Code § 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("Code section 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("26 U.S.C. § 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("26 USC 162", [("/us/usc/t26/s162", "I.R.C. § 162")]),
    ("26 U.S.C. 162(a)", [("/us/usc/t26/s162/a", "I.R.C. § 162(a)")]),
    ("26 U.S.C.A. § 61", [("/us/usc/t26/s61", "I.R.C. § 61")]),
    ("§ 45Q", [("/us/usc/t26/s45Q", "I.R.C. § 45Q")]),
    ("§ 280G", [("/us/usc/t26/s280G", "I.R.C. § 280G")]),
    ("§ 1400Z-2", [("/us/usc/t26/s1400Z-2", "I.R.C. § 1400Z-2")]),
    ("§ 199A", [("/us/usc/t26/s199A", "I.R.C. § 199A")]),
    (
        "§ 7701(a)(30)(A)",
        [("/us/usc/t26/s7701/a/30/A", "I.R.C. § 7701(a)(30)(A)")],
    ),
    ("§ 163(j)(1)", [("/us/usc/t26/s163/j/1", "I.R.C. § 163(j)(1)")]),
    ("§ 1(i)", [("/us/usc/t26/s1/i", "I.R.C. § 1(i)")]),
    ("§ 6011(e)(2)(B)", [("/us/usc/t26/s6011/e/2/B", "I.R.C. § 6011(e)(2)(B)")]),
]

IRC_MULTI_CASES: list[tuple[str, list[tuple[str | None, str]]]] = [
    (
        "§§ 162 and 263",
        [
            ("/us/usc/t26/s162", "I.R.C. § 162"),
            ("/us/usc/t26/s263", "I.R.C. § 263"),
        ],
    ),
    (
        "§§ 162(a), 263(a), and 263A",
        [
            ("/us/usc/t26/s162/a", "I.R.C. § 162(a)"),
            ("/us/usc/t26/s263/a", "I.R.C. § 263(a)"),
            ("/us/usc/t26/s263A", "I.R.C. § 263A"),
        ],
    ),
    (
        "sections 1001 and 1012",
        [
            ("/us/usc/t26/s1001", "I.R.C. § 1001"),
            ("/us/usc/t26/s1012", "I.R.C. § 1012"),
        ],
    ),
    (
        "§§ 1401–1403",
        [
            ("/us/usc/t26/s1401", "I.R.C. § 1401"),
            ("/us/usc/t26/s1402", "I.R.C. § 1402"),
            ("/us/usc/t26/s1403", "I.R.C. § 1403"),
        ],
    ),
    (
        "§ 162(a)–(c)",
        [
            ("/us/usc/t26/s162/a", "I.R.C. § 162(a)"),
            ("/us/usc/t26/s162/b", "I.R.C. § 162(b)"),
            ("/us/usc/t26/s162/c", "I.R.C. § 162(c)"),
        ],
    ),
    (
        "§ 162(a) and (b)",
        [
            ("/us/usc/t26/s162/a", "I.R.C. § 162(a)"),
            ("/us/usc/t26/s162/b", "I.R.C. § 162(b)"),
        ],
    ),
    (
        "sections 704 through 707",
        [
            ("/us/usc/t26/s704", "I.R.C. § 704"),
            ("/us/usc/t26/s705", "I.R.C. § 705"),
            ("/us/usc/t26/s706", "I.R.C. § 706"),
            ("/us/usc/t26/s707", "I.R.C. § 707"),
        ],
    ),
]

REG_CASES: list[tuple[str, list[tuple[str | None, str]]]] = [
    (
        "Treas. Reg. § 1.162-1(a)",
        [("/us/cfr/t26/s1.162-1/a", "Treas. Reg. § 1.162-1(a)")],
    ),
    (
        "Treas. Reg. §1.263(a)-4(b)(1)",
        [("/us/cfr/t26/s1.263(a)-4/b/1", "Treas. Reg. § 1.263(a)-4(b)(1)")],
    ),
    ("Reg. § 1.61-1", [("/us/cfr/t26/s1.61-1", "Treas. Reg. § 1.61-1")]),
    ("Regs. § 1.199A-1", [("/us/cfr/t26/s1.199A-1", "Treas. Reg. § 1.199A-1")]),
    (
        "Treasury Regulation section 1.1502-13",
        [("/us/cfr/t26/s1.1502-13", "Treas. Reg. § 1.1502-13")],
    ),
    (
        "Treas. Regs. §§ 1.162-1 and 1.162-2",
        [
            ("/us/cfr/t26/s1.162-1", "Treas. Reg. § 1.162-1"),
            ("/us/cfr/t26/s1.162-2", "Treas. Reg. § 1.162-2"),
        ],
    ),
    ("26 C.F.R. § 1.162-1", [("/us/cfr/t26/s1.162-1", "Treas. Reg. § 1.162-1")]),
    ("26 CFR 1.162-1", [("/us/cfr/t26/s1.162-1", "Treas. Reg. § 1.162-1")]),
    (
        "Temp. Treas. Reg. § 1.469-5T",
        [("/us/cfr/t26/s1.469-5T", "Temp. Treas. Reg. § 1.469-5T")],
    ),
    (
        "§ 1.469-5T(a)",
        [("/us/cfr/t26/s1.469-5T/a", "Temp. Treas. Reg. § 1.469-5T(a)")],
    ),
    (
        "Prop. Treas. Reg. § 1.199A-1",
        [("/us/cfr/t26/s1.199A-1", "Prop. Treas. Reg. § 1.199A-1")],
    ),
    (
        "Prop. Reg. § 1.1411-4",
        [("/us/cfr/t26/s1.1411-4", "Prop. Treas. Reg. § 1.1411-4")],
    ),
    ("§ 1.162-1", [("/us/cfr/t26/s1.162-1", "Treas. Reg. § 1.162-1")]),
    (
        "Treas. Reg. § 301.7701-3(b)(1)(ii)",
        [("/us/cfr/t26/s301.7701-3/b/1/ii", "Treas. Reg. § 301.7701-3(b)(1)(ii)")],
    ),
    (
        "§ 1.1400Z2(a)-1",
        [("/us/cfr/t26/s1.1400Z2(a)-1", "Treas. Reg. § 1.1400Z2(a)-1")],
    ),
    (
        "§ 31.3121(a)-1",
        [("/us/cfr/t26/s31.3121(a)-1", "Treas. Reg. § 31.3121(a)-1")],
    ),
    ("§ 1.263A-1(e)", [("/us/cfr/t26/s1.263A-1/e", "Treas. Reg. § 1.263A-1(e)")]),
]


def _actual(text: str) -> list[tuple[str | None, str]]:
    return [(c.canonical_id, c.display) for c in extract_citations(text)]


@pytest.mark.parametrize(("text", "expected"), IRC_CASES + IRC_MULTI_CASES + REG_CASES)
def test_extract_citations(text: str, expected: list[tuple[str | None, str]]) -> None:
    assert _actual(text) == expected


@pytest.mark.parametrize(("text", "expected"), IRC_CASES)
def test_irc_source(text: str, expected: list[tuple[str | None, str]]) -> None:
    del expected
    assert all(c.source is SourceType.IRC for c in extract_citations(text))


@pytest.mark.parametrize(("text", "expected"), REG_CASES)
def test_reg_source(text: str, expected: list[tuple[str | None, str]]) -> None:
    del expected
    assert all(c.source is SourceType.REG for c in extract_citations(text))


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Treas. Reg. § 1.162-1", RegKind.FINAL),
        ("Temp. Treas. Reg. § 1.469-5T", RegKind.TEMPORARY),
        ("§ 1.469-5T(a)", RegKind.TEMPORARY),
        ("Prop. Treas. Reg. § 1.199A-1", RegKind.PROPOSED),
        ("Prop. Reg. § 1.1411-4", RegKind.PROPOSED),
    ],
)
def test_reg_kind(text: str, kind: RegKind) -> None:
    (citation,) = extract_citations(text)
    assert citation.reg_kind is kind


GUIDANCE_CASES = [
    ("Rev. Rul. 2019-24", "Rev. Rul.", "2019-24", SourceType.IRS_GUIDANCE),
    ("Revenue Ruling 2019-24", "Rev. Rul.", "2019-24", SourceType.IRS_GUIDANCE),
    ("Rev. Proc. 2023-34", "Rev. Proc.", "2023-34", SourceType.IRS_GUIDANCE),
    ("Notice 2024-7", "Notice", "2024-7", SourceType.IRS_GUIDANCE),
    ("Ann. 2023-1", "Ann.", "2023-1", SourceType.IRS_GUIDANCE),
    ("T.D. 9959", "T.D.", "9959", SourceType.IRS_GUIDANCE),
    ("PLR 202301001", "PLR", "202301001", SourceType.OTHER),
]


@pytest.mark.parametrize(("text", "gtype", "gnum", "source"), GUIDANCE_CASES)
def test_guidance_citations(text: str, gtype: str, gnum: str, source: SourceType) -> None:
    (citation,) = extract_citations(text)
    assert citation.guidance_type == gtype
    assert citation.guidance_number == gnum
    assert citation.source is source
    assert citation.canonical_id is None


def test_irb_citation() -> None:
    (citation,) = extract_citations("See 2019-45 I.R.B. 1234 for the ruling.")
    assert citation.source is SourceType.IRS_GUIDANCE
    assert citation.guidance_number == "2019-45 I.R.B. 1234"


@pytest.mark.parametrize(
    "text",
    [
        "Commissioner v. Groetzinger, 480 U.S. 23 (1987)",
        "Welch v. Helvering, 290 U.S. 111 (1933)",
        "T.C. Memo. 2020-12",
    ],
)
def test_case_citations(text: str) -> None:
    citations = extract_citations(text)
    assert citations
    assert all(c.source is SourceType.CASE for c in citations)


# --------------------------------------------------------------------------------------
# Negative cases
# --------------------------------------------------------------------------------------

NEGATIVE_CASES = [
    "Section 3 of the Agreement is controlling.",
    "See section 3 of the Tax Cuts and Jobs Act.",
    "Section 12 of this Plan governs.",
    "Pub. L. 115-97, section 13101, amended the rule.",
    "Visit https://uscode.house.gov/view.xhtml?section=162 for details.",
    "Use the `section 162` helper in the code.",
    "```\n§ 162(a) example\n```",
    "The 162 rule is well known.",
    "Revenue in section headers is irrelevant.",
]


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_no_false_positives(text: str) -> None:
    assert extract_citations(text) == []


def test_emphasis_added_not_absorbed() -> None:
    (citation,) = extract_citations("§ 162(a) (emphasis added)")
    assert citation.path == ["a"]
    assert citation.canonical_id == "/us/usc/t26/s162/a"


def test_trailing_punctuation_excluded() -> None:
    (citation,) = extract_citations("See § 162(a).")
    assert citation.raw == "§ 162(a)"


def test_citation_span_points_at_source_text() -> None:
    text = "As provided in § 162(a), a deduction is allowed."
    (citation,) = extract_citations(text)
    start, end = citation.span
    assert text[start:end] == "§ 162(a)"


def test_inline_code_masking_preserves_later_offsets() -> None:
    text = "Use `foo()` and then § 162(a)."
    (citation,) = extract_citations(text)
    start, end = citation.span
    assert text[start:end] == "§ 162(a)"


def test_multiple_citations_in_a_sentence() -> None:
    text = "Compare § 162(a) with Treas. Reg. § 1.162-1(a) and Rev. Rul. 2019-24."
    citations = extract_citations(text)
    assert [c.display for c in citations] == [
        "I.R.C. § 162(a)",
        "Treas. Reg. § 1.162-1(a)",
        "Rev. Rul. 2019-24",
    ]


def test_expanded_citations_share_parent_span_and_raw() -> None:
    text = "§§ 162 and 263"
    citations = extract_citations(text)
    assert len(citations) == 2
    assert {c.span for c in citations} == {(0, len(text))}
    assert {c.raw for c in citations} == {text}


def test_long_range_is_not_expanded() -> None:
    citations = extract_citations("§§ 1 through 500")
    assert [c.section for c in citations] == ["1", "500"]
    assert any("range endpoints not expanded" in w for c in citations for w in c.warnings)


def test_level_warning_for_impossible_token() -> None:
    (citation,) = extract_citations("§ 162(3)")
    assert citation.canonical_id == "/us/usc/t26/s162/3"
    assert citation.warnings == ["unexpected token '(3)' at subsection level"]


def test_no_level_warning_for_valid_deep_path() -> None:
    (citation,) = extract_citations("§ 7701(a)(30)(A)")
    assert citation.warnings == []


# --------------------------------------------------------------------------------------
# parse_citation
# --------------------------------------------------------------------------------------


def test_parse_citation_single() -> None:
    citation = parse_citation("§ 162(a)")
    assert citation.canonical_id == "/us/usc/t26/s162/a"


def test_parse_citation_rejects_nonsense() -> None:
    with pytest.raises(MalformedCitationError):
        parse_citation("not a citation at all")


def test_parse_citation_rejects_multiple() -> None:
    with pytest.raises(MalformedCitationError):
        parse_citation("§§ 162 and 263")


def test_parse_citation_tolerates_duplicates() -> None:
    assert parse_citation("§ 162 (see § 162)").canonical_id == "/us/usc/t26/s162"
