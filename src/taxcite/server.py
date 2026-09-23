"""The TaxCite MCP server (SPEC 9).

Every tool returns text written for a model to read: the provision's words first, then
the metadata needed to cite it — the canonical identifier, the official URL, and the
release point the text came from. A model that has those three things can cite
precisely instead of from memory, which is the whole point of the exercise.

Errors are returned as plain sentences. A client should never see a traceback, and
"Index not built. Run: taxcite build-index" is a far more useful answer than one.

The spec named the SDK's ``FastMCP`` class; in the installed 2.x SDK that class is
called ``MCPServer``. See docs/architecture.md.
"""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from typing import Final, ParamSpec

from mcp.server.mcpserver import MCPServer

from taxcite import __version__, api
from taxcite.config import DEFAULT_MAX_CHARS, DEFAULT_SEARCH_LIMIT
from taxcite.errors import TaxCiteError
from taxcite.graph import definitions as defs
from taxcite.graph import xrefs
from taxcite.index import db
from taxcite.models import Citation, CrossReference, Provision, SourceType
from taxcite.verify import verify_text
from taxcite.verify.report import CASE_NOTE, to_json, to_markdown

DISCLAIMER: Final = (
    "TaxCite is a research tool, not tax advice. It verifies that cited provisions "
    "exist and that quoted language matches the official source; it does not judge "
    "whether a legal conclusion is correct."
)

INSTRUCTIONS: Final = f"""\
TaxCite gives you the actual text of the Internal Revenue Code (26 U.S.C.) and the
Treasury Regulations (26 C.F.R.), and checks citations and quotations against them.
A regulation that is not already cached is fetched from the eCFR the first time you
ask for it, which takes a moment.

Use it like this:
- Search or look up a provision before asserting what it says.
- Quote only language these tools returned to you.
- Cite with a pinpoint: I.R.C. § 162(a)(1), Treas. Reg. § 1.162-1(a).
- Tax questions are almost always about a particular year. Pass as_of and tax_year
  when you know the year: current law is often the wrong answer, and a provision that
  did not govern the year is a real error however well it reads.
- Run verify_citations on your draft before you finish, and fix every error.
- Run check_authority when the question touches a return position: a memo resting on a
  treatise has a penalty problem under I.R.C. § 6662 that the prose will not show.

{DISCLAIMER}
"""

mcp: Final = MCPServer(
    name="taxcite",
    title="TaxCite",
    version=__version__,
    instructions=INSTRUCTIONS,
)

P = ParamSpec("P")


def friendly(tool: Callable[P, str]) -> Callable[P, str]:
    """Turn TaxCite's typed exceptions into a sentence a model can act on."""

    @functools.wraps(tool)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return tool(*args, **kwargs)
        except TaxCiteError as exc:
            return f"TaxCite could not answer that: {exc.friendly()}"

    return wrapper


@contextmanager
def _index() -> Iterator[sqlite3.Connection]:
    """Open the index for one tool call."""
    with api.open_index() as connection:
        yield connection


