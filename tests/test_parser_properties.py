"""Property tests: the canonical id must be stable across citation styles (SPEC 12)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from taxcite.citations import extract_citations
from taxcite.citations.normalize import irc_canonical_id, reg_canonical_id

SUBSECTIONS = st.sampled_from(["a", "b", "c", "j", "i", "aa"])
PARAGRAPHS = st.sampled_from(["1", "2", "10", "30"])
SUBPARAGRAPHS = st.sampled_from(["A", "B", "C"])

IRC_SECTIONS = st.sampled_from(
    ["1", "61", "162", "163", "199A", "263A", "280G", "45Q", "1400Z-2", "7701", "6011"]
)
REG_SECTIONS = st.sampled_from(
    ["1.61-1", "1.162-1", "1.263(a)-4", "1.263A-1", "301.7701-3", "1.469-5T", "31.3121(a)-1"]
)

IRC_STYLES = [
    "§ {s}{p}",
    "§{s}{p}",
    "Section {s}{p}",
    "section {s}{p}",
    "Sec. {s}{p}",
    "IRC § {s}{p}",
    "I.R.C. § {s}{p}",
    "IRC Section {s}{p}",
    "Code § {s}{p}",
    "26 U.S.C. § {s}{p}",
    "26 USC {s}{p}",
]

REG_STYLES = [
    "Treas. Reg. § {s}{p}",
    "Treas. Reg. §{s}{p}",
    "Reg. § {s}{p}",
    "Regs. § {s}{p}",
    "Treasury Regulation section {s}{p}",
    "26 C.F.R. § {s}{p}",
    "26 CFR {s}{p}",
    "§ {s}{p}",
]


@st.composite
def _path(draw: st.DrawFn) -> list[str]:
    depth = draw(st.integers(min_value=0, max_value=3))
    tokens: list[str] = []
    if depth >= 1:
        tokens.append(draw(SUBSECTIONS))
    if depth >= 2:
        tokens.append(draw(PARAGRAPHS))
    if depth >= 3:
        tokens.append(draw(SUBPARAGRAPHS))
    return tokens


def _render(path: list[str]) -> str:
    return "".join(f"({token})" for token in path)


@settings(max_examples=300, deadline=None)
@given(section=IRC_SECTIONS, path=_path(), style=st.sampled_from(IRC_STYLES))
def test_irc_styles_agree_on_canonical_id(section: str, path: list[str], style: str) -> None:
    text = style.format(s=section, p=_render(path))
    citations = extract_citations(text)
    assert len(citations) == 1
    assert citations[0].canonical_id == irc_canonical_id(section, path)


@settings(max_examples=300, deadline=None)
@given(section=REG_SECTIONS, path=_path(), style=st.sampled_from(REG_STYLES))
def test_reg_styles_agree_on_canonical_id(section: str, path: list[str], style: str) -> None:
    text = style.format(s=section, p=_render(path))
    citations = extract_citations(text)
    assert len(citations) == 1
    assert citations[0].canonical_id == reg_canonical_id(section, path)


@settings(max_examples=200, deadline=None)
@given(section=IRC_SECTIONS, path=_path(), style=st.sampled_from(IRC_STYLES))
def test_citation_span_round_trips(section: str, path: list[str], style: str) -> None:
    body = style.format(s=section, p=_render(path))
    text = f"The rule in {body} applies here."
    citations = extract_citations(text)
    assert len(citations) == 1
    start, end = citations[0].span
    assert text[start:end] == body


@settings(max_examples=200, deadline=None)
@given(
    section=IRC_SECTIONS,
    path=_path(),
    style=st.sampled_from(IRC_STYLES),
    filler=st.text(alphabet="abc ", min_size=0, max_size=10),
)
def test_citations_survive_surrounding_prose(
    section: str, path: list[str], style: str, filler: str
) -> None:
    text = f"{filler} {style.format(s=section, p=_render(path))} {filler}"
    citations = extract_citations(text)
    assert len(citations) == 1
    assert citations[0].canonical_id == irc_canonical_id(section, path)
