r"""Compiled regular expressions for every supported citation form (SPEC 6.1).

The grammar is assembled from small named fragments so that ``docs/citation-grammar.md``
can describe each piece and the tests can exercise them individually. Three families of
pattern are exported:

``SECTION_CITE_RE``
    IRC and Treasury Regulation citations, including ``§§`` lists and ranges.
``GUIDANCE_RE``
    IRS sub-regulatory guidance (Revenue Rulings, Notices, and friends), recognised but
    not verifiable in v1.
``CASE_RE``
    Court citations, recognised so that reporter numbers are never mistaken for Code
    sections.

Every pattern is anchored with ``\b`` where a word boundary is meaningful and none of
them consume trailing sentence punctuation.
"""

from __future__ import annotations

import re
from typing import Final

# --------------------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------------------

#: An IRC section number: ``162``, ``199A``, ``280G``, ``45Q``, ``1400Z-2``.
#: A hyphenated suffix is only allowed after letters, so that ``1401-1403`` reads as a
#: range of two sections rather than one impossible section.
IRC_SECTION_NUM: Final = r"\d+[A-Za-z]{1,3}-\d+[A-Za-z]?|\d+[A-Za-z]{0,3}"

#: A Treasury Regulation section number: ``1.162-1``, ``1.263(a)-4``, ``301.7701-3``,
#: ``1.469-5T``, ``31.3121(a)-1``, ``1.1400Z2(a)-1``.
REG_SECTION_NUM: Final = r"\d+[A-Za-z]?\.[0-9A-Za-z]+(?:\([a-z]\))?-\d+[A-Za-z]{0,2}"

#: One pinpoint token: one to six alphanumerics in parentheses. The length cap is what
#: stops ``§ 162(a) (emphasis added)`` from swallowing the parenthetical.
PINPOINT_TOKEN: Final = r"\([0-9A-Za-z]{1,6}\)"

#: A pinpoint path: ``(a)(1)(A)(i)(I)``, optionally with single spaces between tokens.
PINPOINT_PATH: Final = rf"(?:\s?{PINPOINT_TOKEN})*"

#: The section symbol or the word "section" in its usual abbreviations.
SECTION_MARK: Final = r"§§|§|[Ss]ections\b|[Ss]ection\b|[Ss]ecs\.|[Ss]ec\.|[Ss]s\."

#: Separators inside a list of sections: ``, ``, `` and ``, ``, and ``, `` or ``.
LIST_SEP: Final = r"\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+"

#: Separators inside a range of sections: an en/em dash, a hyphen, or "through"/"to".
RANGE_SEP: Final = r"\s*(?:–|—|-)\s*|\s+(?:through|to)\s+"

#: One element of a citation body: a section number with an optional pinpoint path, or a
#: bare pinpoint (``§ 162(a) and (b)``). The parser rejects bare pinpoints that do not
#: follow a section carrying its own pinpoint.
_ELEMENT: Final = (
    rf"(?:{REG_SECTION_NUM}|{IRC_SECTION_NUM}){PINPOINT_PATH}|{PINPOINT_TOKEN}{PINPOINT_PATH}"
)

#: A citation body: one element, then any number of separated further elements.
CITE_BODY: Final = rf"(?:{_ELEMENT})(?:(?:{LIST_SEP}|{RANGE_SEP})(?:{_ELEMENT}))*"

#: Authority prefixes. ``reg`` covers Treas. Reg./Reg./Regs./Treasury Regulation with the
#: optional Temp./Prop. qualifiers; ``cfr`` and ``usc`` cover the positive-law forms.
_AUTHORITY: Final = (
    r"(?P<usc>\b26\s+U\.?\s?S\.?\s?C\.?(?:A\.?)?)"
    r"|(?P<cfr>\b26\s+C\.?\s?F\.?\s?R\.?)"
    r"|(?P<reg>(?P<regqual>(?:\b(?:Temp|Temporary|Prop|Proposed)\.?\s+)+)?"
    r"(?:\bTreas(?:ury)?\.?\s+)?\bReg(?:ulation)?s?\.?)"
    r"|(?P<irc>\bI\.?R\.?C\.?|\bInternal\s+Revenue\s+Code|\bCode\b)"
)

SECTION_CITE_RE: Final = re.compile(
    rf"(?:(?P<auth>{_AUTHORITY})\s*(?:(?P<mark1>{SECTION_MARK})\s*)?"
    rf"|(?P<mark2>{SECTION_MARK})\s*)"
    rf"(?P<body>{CITE_BODY})"
)

