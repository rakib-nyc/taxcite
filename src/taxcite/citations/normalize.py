"""Canonical identifiers, display strings, and source URLs (SPEC 6.3).

IRC identifiers are exactly the USLM ``identifier`` attribute, so a citation's canonical
id can be handed straight to the index. Regulation identifiers follow the same shape
under ``/us/cfr/t26``.
"""

from __future__ import annotations

from typing import Final

from taxcite.config import ECFR_VIEW_URL, USCODE_VIEW_URL
from taxcite.models import RegKind, SourceType

IRC_ID_PREFIX: Final = "/us/usc/t26/s"
REG_ID_PREFIX: Final = "/us/cfr/t26/s"


def irc_canonical_id(section: str, path: list[str] | None = None) -> str:
    """Return the USLM identifier for an IRC section and pinpoint path."""
    return "/".join([f"{IRC_ID_PREFIX}{section}", *(path or [])])


def reg_canonical_id(section: str, path: list[str] | None = None) -> str:
    """Return the canonical identifier for a Treasury Regulation provision."""
    return "/".join([f"{REG_ID_PREFIX}{section}", *(path or [])])


def canonical_id(source: SourceType, section: str, path: list[str] | None = None) -> str:
    """Return the canonical identifier for ``section`` under ``source``."""
    if source is SourceType.REG:
        return reg_canonical_id(section, path)
    return irc_canonical_id(section, path)


def split_canonical_id(identifier: str) -> tuple[SourceType, str, list[str]]:
    """Split a canonical identifier back into source, section, and pinpoint path.

    Raises:
        ValueError: if ``identifier`` is not a TaxCite canonical id.
    """
    if identifier.startswith(IRC_ID_PREFIX):
        source = SourceType.IRC
        rest = identifier[len(IRC_ID_PREFIX) :]
    elif identifier.startswith(REG_ID_PREFIX):
        source = SourceType.REG
        rest = identifier[len(REG_ID_PREFIX) :]
    else:
        raise ValueError(f"not a TaxCite canonical id: {identifier!r}")
    parts = [p for p in rest.split("/") if p]
    if not parts:
        raise ValueError(f"canonical id has no section: {identifier!r}")
    return source, parts[0], parts[1:]


def section_id(identifier: str) -> str:
    """Return the section-level canonical id that contains ``identifier``."""
    source, section, _ = split_canonical_id(identifier)
    return canonical_id(source, section)


def parent_id(identifier: str) -> str | None:
    """Return the canonical id of the parent provision, or ``None`` for a section."""
    source, section, path = split_canonical_id(identifier)
    if not path:
        return None
    return canonical_id(source, section, path[:-1])


def _pinpoint(path: list[str] | None) -> str:
    """Render a pinpoint path as ``(a)(1)(A)``."""
    return "".join(f"({token})" for token in (path or []))


def irc_display(section: str, path: list[str] | None = None) -> str:
    """Return the standard display form of an IRC citation."""
    return f"I.R.C. § {section}{_pinpoint(path)}"


def reg_display(
    section: str,
    path: list[str] | None = None,
    reg_kind: RegKind | None = None,
) -> str:
    """Return the standard display form of a Treasury Regulation citation."""
    match reg_kind:
        case RegKind.TEMPORARY:
            prefix = "Temp. Treas. Reg."
        case RegKind.PROPOSED:
            prefix = "Prop. Treas. Reg."
        case _:
            prefix = "Treas. Reg."
    return f"{prefix} § {section}{_pinpoint(path)}"


def display_for(
    source: SourceType,
    section: str,
    path: list[str] | None = None,
    reg_kind: RegKind | None = None,
) -> str:
    """Return the display form appropriate to ``source``."""
    if source is SourceType.REG:
        return reg_display(section, path, reg_kind)
    return irc_display(section, path)


def irc_source_url(section: str) -> str:
    """Return the official uscode.house.gov URL for an IRC section."""
    return USCODE_VIEW_URL.format(section=section)


def reg_source_url(section: str, path: list[str] | None = None) -> str:
    """Return the official eCFR URL for a regulation section, anchored to a pinpoint."""
    url = ECFR_VIEW_URL.format(section=section)
    if path:
        url = f"{url}#p-{section}{_pinpoint(path)}"
    return url


def source_url_for(
    source: SourceType, section: str | None, path: list[str] | None = None
) -> str | None:
    """Return the official source URL for a citation, if one can be built."""
    if not section:
        return None
    if source is SourceType.REG:
        return reg_source_url(section, path)
    if source is SourceType.IRC:
        return irc_source_url(section)
    return None
