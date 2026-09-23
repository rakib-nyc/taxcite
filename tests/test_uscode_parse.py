"""Tests for the USLM parser against real Title 26 excerpts (SPEC 4.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxcite.models import Provision
from taxcite.sources.uscode import (
    SUBDIVISION_TAGS,
    iter_sections,
    normalize_identifier,
    ordinal_of,
)

from conftest import FIXTURE_RELEASE_POINT, USLM_DIR


def _provisions(name: str) -> dict[str, Provision]:
    parsed = list(iter_sections(USLM_DIR / name, FIXTURE_RELEASE_POINT))
    return {p.id: p for section in parsed for p in section.provisions}


@pytest.fixture(scope="module")
def s162() -> dict[str, Provision]:
    return _provisions("s162.xml")


@pytest.fixture(scope="module")
def s7701() -> dict[str, Provision]:
    return _provisions("s7701.xml")


@pytest.fixture(scope="module")
def synthetic() -> dict[str, Provision]:
    return _provisions("synthetic_levels.xml")


def test_section_and_subdivisions_are_emitted(s162: dict[str, Provision]) -> None:
    assert "/us/usc/t26/s162" in s162
    assert "/us/usc/t26/s162/a" in s162
    assert "/us/usc/t26/s162/a/1" in s162


def test_section_heading(s162: dict[str, Provision]) -> None:
    assert s162["/us/usc/t26/s162"].heading == "Trade or business expenses"


def test_ordinary_and_necessary_text(s162: dict[str, Provision]) -> None:
    text = s162["/us/usc/t26/s162/a"].text
    assert text.startswith(
        "There shall be allowed as a deduction all the ordinary and necessary expenses "
        "paid or incurred during the taxable year in carrying on any trade or business"
    )


def test_own_text_excludes_child_paragraphs(s162: dict[str, Provision]) -> None:
    assert "reasonable allowance for salaries" not in s162["/us/usc/t26/s162/a"].text
    assert "reasonable allowance for salaries" in s162["/us/usc/t26/s162/a"].full_text


def test_full_text_is_in_document_order(s162: dict[str, Provision]) -> None:
    full = s162["/us/usc/t26/s162/a"].full_text
    chapeau = full.index("ordinary and necessary")
    paragraph = full.index("reasonable allowance for salaries")
    continuation = full.index("For purposes of the preceding sentence")
    assert chapeau < paragraph < continuation


def test_deep_pinpoint_7701(s7701: dict[str, Provision]) -> None:
    provision = s7701["/us/usc/t26/s7701/a/30/A"]
    assert provision.level == "subparagraph"
    assert provision.path == ["a", "30", "A"]
    assert provision.text == "a citizen or resident of the United States,"


def test_parent_links(s7701: dict[str, Provision]) -> None:
    assert s7701["/us/usc/t26/s7701/a/30/A"].parent_id == "/us/usc/t26/s7701/a/30"
    assert s7701["/us/usc/t26/s7701/a"].parent_id == "/us/usc/t26/s7701"
    assert s7701["/us/usc/t26/s7701"].parent_id is None


@pytest.mark.parametrize(
    ("name", "identifier", "status"),
    [
        ("s4_repealed.xml", "/us/usc/t26/s4", "repealed"),
        ("s1000_reserved.xml", "/us/usc/t26/s1000", "reserved"),
        ("s2614_omitted.xml", "/us/usc/t26/s2614", "omitted"),
    ],
)
def test_section_status(name: str, identifier: str, status: str) -> None:
    assert _provisions(name)[identifier].status == status


def test_repealed_heading_has_no_stray_bracket() -> None:
    heading = _provisions("s4_repealed.xml")["/us/usc/t26/s4"].heading
    assert heading is not None
    assert heading.startswith("Repealed.")
    assert not heading.endswith("]")


def test_one_element_can_carry_two_sections() -> None:
    provisions = _provisions("s50A_50B_repealed.xml")
    assert set(provisions) == {"/us/usc/t26/s50A", "/us/usc/t26/s50B"}
    assert provisions["/us/usc/t26/s50A"].num == "50A"
    assert all(p.status == "repealed" for p in provisions.values())


def test_en_dash_section_numbers_are_normalised() -> None:
    provisions = _provisions("s1400Z-2.xml")
    assert "/us/usc/t26/s1400Z-2" in provisions
    assert provisions["/us/usc/t26/s1400Z-2"].section == "1400Z-2"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/us/usc/t26/s1400Z–2", "/us/usc/t26/s1400Z-2"),
        ("/us/usc/t26/s162", "/us/usc/t26/s162"),
    ],
)
def test_normalize_identifier(raw: str, expected: str) -> None:
    assert normalize_identifier(raw) == expected


def test_every_level_is_recognised(synthetic: dict[str, Provision]) -> None:
    levels = {p.level for p in synthetic.values()}
    assert levels == {"section", *SUBDIVISION_TAGS[: len(levels) - 1]}
    assert "/us/usc/t26/s9999/a/2/A/i/I/aa/AA" in synthetic


def test_editorial_notes_never_reach_provision_text(synthetic: dict[str, Provision]) -> None:
    for provision in synthetic.values():
        assert "editorial note" not in provision.full_text
        assert "source credit" not in provision.full_text


def test_explicit_refs_are_collected() -> None:
    parsed = list(iter_sections(USLM_DIR / "synthetic_levels.xml", FIXTURE_RELEASE_POINT))
    refs = [ref for section in parsed for ref in section.refs]
    assert any(ref.to_id == "/us/usc/t26/s61" and not ref.external for ref in refs)


def test_ordinals_follow_document_order(s162: dict[str, Provision]) -> None:
    provisions = list(s162.values())
    ordinals = ordinal_of(provisions)
    assert ordinals["/us/usc/t26/s162/a/1"] == 0
    assert ordinals["/us/usc/t26/s162/a/2"] == 1
    assert ordinals["/us/usc/t26/s162/a/3"] == 2


def test_every_fixture_parses() -> None:
    for path in sorted(USLM_DIR.glob("*.xml")):
        parsed = list(iter_sections(path, FIXTURE_RELEASE_POINT))
        assert parsed, f"{path.name} produced no sections"
        for section in parsed:
            assert section.provisions


def test_fixtures_directory_documents_its_sources() -> None:
    sources = (Path(USLM_DIR).parent / "SOURCES.md").read_text(encoding="utf-8")
    assert "uscode.house.gov" in sources
    assert FIXTURE_RELEASE_POINT in sources