# --------------------------------------------------------------------------------------
# IRS sub-regulatory guidance
# --------------------------------------------------------------------------------------

GUIDANCE_RE: Final = re.compile(
    r"(?:(?P<gtype>"
    r"\bRev\.?\s*Rul\.?|\bRevenue\s+Ruling"
    r"|\bRev\.?\s*Proc\.?|\bRevenue\s+Procedure"
    r"|\bNotice|\bAnnouncement|\bAnn\."
    r"|\bPLR|\bPriv\.?\s*Ltr\.?\s*Rul\.?|\bTAM"
    r")\s*(?P<gnum>\d{4}[-\u2013\u2014]\d{1,3}\b|\d{2}[-\u2013\u2014]\d{1,3}\b|\d{6,12}\b))"
    # Internal memoranda are numbered sequentially rather than by year-and-index,
    # so they need their own, looser number shape.
    r"|(?:(?P<mtype>\bCCA|\bGCM|\bFSA|\bAOD|\bILM)\s*"
    r"(?P<mnum>\d{4}[-\u2013\u2014]\d{1,3}\b|\d{4,12}\b))"
    r"|(?:(?P<tdtype>\bT\.\s?D\.?|\bTreasury\s+Decision)\s*(?P<tdnum>\d{4,5}\b))"
)

IRB_RE: Final = re.compile(r"\b(?P<vol>\d{4}-\d{1,2})\s+I\.?\s?R\.?\s?B\.?\s+(?P<page>\d+)\b")

# --------------------------------------------------------------------------------------
# Court decisions
# --------------------------------------------------------------------------------------

_REPORTER: Final = (
    r"T\.\s?C\.\s?(?:Memo\.?|Summary\s+Opinion)"
    # "155 T.C. No. 8" is how a regular Tax Court opinion is cited before the bound
    # volume assigns it a page; "68 T.C.M. 89" is the commercial memorandum reporter.
    # Both are everyday tax citations, and without them the parser walked straight past.
    r"|T\.\s?C\.\s?No\.|T\.\s?C\.\s?M\.(?:\s?2d)?"
    r"|T\.\s?C\.|B\.\s?T\.\s?A\."
    r"|F\.\s?(?:2d|3d|4th)|F\.\s?Supp\.(?:\s?\d[a-z]{0,2})?"
    r"|U\.\s?S\.|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?2d)?"
    r"|Fed\.\s?Cl\.|Cl\.\s?Ct\.|A\.\s?F\.\s?T\.\s?R\.(?:\s?2d)?"
    r"|U\.\s?S\.\s?T\.\s?C\."
)

#: Introductory signals are capitalised and look exactly like the first word of a
#: party name, so "See Welch v. Helvering" would otherwise be read as a case called
#: "See Welch". "In" is deliberately absent: "In re" is a real case name.
_SIGNAL: Final = (
    r"See|Compare|Accord|Cf|But|Contra|Citing|Quoting|Following|Also"
    r"|Versus|Per|Under|Applying|Distinguishing|Overruling|Quoted|Cited"
)

_CASE_NAME: Final = (
    rf"(?!(?:{_SIGNAL})\b)"
    r"(?:[A-Z][\w&.'’-]*\.?(?:\s+(?:[A-Z][\w&.'’-]*\.?|of|the|and|for|de|van|von))*)"
    r"\s+v\.?\s+"
    r"(?:[A-Z][\w&.'’-]*\.?(?:\s+(?:[A-Z][\w&.'’-]*\.?|of|the|and|for|de|van|von))*)"
)

CASE_RE: Final = re.compile(
    rf"(?:(?P<case_name>{_CASE_NAME}),?\s+)?"
    rf"(?P<reporter_cite>\d+\s+(?:{_REPORTER})\s+\d+"
    rf"|T\.\s?C\.\s?Memo\.?\s+\d{{4}}-\d+)"
    # A pinpoint page: "290 U.S. 111, 115". Kept separate from the reporter citation,
    # because the citation can be right while the page is wrong, and a reader chasing
    # the wrong page concludes the passage is not there at all. The lookahead refuses a
    # parallel citation — in "290 U.S. 111, 54 S. Ct. 8" the 54 is a volume, not a page.
    rf"(?:,\s*(?P<case_pin>\d{{1,5}})(?:\s*[-\u2013]\s*\d{{1,5}})?"
    rf"(?!\d)(?!\.\d)(?!\s*(?:{_REPORTER})))?"
    rf"(?:\s*\((?:[A-Za-z0-9.\s]{{1,20}}\s)?(?P<case_year>\d{{4}})\))?"
)