def _parse_date(value: str | None) -> date | None:
    """Parse an ISO date argument, ignoring anything unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _versions(connection: sqlite3.Connection) -> str:
    """Render the source versions line that every response carries."""
    versions = db.source_versions(connection)
    if not versions:
        return "Source version: unknown."
    rendered = ", ".join(f"{name}: {value}" for name, value in sorted(versions.items()))
    return f"Source version — {rendered}."


def _provision_response(result: api.LookupResult, connection: sqlite3.Connection) -> str:
    """Render a provision for a model: text first, then how to cite it."""
    provision = result.provision
    heading = f" — {provision.heading}" if provision.heading else ""
    lines = [f"{result.citation.display}{heading}", ""]
    if provision.status != "active":
        lines += [f"NOTE: this provision is {provision.status}.", ""]
    lines.append(result.text or "(this provision has no text of its own)")
    if result.truncated:
        lines += [
            "",
            "[truncated] Use list_subdivisions to navigate, or raise max_chars.",
        ]
    lines += ["", f"Canonical id: {provision.id}"]
    if result.children:
        lines.append("Subdivisions: " + ", ".join(child.num for child in result.children))
    if result.source_url:
        lines.append(f"Official source: {result.source_url}")
    lines.append(_versions(connection))
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


@mcp.tool()
@friendly
def get_irc_provision(
    citation: str,
    include_children: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
    as_of: str | None = None,
) -> str:
    """Get the official text of an Internal Revenue Code provision.

    Args:
        citation: Any citation form, e.g. "162", "§ 162(a)", "I.R.C. § 7701(a)(30)(A)".
        include_children: Include the text of every subdivision below it.
        max_chars: Truncate the text at this length.
        as_of: Read the law as it stood on this date (YYYY-MM-DD). Tax questions are
            usually about a past year, and current law is often the wrong answer.
    """
    when = _parse_date(as_of)
    with api.open_index(as_of=when) as connection:
        result = api.lookup(
            citation,
            include_children=include_children,
            max_chars=max_chars,
            connection=connection,
        )
        if result.provision.source is not SourceType.IRC:
            return (
                f"{result.citation.display} is a regulation, not a Code section. "
                "Use get_treasury_reg instead."
            )
        return _provision_response(result, connection)


@mcp.tool()
@friendly
def get_treasury_reg(
    citation: str, include_children: bool = True, max_chars: int = DEFAULT_MAX_CHARS
) -> str:
    """Get the official text of a Treasury Regulation.

    Args:
        citation: Any citation form, e.g. "1.162-1", "Treas. Reg. § 1.263(a)-4(b)(1)".
        include_children: Include the text of every paragraph below it.
        max_chars: Truncate the text at this length.
    """
    with _index() as connection:
        result = api.lookup(
            citation,
            include_children=include_children,
            max_chars=max_chars,
            connection=connection,
        )
        if result.provision.source is not SourceType.REG:
            return (
                f"{result.citation.display} is a Code section, not a regulation. "
                "Use get_irc_provision instead."
            )
        return _provision_response(result, connection)


@mcp.tool()
@friendly
def search_tax_law(query: str, source: str = "all", limit: int = DEFAULT_SEARCH_LIMIT) -> str:
    """Search the text of the Code and the regulations.

    Args:
        query: Words or a phrase to look for, e.g. "ordinary and necessary".
        source: "irc", "reg", or "all".
        limit: Maximum number of results.
    """
    with _index() as connection:
        hits = api.search(query, source=source, limit=limit, connection=connection)
        if not hits:
            return f"No provisions matched {query!r}."
        lines = [f"{len(hits)} results for {query!r}:", ""]
        for position, hit in enumerate(hits, start=1):
            heading = f" — {hit.heading}" if hit.heading else ""
            lines += [
                f"{position}. {hit.display}{heading}",
                f"   {hit.snippet}",
                f"   id: {hit.provision_id}",
            ]
        lines += ["", _versions(connection)]
        return "\n".join(lines)


@mcp.tool()
@friendly
def resolve_citation(citation: str) -> str:
    """Parse a citation and say whether the provision it names actually exists.

    Args:
        citation: The citation to check, e.g. "§ 162(z)".
    """
    from taxcite.verify.resolver import Resolver

    parsed = api.coerce_citation(citation)
    with _index() as connection:
        result = Resolver(connection).resolve(parsed)
        lines = [
            f"Citation: {result.citation.display}",
            f"Parsed as: source={parsed.source.value} section={parsed.section} "
            f"path={parsed.path or '[]'}",
            f"Canonical id: {parsed.canonical_id or '(none)'}",
            f"Status: {result.status.value} ({result.severity.value})",
            result.message,
        ]
        if result.provision_heading:
            lines.append(f"Heading: {result.provision_heading}")
        if result.suggestion:
            lines.append(f"Suggestion: {result.suggestion}")
        if result.source_url:
            lines.append(f"Official source: {result.source_url}")
        if parsed.warnings:
            lines.append("Warnings: " + "; ".join(parsed.warnings))
        lines.append(_versions(connection))
        return "\n".join(lines)


@mcp.tool()
@friendly
def check_case(citation: str, quote: str | None = None) -> str:
    """Look up a court decision, and check a passage you mean to quote from it.

    Use this before you attribute language to a case. A citation that resolves is not
    evidence that the words you put in quotation marks are in it, and a quotation that
    is nearly right is the failure this catches.

    Args:
        citation: The case, e.g. "Welch v. Helvering, 290 U.S. 111 (1933)".
        quote: A passage to check against the decision. Give the words only; enough of
            them to be distinctive — a handful is not enough to be sure of.
    """
    from taxcite.sources import courtlistener
    from taxcite.verify.resolver import Resolver

    parsed = api.coerce_citation(citation)
    if parsed.source is not SourceType.CASE or parsed.reporter_cite is None:
        return (
            f"{citation!r} is not a case citation. Give a reporter citation such as "
            f'"290 U.S. 111" or "T.C. Memo. 2020-12".'
        )
    with _index() as connection:
        result = Resolver(connection).resolve(parsed)
        lines = [
            f"Citation: {result.citation.display}",
            f"Status: {result.status.value} ({result.severity.value})",
            result.message,
        ]
        if result.suggestion:
            lines.append(result.suggestion)
        if result.source_url:
            lines.append(f"Official source: {result.source_url}")
        record = courtlistener.cached(connection, parsed.reporter_cite)
        if record is not None and record.excerpt:
            lines += ["", f"Excerpt: {record.excerpt}"]
        if quote and record is not None:
            lines += ["", _quote_verdict(connection, parsed, quote)]
        lines += ["", CASE_NOTE.replace("**", "")]
        return "\n".join(lines)


def _quote_verdict(connection: sqlite3.Connection, citation: Citation, quote: str) -> str:
    """Check one passage against one decision and describe the outcome."""
    from taxcite.verify.core import default_case_prober
    from taxcite.verify.quotes import ExtractedQuote, QuoteVerifier
    from taxcite.verify.resolver import Resolver

    result = Resolver(connection).resolve(citation)
    verifier = QuoteVerifier(connection, case_prober=default_case_prober(offline=False))
    check = verifier.check(ExtractedQuote(quote, (0, len(quote)), "straight"), result)
    lines = [f"Quotation: {check.status.value} (score {check.score:.2f})"]
    if check.diff:
        lines.append(check.diff)
    if check.matched_text:
        lines.append(f"Source reads: {check.matched_text}")
    return "\n".join(lines)


@mcp.tool()
@friendly
def verify_citations(text: str, tax_year: int | None = None) -> str:
    """Check every citation and quotation in a draft against the official sources.

    Run this on your own draft before you finish, and fix every error-severity finding.

    Args:
        text: The draft to check, as Markdown or plain text.
        tax_year: Also flag any provision that did not govern this tax year. Pass it
            whenever the question is about a particular year, which in tax it usually
            is.
    """
    with _index() as connection:
        report = verify_text(text, tax_year=tax_year, connection=connection)
        markdown = to_markdown(report, text=text)
        return f"{markdown}\n```json\n{to_json(report)}\n```\n"


@mcp.tool()
@friendly
def model_section_382(
    value: float,
    change_date: str,
    nol: float = 0.0,
    years: int = 0,
    taxable_income: str | None = None,
    rbig: float = 0.0,
    continuity: bool = True,
    short_year_days: int | None = None,
) -> str:
    """Compute the I.R.C. § 382 limitation on a loss corporation's pre-change NOLs.

    After an ownership change, § 382(a) caps how much post-change income the target's
    pre-change losses may offset. The cap is the corporation's value times the
    long-term tax-exempt rate for the month of the change — the number that decides
    what a target's loss carryforwards are worth in a deal.

    This computes and cites; it does not advise. It will not tell you whether an
    ownership change occurred (a § 382(g) factual question), what the corporation is
    worth, or whether a position is sustainable.

    Args:
        value: Fair market value of the loss corporation immediately before the
            ownership change, I.R.C. § 382(e)(1).
        change_date: Ownership change date as YYYY-MM-DD. Its month fixes the rate.
        nol: Pre-change net operating loss carryforwards subject to the limitation.
        years: How many taxable years to project absorption over.
        taxable_income: Optional comma-separated projected pre-NOL taxable income per
            year. Without it the projection assumes income at least equal to the
            limitation, which is the most favourable case and is labelled as such.
        rbig: Recognised built-in gain, I.R.C. § 382(h)(1)(A).
        continuity: Whether continuity of business enterprise is met, § 382(c)(1).
        short_year_days: Days in the first taxable year, for § 382(b)(3)(A) proration.
    """
    from decimal import Decimal

    from taxcite.model import section382

    when = _parse_date(change_date)
    if when is None:
        return "change_date is required, as YYYY-MM-DD."
    projected = (
        [Decimal(str(float(piece))) for piece in taxable_income.split(",") if piece.strip()]
        if taxable_income
        else None
    )
    with _index() as connection:
        result = section382.compute(
            connection,
            change_date=when,
            value=Decimal(str(value)),
            nol=Decimal(str(nol)),
            years=years,
            taxable_income=projected,
            rbig=Decimal(str(rbig)),
            continuity=continuity,
            short_year_days=short_year_days,
        )
        return section382.to_markdown(result)


@mcp.tool()
@friendly
def reading_list(
    citation: str, depth: int = 2, limit: int = 25, tax_year: int | None = None
) -> str:
    """List what to read to understand a provision, and what its terms of art mean.

    Use this before answering from a regulation whose sentences are built out of
    defined terms — the consolidated return rules especially, where "member",
    "group" and "SRLY" each mean something the regulation states elsewhere.

    Args:
        citation: The provision to start from, e.g. "Treas. Reg. § 1.1502-21(c)".
        depth: How far to walk the reference graph.
        limit: Maximum provisions to list.
        tax_year: Flag anything that did not govern this tax year.
    """
    from taxcite.graph import readinglist

    parsed = api.coerce_citation(citation)
    if parsed.canonical_id is None:
        return f"{parsed.display} is not a Code or regulation citation."
    with _index() as connection:
        reading = readinglist.build(
            connection, parsed.canonical_id, depth=depth, limit=limit, tax_year=tax_year
        )
        if reading is None:
            return f"{parsed.display} is not in the index."
        return readinglist.to_markdown(reading)


@mcp.tool()
@friendly
def test_ownership_change(register: str) -> str:
    """Test a shareholder register for an ownership change under I.R.C. § 382(g).

    Run this before computing a § 382 limitation: the limitation only applies if an
    ownership change has occurred, and whether one has is arithmetic over a register
    rather than a judgment call.

    Args:
        register: CSV rows of ``date,shareholder,percent`` — one row per holding per
            date, dates as YYYY-MM-DD. Percentages must already reflect any public
            group aggregation; this does not perform it.
    """
    import csv
    import io

    from taxcite.model import ownership

    rows: list[tuple[str, str, float | str]] = []
    for fields in csv.reader(io.StringIO(register)):
        cleaned = [f.strip() for f in fields if f.strip()]
        if not cleaned or cleaned[0].lower().startswith(("date", "#")):
            continue
        if len(cleaned) < 3:
            return "Each row needs date,shareholder,percent — e.g. 2024-06-30,Fund A,30."
        rows.append((cleaned[0], cleaned[1], cleaned[2]))
    try:
        holdings = ownership.parse_register(rows)
    except ValueError as exc:
        return f"Could not read the register: {exc}"
    return ownership.to_markdown(ownership.analyse(holdings))


@mcp.tool()
@friendly
def get_cross_references(citation: str, direction: str = "both") -> str:
    """List what a provision cites, and what cites it.

    Args:
        citation: The provision to look at, e.g. "§ 1411".
        direction: "outgoing", "incoming", or "both".
    """
    if direction not in {"outgoing", "incoming", "both"}:
        return 'direction must be "outgoing", "incoming", or "both".'
    parsed = api.coerce_citation(citation)
    if parsed.canonical_id is None:
        return f"{parsed.display} is not a Code or regulation citation."
    with _index() as connection:
        lines = [f"Cross-references for {parsed.display}", ""]
        if direction in {"outgoing", "both"}:
            lines += _reference_block(
                "Cites", xrefs.outgoing(connection, parsed.canonical_id), "to_id"
            )
        if direction in {"incoming", "both"}:
            lines += _reference_block(
                "Cited by", xrefs.incoming(connection, parsed.canonical_id), "from_id"
            )
        lines.append(_versions(connection))
        return "\n".join(lines)


def _reference_block(label: str, edges: list[CrossReference], side: str) -> list[str]:
    """Render one direction of the cross-reference graph, grouped by section."""
    if not edges:
        return [f"{label}: none recorded.", ""]
    grouped: dict[str, list[CrossReference]] = {}
    for edge in edges:
        key = edge.display or (edge.to_id if side == "to_id" else edge.from_id)
        grouped.setdefault(key, []).append(edge)
    lines = [f"{label} ({len(grouped)} provisions):"]
    for key in sorted(grouped):
        group = grouped[key]
        heading = next((e.heading for e in group if e.heading), None)
        suffix = f" — {heading}" if heading else ""
        kinds = ", ".join(sorted({e.kind for e in group}))
        lines.append(f"  {key}{suffix} [{len(group)}× {kinds}]")
    lines.append("")
    return lines


@mcp.tool()
@friendly
def find_definition(term: str, limit: int = 5) -> str:
    """Find where a term is defined, and what scope the definition has.

    Args:
        term: The term to look up, e.g. "gross income".
        limit: Maximum number of definitions to return.
    """
    with _index() as connection:
        found = defs.find(connection, term, limit=limit)
        if not found:
            return f"No definition of {term!r} found in the indexed sources."
        lines = [f"Definitions of {term!r}:", ""]
        for position, definition in enumerate(found, start=1):
            scope = definition.scope or "not stated"
            lines += [
                f"{position}. {definition.display} (scope: {scope})",
                f"   “{definition.term_display}”: {definition.definition_text}",
                f"   id: {definition.provision_id}",
            ]
            if definition.by_reference:
                lines.append(f"   defined by reference: {definition.by_reference}")
            lines.append("")
        lines.append(_versions(connection))
        return "\n".join(lines)


@mcp.tool()
@friendly
def check_authority(text: str, tax_year: int | None = None) -> str:
    """Classify a draft's citations as authority, or not, for penalty purposes.

    Applies Treas. Reg. § 1.6662-4(d)(3)(iii), which lists what counts as authority for
    the substantial-authority standard under I.R.C. § 6662. Treatises, law review
    articles and practitioners' opinions are expressly not authority; a position resting
    on them has a penalty-protection problem. This classifies; it does not weigh.

    Args:
        text: The draft to analyse.
        tax_year: Also check each authority against this tax year.
    """
    from taxcite.verify import authority

    with _index() as connection:
        report, year = authority.analyse_text(text, connection=connection, tax_year=tax_year)
        return authority.to_markdown(report, tax_year=year)


@mcp.tool()
@friendly
def compare_versions(citation: str, from_date: str, to_date: str | None = None) -> str:
    """Show how a provision changed between two dates.

    Args:
        citation: The provision, e.g. "§ 163(j)".
        from_date: The earlier date, YYYY-MM-DD.
        to_date: The later date; current law if omitted.
    """
    from datetime import date as _date

    from taxcite.index import compare

    try:
        earlier = _date.fromisoformat(from_date)
        later = _date.fromisoformat(to_date) if to_date else None
    except ValueError:
        return "Dates must be written YYYY-MM-DD."
    parsed = api.coerce_citation(citation)
    if parsed.canonical_id is None:
        return f"{parsed.display} is not a Code or regulation citation."
    with api.open_index(as_of=earlier) as before, api.open_index(as_of=later) as after:
        return compare.to_markdown(
            compare.compare_provision(before, after, parsed.canonical_id, citation=parsed.display)
        )


@mcp.tool()
@friendly
def reading_closure(citation: str, depth: int = 2, limit: int = 25) -> str:
    """List everything you must read to understand a provision.

    Tax provisions are a graph. Before asserting what § 163(j)(1) means, read what it
    points at. This walks outward, nearest first.

    Args:
        citation: The provision to start from, e.g. "§ 163(j)".
        depth: How many hops to follow. The transitive closure of the Code is the Code.
        limit: Maximum number of provisions to return.
    """
    parsed = api.coerce_citation(citation)
    if parsed.canonical_id is None:
        return f"{parsed.display} is not a Code or regulation citation."
    with _index() as connection:
        entries = xrefs.closure(connection, parsed.canonical_id, max_depth=depth, limit=limit)
        if not entries:
            return f"{parsed.display} does not depend on any other provision."
        lines = [f"To read {parsed.display} you also need:", ""]
        for entry in entries:
            heading = f" \u2014 {entry.heading}" if entry.heading else ""
            lines += [
                f"{'  ' * entry.depth}{entry.display}{heading}",
                f"{'  ' * entry.depth}  {entry.excerpt}",
                f"{'  ' * entry.depth}  (reached via {entry.reached_via})",
                "",
            ]
        lines.append(_versions(connection))
        return "\n".join(lines)


@mcp.tool()
@friendly
def list_subdivisions(citation: str) -> str:
    """List a provision's subdivisions, so you can fetch the one you need.

    Args:
        citation: The provision to open up, e.g. "§ 163".
    """
    with _index() as connection:
        provision, children = api.list_subdivisions(citation, connection=connection)
        heading = f" — {provision.heading}" if provision.heading else ""
        lines = [f"{_display(provision)}{heading}", ""]
        if not children:
            lines.append("This provision has no subdivisions.")
        else:
            for child in children:
                child_heading = f" — {child.heading}" if child.heading else ""
                lines.append(f"  {child.num}{child_heading}")
        lines += ["", _versions(connection)]
        return "\n".join(lines)


def _display(provision: Provision) -> str:
    """Render a provision's display citation."""
    from taxcite.citations import normalize as cite_norm

    return cite_norm.display_for(provision.source, provision.section, provision.path)


