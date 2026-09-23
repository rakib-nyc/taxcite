"""In-process tests for every MCP tool, resource, and prompt (SPEC 9)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from taxcite import server


@pytest.fixture
def served(fixture_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server's index at a copy of the fixture index."""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(fixture_db_path, data / "taxcite.db")
    monkeypatch.setenv("TAXCITE_DATA_DIR", str(data))
    return data


@pytest.fixture
def no_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the server at an empty data directory."""
    monkeypatch.setenv("TAXCITE_DATA_DIR", str(tmp_path / "empty"))
    yield


async def call(name: str, arguments: dict[str, object]) -> str:
    """Call a tool the way a client would and return its text."""
    result = await server.mcp.call_tool(name, arguments)
    blocks = getattr(result, "content", result)
    return "\n".join(getattr(block, "text", "") for block in blocks)


EXPECTED_TOOLS = {
    "get_irc_provision",
    "get_treasury_reg",
    "search_tax_law",
    "resolve_citation",
    "check_case",
    "model_section_382",
    "reading_list",
    "test_ownership_change",
    "attribute_carryover",
    "verify_citations",
    "get_cross_references",
    "find_definition",
    "list_subdivisions",
}


@pytest.mark.anyio
async def test_every_specified_tool_is_registered() -> None:
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert names >= EXPECTED_TOOLS


@pytest.mark.anyio
async def test_every_tool_documents_itself() -> None:
    for tool in await server.mcp.list_tools():
        assert tool.description, tool.name


@pytest.mark.anyio
async def test_get_irc_provision(served: Path) -> None:
    out = await call("get_irc_provision", {"citation": "§ 162(a)", "include_children": False})
    assert "ordinary and necessary expenses" in out
    assert "/us/usc/t26/s162/a" in out
    assert "uscode.house.gov" in out
    assert "119-110" in out
    assert "not tax advice" in out


@pytest.mark.anyio
async def test_get_irc_provision_lists_subdivisions(served: Path) -> None:
    out = await call("get_irc_provision", {"citation": "§ 162"})
    assert "Subdivisions: (a)" in out


@pytest.mark.anyio
async def test_get_irc_provision_truncates(served: Path) -> None:
    out = await call("get_irc_provision", {"citation": "§ 162", "max_chars": 200})
    assert "[truncated]" in out


@pytest.mark.anyio
async def test_get_irc_provision_flags_a_repealed_section(served: Path) -> None:
    out = await call("get_irc_provision", {"citation": "§ 4"})
    assert "this provision is repealed" in out


@pytest.mark.anyio
async def test_get_irc_provision_redirects_a_regulation(served: Path) -> None:
    out = await call("get_irc_provision", {"citation": "Treas. Reg. § 1.162-1"})
    assert "could not answer" in out or "get_treasury_reg" in out


@pytest.mark.anyio
async def test_get_treasury_reg(served: Path) -> None:
    out = await call("get_treasury_reg", {"citation": "Treas. Reg. § 1.162-1(a)"})
    assert "Business expenses deductible from gross income" in out
    assert "/us/cfr/t26/s1.162-1/a" in out
    assert "ecfr.gov" in out


@pytest.mark.anyio
async def test_get_treasury_reg_that_cannot_be_fetched_is_friendly(served: Path) -> None:
    out = await call("get_treasury_reg", {"citation": "Treas. Reg. § 1.9999-9"})
    assert "TaxCite could not answer that" in out
    assert "Traceback" not in out


@pytest.mark.anyio
async def test_get_treasury_reg_redirects_a_code_section(served: Path) -> None:
    out = await call("get_treasury_reg", {"citation": "§ 162(a)"})
    assert "get_irc_provision" in out


@pytest.mark.anyio
async def test_search_tax_law(served: Path) -> None:
    out = await call("search_tax_law", {"query": "ordinary and necessary", "limit": 3})
    assert "I.R.C. § 162(a)" in out
    assert "id: /us/usc/t26/s162/a" in out


@pytest.mark.anyio
async def test_search_tax_law_with_no_hits(served: Path) -> None:
    out = await call("search_tax_law", {"query": "zzzznotawordzzzz"})
    assert "No provisions matched" in out


@pytest.mark.anyio
async def test_resolve_citation_for_a_bad_pinpoint(served: Path) -> None:
    out = await call("resolve_citation", {"citation": "§ 162(z)"})
    assert "pinpoint_not_found" in out
    assert "subsections (a)–(s)" in out
    assert "/us/usc/t26/s162/z" in out


@pytest.mark.anyio
async def test_resolve_citation_for_a_good_one(served: Path) -> None:
    out = await call("resolve_citation", {"citation": "§ 7701(a)(30)(A)"})
    assert "verified" in out


@pytest.mark.anyio
async def test_verify_citations_returns_markdown_and_json(served: Path) -> None:
    out = await call(
        "verify_citations",
        {"text": "See § 162A and § 162(a)."},
    )
    assert "# TaxCite verification report" in out
    assert "```json" in out
    assert "not_found" in out


@pytest.mark.anyio
async def test_get_cross_references(served: Path) -> None:
    out = await call("get_cross_references", {"citation": "§ 1411"})
    assert "Cites" in out
    assert "Cited by" in out


@pytest.mark.anyio
async def test_get_cross_references_rejects_a_bad_direction(served: Path) -> None:
    out = await call("get_cross_references", {"citation": "§ 1411", "direction": "sideways"})
    assert "direction must be" in out


@pytest.mark.anyio
async def test_find_definition(served: Path) -> None:
    out = await call("find_definition", {"term": "gross income", "limit": 3})
    assert "I.R.C. § 61(a)" in out
    assert "scope" in out


@pytest.mark.anyio
async def test_find_definition_with_no_match(served: Path) -> None:
    out = await call("find_definition", {"term": "zzzznotawordzzzz"})
    assert "No definition" in out


@pytest.mark.anyio
async def test_list_subdivisions(served: Path) -> None:
    out = await call("list_subdivisions", {"citation": "§ 1411"})
    assert "(a) — In general" in out
    assert "(c) — Net investment income" in out


@pytest.mark.anyio
async def test_index_not_built_is_a_friendly_error(no_index: None) -> None:
    out = await call("get_irc_provision", {"citation": "§ 162"})
    assert "taxcite build-index" in out
    assert "Traceback" not in out


@pytest.mark.anyio
async def test_unparseable_citation_is_a_friendly_error(served: Path) -> None:
    out = await call("get_irc_provision", {"citation": "the lunch rule"})
    assert "TaxCite could not answer that" in out


@pytest.mark.anyio
async def test_resources_are_registered() -> None:
    templates = {str(t.uri_template) for t in await server.mcp.list_resource_templates()}
    assert "taxcite://irc/{section}" in templates
    assert "taxcite://reg/{section}" in templates


@pytest.mark.anyio
async def test_irc_resource_returns_text(served: Path) -> None:
    contents = await server.mcp.read_resource("taxcite://irc/162")
    text = "\n".join(str(item.content) for item in contents)
    assert "ordinary and necessary" in text


@pytest.mark.anyio
async def test_prompt_is_registered_and_instructs_verification() -> None:
    prompts = {p.name for p in await server.mcp.list_prompts()}
    assert "grounded_tax_memo" in prompts
    result = await server.mcp.get_prompt("grounded_tax_memo", {"question": "Are meals deductible?"})
    text = "\n".join(str(getattr(m.content, "text", m.content)) for m in result.messages)
    assert "Are meals deductible?" in text
    assert "verify_citations" in text
    assert "pinpoint" in text
    assert "not tax advice" in text


def test_server_instructions_mention_the_disclaimer() -> None:
    assert "not tax advice" in server.INSTRUCTIONS
    assert "verify_citations" in server.INSTRUCTIONS


# --------------------------------------------------------------------------------------
# check_case
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_check_case_rejects_a_statute(served: Path) -> None:
    """Pointing it at § 162 should say so, not fail obscurely."""
    del served
    text = await call("check_case", {"citation": "I.R.C. § 162(a)"})
    assert "not a case citation" in text


@pytest.mark.anyio
async def test_check_case_reports_an_unknown_decision_honestly(served: Path) -> None:
    """With no cache and no network, the answer is "I do not know", not "invented"."""
    del served
    text = await call("check_case", {"citation": "Smith v. Jones, 999 U.S. 999 (2020)"})
    assert "unverifiable" in text


@pytest.mark.anyio
async def test_check_case_always_states_the_treatment_boundary(served: Path) -> None:
    del served
    text = await call("check_case", {"citation": "Smith v. Jones, 999 U.S. 999 (2020)"})
    assert "reversed, vacated, or overruled" in text


@pytest.mark.anyio
async def test_check_case_reads_a_cached_decision(served: Path) -> None:
    """A decision already in the index is answered without a request."""
    from taxcite.index import db
    from taxcite.sources import courtlistener as cl

    connection = db.connect(served / "taxcite.db")
    cl.index_case(
        connection,
        cl.CaseRecord(
            id=cl.canonical_id("290 U.S. 111"),
            reporter_cite="290 U.S. 111",
            case_name="Welch v. Helvering",
            court="Supreme Court of the United States",
            date_filed="1933-11-06",
            url="/opinion/102139/welch-v-helvering/",
            citations=["290 U.S. 111"],
            cite_count=1700,
            citing_count=1401,
            last_cited="2026-08-20",
        ),
    )
    connection.close()
    text = await call("check_case", {"citation": "Welch v. Helvering, 290 U.S. 111 (1933)"})
    assert "verified" in text
    assert "Welch v. Helvering" in text
    assert "1,401 later decisions" in text
