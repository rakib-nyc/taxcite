"""Tests for the eCFR client and paragraph-hierarchy reconstruction (SPEC 4.2, 6.4)."""

from __future__ import annotations

import pytest

from taxcite.errors import SourceParseError
from taxcite.models import Provision, SourceType
from taxcite.sources.ecfr import (
    L1,
    L2,
    L3,
    L4,
    L5,
    L6,
    assign_level,
    parse_sections,
    part_of,
    split_markers,
)

from conftest import ECFR_DIR, FIXTURE_ECFR_DATE


def _provisions(name: str) -> dict[str, Provision]:
    parsed = list(parse_sections((ECFR_DIR / name).read_bytes(), FIXTURE_ECFR_DATE))
    return {p.id: p for section in parsed for p in section.provisions}


@pytest.fixture(scope="module")
def synthetic() -> dict[str, Provision]:
    return _provisions("synthetic_levels.xml")


# --------------------------------------------------------------------------------------
# Level assignment (the ambiguity cases SPEC 6.4 calls out)
# --------------------------------------------------------------------------------------


def test_letter_i_after_h_stays_at_the_letter_level() -> None:
    assert assign_level("i", italic=False, stack=[(L1, "h")]) == L1


def test_letter_j_after_i_stays_at_the_letter_level() -> None:
    assert assign_level("j", italic=False, stack=[(L1, "i")]) == L1


def test_roman_i_after_a_number_opens_the_roman_level() -> None:
    assert assign_level("i", italic=False, stack=[(L1, "k"), (L2, "2")]) == L3


def test_roman_ii_continues_the_roman_level() -> None:
    assert assign_level("ii", italic=False, stack=[(L1, "k"), (L2, "2"), (L3, "i")]) == L3


def test_v_after_iv_is_a_roman_five() -> None:
    assert assign_level("v", italic=False, stack=[(L1, "k"), (L2, "2"), (L3, "iv")]) == L3


def test_v_after_u_is_the_twenty_second_letter() -> None:
    assert assign_level("v", italic=False, stack=[(L1, "u")]) == L1


def test_first_number_opens_the_number_level() -> None:
    assert assign_level("1", italic=False, stack=[(L1, "a")]) == L2


def test_uppercase_opens_the_subclause_level() -> None:
    assert assign_level("A", italic=False, stack=[(L1, "b"), (L2, "1"), (L3, "i")]) == L4


def test_italic_numbers_are_a_deeper_level_than_plain_ones() -> None:
    stack = [(L1, "d"), (L2, "2"), (L3, "i"), (L4, "C")]
    assert assign_level("1", italic=True, stack=stack) == L5


def test_italic_romans_are_the_deepest_level() -> None:
    stack = [(L1, "d"), (L2, "2"), (L3, "i"), (L4, "C"), (L5, "2")]
    assert assign_level("i", italic=True, stack=stack) == L6


def test_a_marker_at_the_start_opens_the_first_level() -> None:
    assert assign_level("a", italic=False, stack=[]) == L1


def test_a_marker_that_fits_no_level_is_rejected() -> None:
    assert assign_level("!!", italic=False, stack=[]) is None


def test_double_letters_follow_z() -> None:
    assert assign_level("aa", italic=False, stack=[(L1, "z")]) == L1


# --------------------------------------------------------------------------------------
# Marker splitting
# --------------------------------------------------------------------------------------


def test_split_one_marker() -> None:
    markers, rest = split_markers("(a) Some text here.")
    assert [m.token for m in markers] == ["a"]
    assert rest.strip() == "Some text here."


def test_split_a_marker_with_a_run_in_heading() -> None:
    markers, rest = split_markers("(a) \x01In general.\x02 Some text here.")
    assert [m.token for m in markers] == ["a"]
    assert markers[0].heading == "In general"
    assert rest.strip() == "Some text here."


def test_split_two_markers_in_one_paragraph() -> None:
    markers, rest = split_markers("(b) \x01Cross references\x02—(1) \x01In general.\x02 Body text.")
    assert [m.token for m in markers] == ["b", "1"]
    assert markers[0].heading == "Cross references"
    assert rest.strip() == "Body text."


def test_split_three_markers_in_one_paragraph() -> None:
    markers, _rest = split_markers("(x) (1) (i) Body text.")
    assert [m.token for m in markers] == ["x", "1", "i"]


def test_split_an_italic_marker() -> None:
    markers, _rest = split_markers("(\x011\x02) A letter of credit;")
    assert markers[0].token == "1"
    assert markers[0].italic


def test_a_parenthetical_is_not_a_marker() -> None:
    markers, _rest = split_markers("An amount paid (see paragraph (c) of this section);")
    assert markers == []


# --------------------------------------------------------------------------------------
# The synthetic fixture exercises every level
# --------------------------------------------------------------------------------------


def test_lettered_run_stays_flat(synthetic: dict[str, Provision]) -> None:
    for letter in "hij":
        assert f"/us/cfr/t26/s1.9999-1/{letter}" in synthetic


def test_romans_nest_under_numbers(synthetic: dict[str, Provision]) -> None:
    assert "/us/cfr/t26/s1.9999-1/k/2/i" in synthetic
    assert "/us/cfr/t26/s1.9999-1/k/2/ii" in synthetic


