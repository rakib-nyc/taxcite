"""Tests for subdivision level inference and roman numeral helpers (SPEC 6.3)."""

from __future__ import annotations

import pytest

from taxcite.citations.levels import level_at, level_for_path, validate_path
from taxcite.citations.roman import int_to_roman, is_lower_roman, is_roman, roman_to_int


@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        (0, "subsection"),
        (1, "paragraph"),
        (2, "subparagraph"),
        (3, "clause"),
        (4, "subclause"),
        (5, "item"),
        (6, "subitem"),
        (7, "subsubitem"),
        (12, "subsubitem"),
    ],
)
def test_irc_level_at(depth: int, expected: str) -> None:
    assert level_at(depth) == expected


@pytest.mark.parametrize(
    ("depth", "expected"),
    [(0, "paragraph"), (1, "subparagraph"), (2, "clause"), (3, "subclause")],
)
def test_reg_level_at(depth: int, expected: str) -> None:
    assert level_at(depth, reg=True) == expected


def test_level_for_empty_path_is_section() -> None:
    assert level_for_path([]) == "section"


@pytest.mark.parametrize(
    "path",
    [
        ["a"],
        ["a", "1"],
        ["a", "30", "A"],
        ["a", "1", "A", "i"],
        ["a", "1", "A", "i", "I"],
        ["j", "1", "B", "ii", "II", "aa"],
        ["i"],
        ["aa"],
    ],
)
def test_valid_paths_produce_no_warnings(path: list[str]) -> None:
    assert validate_path(path) == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (["3"], ["unexpected token '(3)' at subsection level"]),
        (["a", "b"], ["unexpected token '(b)' at paragraph level"]),
        (["a", "1", "1"], ["unexpected token '(1)' at subparagraph level"]),
    ],
)
def test_invalid_tokens_produce_warnings(path: list[str], expected: list[str]) -> None:
    assert validate_path(path) == expected


def test_first_token_is_always_a_subsection() -> None:
    """``§ 1(i)`` is subsection "i", not clause "i" (SPEC 6.3)."""
    assert validate_path(["i"]) == []
    assert level_for_path(["i"]) == "subsection"


@pytest.mark.parametrize("token", ["i", "ii", "iv", "ix", "xiv", "I", "IV", "MCMLXXXIV"])
def test_is_roman(token: str) -> None:
    assert is_roman(token)


@pytest.mark.parametrize("token", ["", "iiii", "abc", "vx", "a"])
def test_is_not_roman(token: str) -> None:
    assert not is_roman(token)


def test_is_lower_roman_is_case_sensitive() -> None:
    assert is_lower_roman("iv")
    assert not is_lower_roman("IV")


@pytest.mark.parametrize(("token", "value"), [("i", 1), ("iv", 4), ("ix", 9), ("xl", 40)])
def test_roman_to_int(token: str, value: int) -> None:
    assert roman_to_int(token) == value


@pytest.mark.parametrize("value", [1, 4, 9, 14, 40, 99, 1984])
def test_roman_round_trip(value: int) -> None:
    assert roman_to_int(int_to_roman(value)) == value


def test_roman_to_int_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a roman numeral"):
        roman_to_int("iiii")


def test_int_to_roman_rejects_zero() -> None:
    with pytest.raises(ValueError, match="positive"):
        int_to_roman(0)
