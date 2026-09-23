"""Tests for the raw regular expressions in :mod:`taxcite.citations.patterns`."""

from __future__ import annotations

import re

import pytest

from taxcite.citations.patterns import (
    CASE_RE,
    GUIDANCE_RE,
    IRB_RE,
    IRC_SECTION_NUM,
    NON_TAX_AFTER_RE,
    NON_TAX_BEFORE_RE,
    PINPOINT_PATH,
    REG_SECTION_NUM,
    SECTION_CITE_RE,
    URL_RE,
)

IRC_NUM_RE = re.compile(rf"^(?:{IRC_SECTION_NUM})$")
REG_NUM_RE = re.compile(rf"^(?:{REG_SECTION_NUM})$")
PINPOINT_RE = re.compile(rf"^(?:{PINPOINT_PATH})$")


@pytest.mark.parametrize(
    "number",
    ["1", "61", "162", "199A", "280G", "45Q", "1400Z-2", "7701", "6011", "1400N-4"],
)
def test_irc_section_numbers_match(number: str) -> None:
    assert IRC_NUM_RE.match(number)


@pytest.mark.parametrize("number", ["1.162-1", "1.162", "", "abc"])
def test_non_irc_section_numbers_do_not_fully_match(number: str) -> None:
    assert not IRC_NUM_RE.match(number)


@pytest.mark.parametrize(
    "number",
    [
        "1.162-1",
        "1.263(a)-4",
        "1.263A-1",
        "301.7701-3",
        "1.1400Z2(a)-1",
        "1.469-5T",
        "31.3121(a)-1",
        "1.1502-13",
        "20.2031-1",
    ],
)
def test_reg_section_numbers_match(number: str) -> None:
    assert REG_NUM_RE.match(number)


@pytest.mark.parametrize("number", ["162", "1.162", "1162-1"])
def test_non_reg_section_numbers_do_not_fully_match(number: str) -> None:
    assert not REG_NUM_RE.match(number)


@pytest.mark.parametrize(
    "path", ["", "(a)", "(a)(1)", "(a)(1)(A)(i)(I)", "(a) (1)", "(30)", "(aa)"]
)
def test_pinpoint_paths_match(path: str) -> None:
    assert PINPOINT_RE.match(path)


def test_pinpoint_does_not_absorb_parentheticals() -> None:
    match = SECTION_CITE_RE.search("§ 162(a) (emphasis added)")
    assert match is not None
    assert match.group("body") == "162(a)"


def test_section_mark_required_for_bare_numbers() -> None:
    assert SECTION_CITE_RE.search("The 162 rule") is None


@pytest.mark.parametrize(
    ("text", "gtype", "gnum"),
    [
        ("Rev. Rul. 2019-24", "Rev. Rul.", "2019-24"),
        ("Rev. Proc. 2023-34", "Rev. Proc.", "2023-34"),
        ("Notice 2024-7", "Notice", "2024-7"),
        ("Ann. 2023-1", "Ann.", "2023-1"),
        ("PLR 202301001", "PLR", "202301001"),
    ],
)
def test_guidance_pattern(text: str, gtype: str, gnum: str) -> None:
    match = GUIDANCE_RE.search(text)
    assert match is not None
    assert match.group("gtype") == gtype
    assert match.group("gnum") == gnum


def test_treasury_decision_pattern() -> None:
    match = GUIDANCE_RE.search("T.D. 9959 was published")
    assert match is not None
    assert match.group("tdnum") == "9959"


def test_irb_pattern() -> None:
    match = IRB_RE.search("2019-45 I.R.B. 1234")
    assert match is not None
    assert match.group("vol") == "2019-45"
    assert match.group("page") == "1234"


@pytest.mark.parametrize(
    "text",
    [
        "Commissioner v. Groetzinger, 480 U.S. 23 (1987)",
        "Welch v. Helvering, 290 U.S. 111 (1933)",
        "T.C. Memo. 2020-12",
        "Estate of Smith v. Commissioner, 123 T.C. 456",
    ],
)
def test_case_pattern(text: str) -> None:
    assert CASE_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section162",
        "www.ecfr.gov/current/title-26",
    ],
)
def test_url_pattern(text: str) -> None:
    match = URL_RE.search(text)
    assert match is not None
    assert match.group(0) == text


@pytest.mark.parametrize(
    "after",
    [
        " of the Agreement",
        " of the Act",
        " of this Agreement",
        " of the Tax Cuts and Jobs Act",
        " of the Affordable Care Act",
    ],
)
def test_non_tax_after_guard(after: str) -> None:
    assert NON_TAX_AFTER_RE.match(after)


@pytest.mark.parametrize("after", [" of the Code", " provides that", " of title 26"])
def test_non_tax_after_guard_allows_tax_context(after: str) -> None:
    assert NON_TAX_AFTER_RE.match(after) is None


@pytest.mark.parametrize("before", ["Pub. L. 115-97, ", "P.L. 115-97 "])
def test_non_tax_before_guard(before: str) -> None:
    assert NON_TAX_BEFORE_RE.search(before)