# --------------------------------------------------------------------------------------
# Secondary sources
# --------------------------------------------------------------------------------------
#
# Treatises, law reviews and practitioner commentary are recognised for one reason:
# Treas. Reg. § 1.6662-4(d)(3)(iii) says in terms that conclusions reached in them are
# **not** authority for the substantial-authority standard. A memo whose support rests
# on a treatise has a penalty-protection problem, and that is invisible in prose.

#: Law review and journal citations: "85 Tax L. Rev. 123", "72 Va. L. Rev. 1".
LAW_REVIEW_RE: Final = re.compile(
    r"\b\d{1,3}\s+"
    r"(?:[A-Z][A-Za-z&.']{0,14}\.?\s+){0,4}"
    r"(?:L\.\s?(?:Rev|J)\.|J\.\s?(?:Tax'?n|Corp\. Tax'?n)|Tax\s?L\.\s?Rev\."
    r"|Tax\s?Law\.|Tax\s?Lawyer|L\.\s?Q\.|Rev\.)"
    r"\s+\d{1,4}\b"
)

#: Treatises, cited by the author names the profession uses for them.
TREATISE_RE: Final = re.compile(
    r"\b(?:Mertens|Bittker(?:\s*(?:&|and)\s*Eustice)?|Eustice|Saltzman"
    r"|Rabkin(?:\s*(?:&|and)\s*Johnson)?|Cavitch|McKee(?:\s*(?:&|and)\s*Nelson)?"
    r"|Nelson\s*(?:&|and)\s*Whitmire)\b"
    r"[^.;\n]{0,80}?"
    r"(?:\u00b6|\u00a7|\bch\.|\bvol\.|\bpara\.)\s?[0-9][0-9A-Za-z.\-]*"
)

#: Practitioner material that names its own genre.
COMMENTARY_RE: Final = re.compile(
    r"\b(?:Tax\s+Management\s+Portfolio|BNA\s+Portfolio|Portfolio\s+No\.\s?\d+"
    r"|(?:practice|practitioner'?s?)\s+guide|treatise|law\s+review\s+article)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------------------
# Masking and false-positive guards
# --------------------------------------------------------------------------------------

#: Spans that must never yield citations: fenced code, inline code, and URLs.
FENCED_CODE_RE: Final = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
INLINE_CODE_RE: Final = re.compile(r"`[^`\n]*`")
URL_RE: Final = re.compile(r"(?:https?://|www\.|ftp://)[^\s<>\"')\]]+")
MARKDOWN_LINK_TARGET_RE: Final = re.compile(r"\]\([^)\s]+\)")

#: Phrases that mark a "section" reference as belonging to some other document.
NON_TAX_BEFORE_RE: Final = re.compile(
    r"(?:Pub\.?\s?L\.?|P\.?L\.?)\s*(?:No\.?\s*)?\d*[-–]?\d*\s*(?:,\s*)?$",
    re.IGNORECASE,
)
_INSTRUMENT: Final = (
    r"Act|Agreement|Plan|Contract|Lease|Law|Rules|Regulations|Protocol|Treaty|Convention"
    r"|Constitution|Charter|Bylaws|Indenture|Note|Order|Statute"
)

NON_TAX_AFTER_RE: Final = re.compile(
    rf"^\s*of\s+(?:the|this|that|such)\s+"
    rf"(?:(?:{_INSTRUMENT})\b"
    rf"|(?:[A-Z][\w-]*(?:\s+(?:[A-Z][\w-]*|and|of|for|to|on))*\s+)(?:{_INSTRUMENT})\b)",
    re.MULTILINE,
)

__all__ = [
    "CASE_RE",
    "CITE_BODY",
    "COMMENTARY_RE",
    "FENCED_CODE_RE",
    "GUIDANCE_RE",
    "INLINE_CODE_RE",
    "IRB_RE",
    "IRC_SECTION_NUM",
    "LAW_REVIEW_RE",
    "LIST_SEP",
    "MARKDOWN_LINK_TARGET_RE",
    "NON_TAX_AFTER_RE",
    "NON_TAX_BEFORE_RE",
    "PINPOINT_PATH",
    "PINPOINT_TOKEN",
    "RANGE_SEP",
    "REG_SECTION_NUM",
    "SECTION_CITE_RE",
    "SECTION_MARK",
    "TREATISE_RE",
    "URL_RE",
]
