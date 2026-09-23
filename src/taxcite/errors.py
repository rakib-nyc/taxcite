"""Typed exceptions raised inside TaxCite.

Internal code raises these; the CLI and MCP server catch them and render friendly
messages. No traceback ever reaches a user.
"""

from __future__ import annotations


class TaxCiteError(Exception):
    """Base class for every error raised by TaxCite."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Store the message and an optional actionable hint."""
        super().__init__(message)
        self.message = message
        self.hint = hint

    def friendly(self) -> str:
        """Return a user-facing one-or-two-line description of the error."""
        return f"{self.message}\n{self.hint}" if self.hint else self.message


class MalformedCitationError(TaxCiteError):
    """The input could not be parsed as a tax citation."""


class IndexNotBuiltError(TaxCiteError):
    """The SQLite index is missing or empty."""

    def __init__(self, message: str = "Index not built.") -> None:
        """Create the error with the standard build hint."""
        super().__init__(message, hint="Run: taxcite build-index")


class ProvisionNotFoundError(TaxCiteError):
    """A provision was requested that does not exist in the index."""


class SourceUnavailableError(TaxCiteError):
    """An official source could not be reached or is not cached locally."""


class SourceNotFoundError(SourceUnavailableError):
    """An official source answered, and said the thing asked for does not exist.

    Distinct from :class:`SourceUnavailableError`, which means the source could not be
    reached. Telling a user their citation is fabricated because a server was down
    would be worse than saying nothing.
    """


class SourceParseError(TaxCiteError):
    """Official source data was retrieved but could not be parsed."""


class OfflineError(SourceUnavailableError):
    """A network fetch was required but TaxCite is running in offline mode."""


class ConfigurationError(TaxCiteError):
    """TaxCite is misconfigured (bad data dir, missing dependency, bad flag)."""
