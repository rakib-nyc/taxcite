"""TaxCite: retrieve U.S. federal tax law and verify citations in AI-drafted writing.

TaxCite is a research tool. It checks that cited provisions exist and that quoted
language matches the official source; it does not judge whether a legal conclusion is
correct, and it is not tax advice.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "extract_citations",
    "lookup",
    "parse_citation",
    "search",
    "verify_text",
]


def __getattr__(name: str) -> object:
    """Lazily expose the public API without importing heavy modules at import time."""
    if name in {"extract_citations", "parse_citation"}:
        from taxcite import citations

        return getattr(citations, name)
    if name in {"lookup", "search"}:
        from taxcite import api

        return getattr(api, name)
    if name == "verify_text":
        from taxcite.verify import core

        return core.verify_text
    raise AttributeError(f"module 'taxcite' has no attribute {name!r}")