def test_v_after_iv_stays_roman(synthetic: dict[str, Provision]) -> None:
    assert "/us/cfr/t26/s1.9999-1/k/2/v" in synthetic


def test_v_after_u_stays_lettered(synthetic: dict[str, Provision]) -> None:
    assert "/us/cfr/t26/s1.9999-1/v" in synthetic
    assert synthetic["/us/cfr/t26/s1.9999-1/v"].level == "paragraph"


def test_six_levels_deep(synthetic: dict[str, Provision]) -> None:
    deepest = "/us/cfr/t26/s1.9999-1/k/2/v/B/2/ii"
    assert deepest in synthetic
    assert synthetic[deepest].level == "subitem"


def test_paragraph_with_two_markers(synthetic: dict[str, Provision]) -> None:
    assert "/us/cfr/t26/s1.9999-1/w/1" in synthetic
    assert synthetic["/us/cfr/t26/s1.9999-1/w"].heading == "Lorem w"


def test_paragraph_with_three_markers(synthetic: dict[str, Provision]) -> None:
    assert "/us/cfr/t26/s1.9999-1/x/1/i" in synthetic


def test_unmarked_paragraph_joins_the_open_one(synthetic: dict[str, Provision]) -> None:
    assert "no marker at all" in synthetic["/us/cfr/t26/s1.9999-1/x/1/i"].text


def test_editorial_citations_never_reach_provision_text(
    synthetic: dict[str, Provision],
) -> None:
    for provision in synthetic.values():
        assert "T.D. 0000" not in provision.full_text


# --------------------------------------------------------------------------------------
# Real sections
# --------------------------------------------------------------------------------------


def test_section_heading_drops_the_number() -> None:
    provisions = _provisions("1.162-1.xml")
    assert provisions["/us/cfr/t26/s1.162-1"].heading == "Business expenses."


def test_real_run_in_heading_is_captured() -> None:
    provisions = _provisions("1.162-1.xml")
    assert provisions["/us/cfr/t26/s1.162-1/a"].heading == "In general"
    assert provisions["/us/cfr/t26/s1.162-1/b"].heading == "Cross references"


def test_ordinary_and_necessary_expenditures_text() -> None:
    provisions = _provisions("1.162-1.xml")
    assert provisions["/us/cfr/t26/s1.162-1/a"].text.startswith(
        "Business expenses deductible from gross income include the ordinary and "
        "necessary expenditures"
    )


def test_section_number_with_parentheses() -> None:
    provisions = _provisions("1.263a-4.xml")
    assert "/us/cfr/t26/s1.263(a)-4" in provisions
    assert provisions["/us/cfr/t26/s1.263(a)-4"].section == "1.263(a)-4"


def test_real_italic_levels_nest_correctly() -> None:
    provisions = _provisions("1.263a-4.xml")
    assert "/us/cfr/t26/s1.263(a)-4/d/2/i/C/1" in provisions
    assert provisions["/us/cfr/t26/s1.263(a)-4/d/2/i/C/1"].text == "A letter of credit;"


def test_deep_nesting_in_the_entity_classification_rules() -> None:
    provisions = _provisions("301.7701-3.xml")
    deep = provisions["/us/cfr/t26/s301.7701-3/b/2/i/A"]
    assert deep.path == ["b", "2", "i", "A"]
    assert deep.text.startswith("A partnership if it has two or more members")


def test_temporary_regulation_parses() -> None:
    provisions = _provisions("1.469-5T.xml")
    assert provisions["/us/cfr/t26/s1.469-5T/a"].text.startswith("Except as provided")


def test_full_text_gathers_descendants() -> None:
    provisions = _provisions("1.162-1.xml")
    section = provisions["/us/cfr/t26/s1.162-1"]
    assert "Business expenses deductible" in section.full_text
    assert "For charitable contributions" in section.full_text


def test_every_provision_is_a_regulation() -> None:
    for name in ("1.61-1.xml", "1.162-1.xml", "301.7701-3.xml"):
        for provision in _provisions(name).values():
            assert provision.source is SourceType.REG
            assert provision.source_version == FIXTURE_ECFR_DATE


def test_every_fixture_parses() -> None:
    for path in sorted(ECFR_DIR.glob("*.xml")):
        parsed = list(parse_sections(path.read_bytes(), FIXTURE_ECFR_DATE))
        assert parsed, path.name
        assert all(section.provisions for section in parsed)


def test_garbage_is_a_typed_error() -> None:
    with pytest.raises(SourceParseError):
        list(parse_sections(b"", FIXTURE_ECFR_DATE))


@pytest.mark.parametrize(
    ("section", "part"),
    [("1.162-1", "1"), ("301.7701-3", "301"), ("31.3121(a)-1", "31")],
)
def test_part_of(section: str, part: str) -> None:
    assert part_of(section) == part


def test_fixtures_directory_documents_the_ecfr_sources() -> None:
    sources = (ECFR_DIR.parent / "SOURCES.md").read_text(encoding="utf-8")
    assert "ecfr.gov" in sources
    assert FIXTURE_ECFR_DATE in sources