# --------------------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------------------


@mcp.resource("taxcite://irc/{section}")
def irc_section(section: str) -> str:
    """Return the full text of an Internal Revenue Code section."""
    return get_irc_provision(f"\u00a7 {section}", include_children=True, max_chars=0)


@mcp.resource("taxcite://reg/{section}")
def reg_section(section: str) -> str:
    """Return the full text of a Treasury Regulation section."""
    return get_treasury_reg(f"Treas. Reg. \u00a7 {section}", include_children=True, max_chars=0)


# --------------------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------------------


@mcp.prompt()
def grounded_tax_memo(question: str) -> str:
    """Draft a tax research memo grounded in the actual text of the law."""
    return f"""\
Write a short tax research memorandum answering this question:

{question}

Work in this order, and do not skip a step.

1. **Find the law before you describe it.** Use `search_tax_law` to locate the relevant
   provisions, then `get_irc_provision` or `get_treasury_reg` to read them. Use
   `list_subdivisions` to navigate a long section rather than guessing a pinpoint.
   Use `find_definition` for any term that carries a statutory definition.
2. **Quote only what the tools returned.** If you did not read it in a tool response,
   do not put it in quotation marks. Never reconstruct statutory language from memory.
3. **Cite with a pinpoint.** Write I.R.C. § 162(a)(1), not "section 162". For
   regulations write Treas. Reg. § 1.162-1(a).
4. **Check your own work.** Before you finish, run `verify_citations` on the full draft.
   Fix every finding at error severity and address every warning. If a quotation comes
   back as `quote_wrong_pinpoint` or `quote_misattributed`, correct the citation to the
   provision the tool identified rather than deleting the quotation.
5. **Say what you could not verify.** Revenue Rulings, Notices, and cases come back as
   `unverifiable`; that is expected, but say so rather than implying you checked them.

{DISCLAIMER} Say so in the memorandum.
"""


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run("stdio")


if __name__ == "__main__":  # pragma: no cover - module entry point
    main()
