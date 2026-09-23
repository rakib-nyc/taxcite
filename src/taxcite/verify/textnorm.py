"""Text normalisation for quotation comparison (SPEC 6.7).

Two passages must be compared without tripping over typography: a memo written in a
word processor has curly quotes and en dashes where the USLM has straight quotes and
hyphens, and a copied passage may carry soft hyphens or zero-width characters.

Normalisation keeps punctuation, because punctuation carries meaning in statutory text,
and offers a ``loose`` variant that drops it for scoring only.

Every normalised string carries a map back to the source offsets, so a match found in
normalised space can be reported as the text the source actually says, in its original
case and spelling.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

#: Characters that are typographic variants of something plainer.
_FOLD: Final[dict[str, str]] = {
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "′": "'",
    "″": '"',
    "–": "-",
    "—": "-",
    "―": "-",
    "‒": "-",
    "−": "-",
    "‐": "-",
    "‑": "-",
    "…": "...",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
}

#: Characters that carry no meaning and must simply disappear.
_DROP: Final[frozenset[str]] = frozenset("­​‌‍﻿⁠")

_WS_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s]+")
_WORD_RE: Final = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class Normalized:
    """A normalised string with a map back into the text it came from."""

    text: str
    offsets: tuple[int, ...]
    source: str

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """Return the span in the source text covering normalised ``[start, end)``."""
        if not self.offsets or start >= len(self.offsets):
            return (len(self.source), len(self.source))
        first = self.offsets[start]
        last = self.offsets[min(end, len(self.offsets)) - 1]
        return (first, last + 1)

    def source_slice(self, start: int, end: int) -> str:
        """Return the source text underlying normalised ``[start, end)``."""
        low, high = self.source_span(start, end)
        return self.source[low:high].strip()

    def __len__(self) -> int:
        """Return the length of the normalised text."""
        return len(self.text)


def _fold_char(char: str) -> str:
    """Fold one character to its plain, casefolded equivalent."""
    if char in _DROP:
        return ""
    replacement = _FOLD.get(char)
    if replacement is not None:
        return replacement
    decomposed = unicodedata.normalize("NFKC", char)
    return decomposed.casefold()


def normalize_mapped(text: str) -> Normalized:
    """Normalise ``text`` and keep a map from each output character to its source index.

    NFKC, typographic folding, removal of invisible characters, whitespace collapse,
    and casefolding, in that order. Punctuation is preserved.
    """
    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        if char.isspace() or char in {" ", " ", " ", " "}:
            if chars:
                pending_space = True
            continue
        folded = _fold_char(char)
        if not folded:
            continue
        if pending_space:
            chars.append(" ")
            offsets.append(index)
            pending_space = False
        for piece in folded:
            chars.append(piece)
            offsets.append(index)
    return Normalized(text="".join(chars), offsets=tuple(offsets), source=text)


def normalize(text: str) -> str:
    """Return the normalised form of ``text``."""
    return normalize_mapped(text).text


def loose(text: str) -> str:
    """Return the normalised form with punctuation stripped, for scoring only."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", normalize(text))).strip()


def words(text: str) -> list[str]:
    """Split ``text`` into whitespace-separated words."""
    return _WORD_RE.findall(text)


def word_count(text: str) -> int:
    """Count the words in ``text``."""
    return len(_WORD_RE.findall(text))
