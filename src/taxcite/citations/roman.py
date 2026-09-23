"""Roman numeral helpers used to classify subdivision tokens.

Statutory subdivisions use roman numerals at the clause and subclause levels, so the
parser has to tell ``(i)`` the roman numeral one from ``(i)`` the ninth letter of the
alphabet. These helpers only answer "is this a well-formed roman numeral" and convert
between numerals and integers; the disambiguation itself lives in :mod:`levels`.
"""

from __future__ import annotations

import re
from typing import Final

_ROMAN_RE: Final = re.compile(
    r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$",
    re.IGNORECASE,
)

_VALUES: Final[tuple[tuple[int, str], ...]] = (
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
)

_DIGITS: Final[dict[str, int]] = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def is_roman(token: str) -> bool:
    """Return ``True`` if ``token`` is a well-formed roman numeral (either case)."""
    return bool(token) and bool(_ROMAN_RE.match(token))


def is_lower_roman(token: str) -> bool:
    """Return ``True`` if ``token`` is a well-formed lowercase roman numeral."""
    return token.islower() and is_roman(token)


def is_upper_roman(token: str) -> bool:
    """Return ``True`` if ``token`` is a well-formed uppercase roman numeral."""
    return token.isupper() and is_roman(token)


def roman_to_int(token: str) -> int:
    """Convert a roman numeral to an integer.

    Raises:
        ValueError: if ``token`` is not a well-formed roman numeral.
    """
    if not is_roman(token):
        raise ValueError(f"not a roman numeral: {token!r}")
    lowered = token.lower()
    total = 0
    for index, char in enumerate(lowered):
        value = _DIGITS[char]
        nxt = _DIGITS[lowered[index + 1]] if index + 1 < len(lowered) else 0
        total += -value if value < nxt else value
    return total


def int_to_roman(value: int, *, upper: bool = False) -> str:
    """Convert a positive integer to a roman numeral."""
    if value <= 0:
        raise ValueError(f"roman numerals are positive: {value}")
    out: list[str] = []
    remaining = value
    for amount, numeral in _VALUES:
        while remaining >= amount:
            out.append(numeral)
            remaining -= amount
    text = "".join(out)
    return text.upper() if upper else text
