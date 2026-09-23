"""Subdivision level inference for statutory and regulatory pinpoints (SPEC 6.3).

Position in the path, not the shape of the token, determines the level: the first token
after a section number is always a subsection, so ``§ 1(i)`` is subsection "i" and not
clause "i". Tokens whose shape does not fit the level their position implies still get
looked up, but they earn a warning.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from taxcite.citations.roman import is_lower_roman, is_upper_roman

#: IRC subdivision levels, outermost first (SPEC 6.3).
IRC_LEVELS: Final[tuple[str, ...]] = (
    "subsection",
    "paragraph",
    "subparagraph",
    "clause",
    "subclause",
    "item",
    "subitem",
    "subsubitem",
)

#: CFR paragraph levels, outermost first. eCFR calls them all "paragraphs"; TaxCite
#: reuses the statutory names so that depth is legible in reports.
REG_LEVELS: Final[tuple[str, ...]] = (
    "paragraph",
    "subparagraph",
    "clause",
    "subclause",
    "item",
    "subitem",
    "subsubitem",
)

_LOWER_ONE_OR_TWO: Final = re.compile(r"^[a-z]{1,2}$")
_ARABIC: Final = re.compile(r"^\d+[A-Z]?$")
_UPPER_ONE_OR_TWO: Final = re.compile(r"^[A-Z]{1,2}$")
_LOWER_DOUBLE: Final = re.compile(r"^[a-z]{2}$")
_UPPER_DOUBLE: Final = re.compile(r"^[A-Z]{2}$")


def _lower_roman(token: str) -> bool:
    """Return ``True`` for a lowercase roman numeral."""
    return is_lower_roman(token)


def _upper_roman(token: str) -> bool:
    """Return ``True`` for an uppercase roman numeral."""
    return is_upper_roman(token)


Charset = Callable[[str], bool]


def _matcher(pattern: re.Pattern[str]) -> Charset:
    """Wrap a compiled pattern as a charset predicate."""
    return lambda token: bool(pattern.match(token))


#: The shape expected at each IRC depth, outermost first (SPEC 6.3).
IRC_CHARSETS: Final[tuple[Charset, ...]] = (
    _matcher(_LOWER_ONE_OR_TWO),
    _matcher(_ARABIC),
    _matcher(_UPPER_ONE_OR_TWO),
    _lower_roman,
    _upper_roman,
    _matcher(_LOWER_DOUBLE),
    _matcher(_UPPER_DOUBLE),
)

#: The shape expected at each CFR depth. Regulation paragraphs start at ``(a)``, so
#: the sequence is the IRC one without its first row, and it repeats arabic and roman
#: at the italic levels (SPEC 6.4).
REG_CHARSETS: Final[tuple[Charset, ...]] = (
    _matcher(_LOWER_ONE_OR_TWO),
    _matcher(_ARABIC),
    _lower_roman,
    _matcher(_UPPER_ONE_OR_TWO),
    _matcher(_ARABIC),
    _lower_roman,
)


def _fits(depth: int, token: str, *, reg: bool) -> bool:
    """Return ``True`` if ``token`` has the shape expected at ``depth``."""
    charsets = REG_CHARSETS if reg else IRC_CHARSETS
    if depth >= len(charsets):
        return True
    return charsets[depth](token)


def level_at(depth: int, *, reg: bool = False) -> str:
    """Return the level name for a pinpoint token at ``depth`` (0-based).

    Args:
        depth: Zero-based position of the token in the path.
        reg: ``True`` for Treasury Regulation paths, which have no "subsection" level.
    """
    levels = REG_LEVELS if reg else IRC_LEVELS
    if depth < len(levels):
        return levels[depth]
    return levels[-1]


def level_for_path(path: list[str], *, reg: bool = False) -> str:
    """Return the level of the provision identified by ``path`` (``section`` if empty)."""
    if not path:
        return "section"
    return level_at(len(path) - 1, reg=reg)


def validate_path(path: list[str], *, reg: bool = False) -> list[str]:
    """Return a warning for every pinpoint token that does not fit its expected level."""
    warnings: list[str] = []
    for depth, token in enumerate(path):
        if not _fits(depth, token, reg=reg):
            level = level_at(depth, reg=reg)
            warnings.append(f"unexpected token '({token})' at {level} level")
    return warnings
