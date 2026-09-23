"""The ``taxcite`` command-line interface (SPEC 10).

TaxCite is a research tool, not tax advice: it verifies that cited provisions exist and
that quoted language matches the official source, not that a legal conclusion is right.
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Final, ParamSpec, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from taxcite import __version__, api
from taxcite.config import DEFAULT_MAX_CHARS, DEFAULT_SEARCH_LIMIT, data_dir, db_path
from taxcite.errors import ConfigurationError, TaxCiteError
from taxcite.graph import definitions
from taxcite.graph import xrefs as graph_xrefs
from taxcite.index import db
from taxcite.index.builder import build_index
from taxcite.models import SEVERITY_ORDER, CrossReference, Severity
from taxcite.sources.http import seal_network
from taxcite.verify import record as diligence
from taxcite.verify import verify_text
from taxcite.verify.report import overall_severity, render

DISCLAIMER: Final = (
    "TaxCite is a research tool, not tax advice. It verifies that cited provisions "
    "exist and that quoted language matches the official source; it does not judge "
    "whether a legal conclusion is correct."
)

app: Final = typer.Typer(
    name="taxcite",
    help=f"Retrieve U.S. federal tax law and verify citations.\n\n{DISCLAIMER}",
    no_args_is_help=True,
    add_completion=False,
)

console: Final = Console()
err_console: Final = Console(stderr=True)

AsOf = Annotated[
    str | None,
    typer.Option(
        "--as-of",
        help="Read the law as it stood on this date (YYYY-MM-DD), not current law.",
    ),
]


def _parse_as_of(value: str | None) -> date | None:
    """Parse an --as-of value, or raise a friendly error."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ConfigurationError(
            f"could not read {value!r} as a date", hint="Use YYYY-MM-DD, e.g. 2022-12-31."
        ) from None


EXIT_OK: Final = 0
EXIT_FINDINGS: Final = 1
EXIT_ERROR: Final = 2

#: The severity at which ``verify`` starts reporting a non-zero exit code.
_FAIL_ON: Final[dict[str, Severity | None]] = {
    "error": Severity.ERROR,
    "warning": Severity.WARNING,
    "never": None,
}


P = ParamSpec("P")
R = TypeVar("R")


def friendly_errors(command: Callable[P, R]) -> Callable[P, R]:
    """Turn TaxCite's typed exceptions into a one-line message and exit code 2.

    No traceback ever reaches a user (SPEC 10).
    """

    @functools.wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return command(*args, **kwargs)
        except TaxCiteError as exc:
            err_console.print(f"[red]error:[/red] {exc.friendly()}")
            raise typer.Exit(EXIT_ERROR) from None

    return wrapper


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging.")] = False,
) -> None:
    """Configure logging for every subcommand."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@app.command()
def version() -> None:
    """Print the TaxCite version."""
    console.print(__version__)


@app.command("build-index")
@friendly_errors
def build_index_command(
    irc: Annotated[bool, typer.Option("--irc/--no-irc", help="Index the Code.")] = True,
    regs: Annotated[
        str | None,
        typer.Option("--regs", help="Comma-separated CFR Title 26 parts, e.g. 1,31,301."),
    ] = None,
    irb: Annotated[
        str | None,
        typer.Option(
            "--irb",
            help="Years of the Internal Revenue Bulletin to index, e.g. 2023-2026.",
        ),
    ] = None,
    rates: Annotated[
        str | None,
        typer.Option(
            "--rates",
            help=("Years to read published § 382 long-term tax-exempt rates for, e.g. 2025-2026."),
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-download sources already on disk.")
    ] = False,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "Build a historical index of the law as it stood on this date, "
                "alongside the current one."
            ),
        ),
    ] = None,
) -> None:
    """Download the official sources and build the local index."""
    if as_of is not None:
        _build_historical(_parse_as_of(as_of), force=force)
        return
    parts = [part.strip() for part in regs.split(",") if part.strip()] if regs else None
    years = _parse_years(irb)
    rate_years = _parse_years(rates)
    with console.status("Building index…") as status:

        def progress(stage: str, count: int) -> None:
            status.update(f"Building index… {count} {stage}")

        result = build_index(
            irc=irc,
            reg_parts=parts,
            irb_years=years,
            rate_years=rate_years,
            force=force,
            progress=progress,
        )
    console.print(
        f"Indexed [bold]{result.irc_sections}[/bold] IRC sections "
        f"({result.irc_provisions} provisions)"
        + (
            f" and [bold]{result.reg_sections}[/bold] regulation sections "
            f"({result.reg_provisions} provisions)"
            if result.reg_sections
            else ""
        )
        + f" into {db_path()}."
    )
    if result.guidance:
        console.print(f"Indexed [bold]{result.guidance}[/bold] guidance documents.")
    if result.rates:
        console.print(f"Indexed [bold]{result.rates}[/bold] months of published rates.")
    for label, version_string in result.source_versions.items():
        console.print(f"  {label}: {version_string}")


def _parse_years(value: str | None) -> list[int] | None:
    """Parse "2024", "2023-2026" or "2019,2024" into a list of years."""
    if not value:
        return None
    years: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            try:
                start, end = int(low), int(high)
            except ValueError:
                raise ConfigurationError(f"could not read {part!r} as a year range") from None
            if end < start or end - start > 40:
                raise ConfigurationError(f"{part!r} is not a sensible year range")
            years.update(range(start, end + 1))
            continue
        try:
            years.add(int(part))
        except ValueError:
            raise ConfigurationError(f"could not read {part!r} as a year") from None
    return sorted(years)


def _build_historical(as_of: date | None, *, force: bool) -> None:
    """Build the historical index covering ``as_of``, alongside the current one."""
    from taxcite.index import versions
    from taxcite.sources.http import HttpClient

    if as_of is None:  # pragma: no cover - guarded by the caller
        return
    connection = db.connect()
    try:
        with HttpClient() as client:
            versions.refresh_catalogue(connection, client)
            point = versions.resolve(connection, as_of, client=client)
            if point.release == versions.current_release(connection):
                console.print(
                    f"The law on {as_of.isoformat()} is the current release point "
                    f"([bold]{point.release}[/bold]); nothing more to build."
                )
                return
            label = f"Building release point {point.release}"
            with console.status(f"{label}\u2026") as status:

                def progress(stage: str, count: int) -> None:
                    status.update(f"{label}\u2026 {count} {stage}")

                path = versions.build_version(point, client=client, force=force, progress=progress)
            versions.refresh_catalogue(connection, client)
    finally:
        connection.close()
    enacted = point.enacted.isoformat() if point.enacted else "unknown"
    console.print(
        f"Indexed release point [bold]{point.release}[/bold] (enacted {enacted}), "
        f"{path.stat().st_size / 1_048_576:.0f} MB, at {path}."
    )


@app.command("versions")
@friendly_errors
def versions_command(
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-fetch the release-point catalogue.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many to list.")] = 40,
) -> None:
    """List the historical versions of the Code that are available and indexed."""
    from taxcite.index import versions as store
    from taxcite.sources.http import HttpClient

    connection = db.connect()
    try:
        known = store.catalogue(connection)
        if refresh or not known:
            with HttpClient() as client:
                known = store.refresh_catalogue(connection, client)
        indexed = store.indexed_releases(connection)
        current = store.current_release(connection)
    finally:
        connection.close()

    if not known:
        console.print("[yellow]no release points known[/yellow]")
        return

    sizes = store.disk_usage()
    table = Table(box=None, pad_edge=False)
    table.add_column("Release", style="bold", no_wrap=True)
    table.add_column("Enacted", no_wrap=True)
    table.add_column("Indexed", no_wrap=True)
    table.add_column("Size", justify="right", no_wrap=True)
    for point in known[:limit]:
        table.add_row(
            f"{point.release}{'  (current)' if point.release == current else ''}",
            point.enacted.isoformat() if point.enacted else "-",
            "yes" if point.release in indexed else "",
            f"{sizes[point.release] / 1_048_576:.0f} MB" if point.release in sizes else "",
        )
    console.print(table)
    if len(known) > limit:
        console.print(f"[dim]\u2026 and {len(known) - limit} more[/dim]")
    console.print("\n[dim]Build one with: taxcite build-index --as-of YYYY-MM-DD[/dim]")


@app.command()
@friendly_errors
def info() -> None:
    """Show index versions, counts, and the data directory."""
    console.print(f"taxcite {__version__}")
    console.print(f"data dir: {data_dir()}")
    console.print(f"index:    {db_path()}")
    try:
        details = api.index_info()
    except TaxCiteError as exc:
        console.print(f"[yellow]{exc.friendly()}[/yellow]")
        return
    table = Table(show_header=False, box=None)
    for key in sorted(details):
        table.add_row(key, details[key])
    console.print(table)


@app.command()
@friendly_errors
def lookup(
    citation: Annotated[str, typer.Argument(help='A citation, e.g. "§ 162(a)".')],
    children: Annotated[
        bool, typer.Option("--children/--no-children", help="Include subdivisions.")
    ] = True,
    max_chars: Annotated[
        int, typer.Option("--max-chars", help="Truncate the text at this length.")
    ] = DEFAULT_MAX_CHARS,
    offline: Annotated[
        bool, typer.Option("--offline", help="Never fetch a regulation from the eCFR.")
    ] = False,
    as_of: AsOf = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Print the official text of a cited provision."""
    when = _parse_as_of(as_of)
    result = api.lookup(
        citation,
        include_children=children,
        max_chars=max_chars,
        offline=offline or when is not None,
        as_of=when,
    )
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "citation": result.citation.model_dump(mode="json"),
                    "provision": result.provision.model_dump(mode="json"),
                    "text": result.text,
                    "truncated": result.truncated,
                    "source_url": result.source_url,
                    "children": [child.num for child in result.children],
                }
            )
        )
        return
    provision = result.provision
    heading = f" — {provision.heading}" if provision.heading else ""
    console.print(f"[bold]{result.citation.display}[/bold]{heading}")
    if provision.status != "active":
        console.print(f"[yellow]status: {provision.status}[/yellow]")
    console.print()
    console.print(result.text or "[dim](no text of its own)[/dim]")
    if result.truncated:
        console.print(f"\n[dim]… truncated at {max_chars} characters[/dim]")
    if result.children:
        nums = ", ".join(child.num for child in result.children)
        console.print(f"\n[dim]subdivisions: {nums}[/dim]")
    _print_effective_date(result)
    console.print(f"\n[dim]source: {result.source_url}[/dim]")
    console.print(f"[dim]version: {result.source_version}[/dim]")
    if when is not None:
        console.print(f"[dim]law as of: {when.isoformat()}[/dim]")


def _print_effective_date(result: api.LookupResult) -> None:
    """Show a provision's stated effective date, if the notes give one."""
    from taxcite.verify import effective

    with api.open_index() as connection:
        found = effective.commencement(connection, result.provision.id)
        pending = effective.later_amendments(connection, result.provision.id, date.today().year)
    if found is not None and found.applies_after is not None:
        console.print(
            f"\n[dim]effective: applies to taxable years beginning after "
            f"{found.applies_after.isoformat()}[/dim]"
        )
    for amendment in pending[:2]:
        label = f" ({amendment.heading})" if amendment.heading else ""
        console.print(
            f"[yellow]pending:[/yellow] an amendment applies only to years after "
            f"{amendment.applies_after.isoformat()}{label}"
        )


@app.command()
@friendly_errors
def search(
    query: Annotated[str, typer.Argument(help="Words to search for.")],
    source: Annotated[str, typer.Option("--source", help="irc, reg, or all.")] = "all",
    limit: Annotated[int, typer.Option("--limit", help="Maximum hits.")] = DEFAULT_SEARCH_LIMIT,
    as_of: AsOf = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Search the text of the Code and the regulations."""
    hits = api.search(query, source=source, limit=limit, as_of=_parse_as_of(as_of))
    if as_json:
        console.print_json(json.dumps([hit.model_dump(mode="json") for hit in hits]))
        return
    if not hits:
        console.print("[yellow]no matches[/yellow]")
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Citation", style="bold", no_wrap=True)
    table.add_column("Heading")
    table.add_column("Snippet")
    for position, hit in enumerate(hits, start=1):
        table.add_row(str(position), hit.display, hit.heading or "", hit.snippet)
    console.print(table)


@app.command()
@friendly_errors
def verify(
    files: Annotated[list[Path], typer.Argument(help="Files to check. Use - for standard input.")],
    fmt: Annotated[str, typer.Option("--format", help="md, json, or github.")] = "md",
    offline: Annotated[bool, typer.Option("--offline", help="Never reach the network.")] = False,
    case_quotes: Annotated[
        bool,
        typer.Option(
            "--case-quotes/--no-case-quotes",
            help=(
                "Check quotations attributed to court decisions. Doing so sends the "
                "quoted passage to CourtListener, because opinion text is not held "
                "locally; --no-case-quotes declines that without going fully offline."
            ),
        ),
    ] = True,
    fail_on: Annotated[str, typer.Option("--fail-on", help="error, warning, or never.")] = "error",
    as_of: AsOf = None,
    tax_year: Annotated[
        int | None,
        typer.Option(
            "--tax-year",
            help="Flag provisions that did not govern this tax year, e.g. 2022.",
        ),
    ] = None,
    record: Annotated[
        Path | None,
        typer.Option(
            "--record",
            help=("Append a diligence record to this JSON Lines log, for the engagement file."),
        ),
    ] = None,
) -> None:
    """Check every citation and quotation in one or more documents."""
    if fmt not in {"md", "markdown", "json", "github"}:
        raise ConfigurationError(f"unknown --format {fmt!r}", hint="Use md, json, or github.")
    if fail_on not in _FAIL_ON:
        raise ConfigurationError(
            f"unknown --fail-on {fail_on!r}", hint="Use error, warning, or never."
        )

    when = _parse_as_of(as_of)
    if offline:
        # A promise, not a preference: seal the process so that no code path can
        # reach the network, including one added later.
        seal_network()
    worst = Severity.OK
    with api.open_index(as_of=when) as connection:
        for position, file in enumerate(files):
            text = sys.stdin.read() if str(file) == "-" else _read(file)
            name = "<stdin>" if str(file) == "-" else str(file)
            report = verify_text(
                text,
                offline=offline,
                as_of=when,
                tax_year=tax_year,
                connection=connection,
                check_case_quotes=case_quotes,
            )
            if position and fmt != "github":
                console.print()
            if fmt == "json":
                console.print_json(render(report, "json"))
            else:
                rendered = render(report, fmt, text=text, filename=name)
                if rendered:
                    print(rendered)
            severity = overall_severity(report)
            if SEVERITY_ORDER[severity] > SEVERITY_ORDER[worst]:
                worst = severity
            if record is not None:
                entry = diligence.build(report, document=name, text=text, offline=offline)
                diligence.append(record, entry)
                err_console.print(
                    f"[dim]diligence record appended to {record} "
                    f"({entry.document_hash[:19]}…)[/dim]"
                )

    threshold = _FAIL_ON[fail_on]
    if threshold is not None and SEVERITY_ORDER[worst] >= SEVERITY_ORDER[threshold]:
        raise typer.Exit(EXIT_FINDINGS)


def _read(file: Path) -> str:
    """Read a document, raising a friendly error if it cannot be read."""
    try:
        return file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"could not read {file}: {exc.strerror}") from None


@app.command()
@friendly_errors
def xrefs(
    citation: Annotated[str, typer.Argument(help='A citation, e.g. "§ 1411".')],
    direction: Annotated[
        str, typer.Option("--direction", help="outgoing, incoming, or both.")
    ] = "both",
    internal: Annotated[
        bool,
        typer.Option("--internal", help="Include references within the same section."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show what a provision cites, and what cites it."""
    if direction not in {"outgoing", "incoming", "both"}:
        raise ConfigurationError(
            f"unknown --direction {direction!r}",
            hint="Use outgoing, incoming, or both.",
        )
    parsed = api.coerce_citation(citation)
    if parsed.canonical_id is None:
        raise ConfigurationError(f"{parsed.display} is not a Code or regulation citation")
    with api.open_index() as connection:
        edges: dict[str, list[CrossReference]] = {}
        if direction in {"outgoing", "both"}:
            edges["cites"] = graph_xrefs.outgoing(
                connection, parsed.canonical_id, internal=internal
            )
        if direction in {"incoming", "both"}:
            edges["cited by"] = graph_xrefs.incoming(
                connection, parsed.canonical_id, internal=internal
            )
    if as_json:
        console.print_json(
            json.dumps(
                {
                    label: [edge.model_dump(mode="json") for edge in group]
                    for label, group in edges.items()
                }
            )
        )
        return
    console.print(f"[bold]{parsed.display}[/bold]")
    for label, group in edges.items():
        console.print(f"\n[bold]{label}[/bold] ({len(group)})")
        if not group:
            console.print("  [dim]none recorded[/dim]")
        for edge in group:
            heading = f" — {edge.heading}" if edge.heading else ""
            console.print(f"  {edge.display or edge.to_id}{heading} [dim]({edge.kind})[/dim]")


@app.command("closure")
@friendly_errors
def closure_command(
    citation: Annotated[str, typer.Argument(help='A citation, e.g. "§ 163(j)".')],
    depth: Annotated[int, typer.Option("--depth", help="How far to walk.")] = 2,
    limit: Annotated[int, typer.Option("--limit", help="Maximum provisions.")] = 25,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List everything you have to read to understand a provision."""
    parsed = api.coerce_citation(citation)
    if parsed.canonical_id is None:
        raise ConfigurationError(f"{parsed.display} is not a Code or regulation citation")
    with api.open_index() as connection:
        entries = graph_xrefs.closure(connection, parsed.canonical_id, max_depth=depth, limit=limit)
    if as_json:
        console.print_json(
            json.dumps(
                [
                    {
                        "provision_id": e.provision_id,
                        "display": e.display,
                        "heading": e.heading,
                        "depth": e.depth,
                        "reached_via": e.reached_via,
                        "excerpt": e.excerpt,
                    }
                    for e in entries
                ]
            )
        )
        return
    console.print(f"[bold]To read {parsed.display} you also need:[/bold]\n")
    if not entries:
        console.print("[dim]nothing — it stands on its own[/dim]")
        return
    for entry in entries:
        indent = "  " * entry.depth
        heading = f" — {entry.heading}" if entry.heading else ""
        console.print(f"{indent}[bold]{entry.display}[/bold]{heading}")
        console.print(f"{indent}[dim]{entry.excerpt}[/dim]")
        console.print(f"{indent}[dim]via {entry.reached_via}[/dim]\n")


@app.command()
@friendly_errors
def define(
    term: Annotated[str, typer.Argument(help='A term, e.g. "gross income".')],
    limit: Annotated[int, typer.Option("--limit", help="Maximum definitions.")] = 5,
    at: Annotated[
        str | None,
        typer.Option(
            "--at",
            help='Only definitions governing this provision, e.g. "§ 162(a)".',
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Find where a term is defined, and what scope the definition has."""
    where = api.coerce_citation(at).canonical_id if at else None
    with api.open_index() as connection:
        found = definitions.find(connection, term, limit=limit)
        conflicts = (
            definitions.scope_conflicts(connection, term, where, limit=limit) if where else []
        )
        if where:
            found = definitions.governing(connection, term, where, limit=limit)
    if as_json:
        console.print_json(json.dumps([d.model_dump(mode="json") for d in found]))
        return
    if not found:
        console.print(f"[yellow]no definition of {term!r} found[/yellow]")
        for conflict in conflicts:
            console.print(f"[yellow]does not govern:[/yellow] {conflict.reason}")
        return
    for definition in found:
        scope = definition.scope or "not stated"
        console.print(f"[bold]{definition.display}[/bold] [dim](scope: {scope})[/dim]")
        console.print(f"  {definition.definition_text}")
        if definition.by_reference:
            console.print(f"  [dim]defined by reference: {definition.by_reference}[/dim]")
        console.print()
    for conflict in conflicts:
        console.print(f"[yellow]does not govern:[/yellow] {conflict.reason}")


@app.command("authority")
@friendly_errors
def authority_command(
    files: Annotated[
        list[Path], typer.Argument(help="Files to analyse. Use - for standard input.")
    ],
    tax_year: Annotated[
        int | None,
        typer.Option("--tax-year", help="Also check each authority against this year."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Classify a document's citations as authority, or not, under § 1.6662-4."""
    from taxcite.verify import authority

    with api.open_index() as connection:
        for position, file in enumerate(files):
            text = sys.stdin.read() if str(file) == "-" else _read(file)
            report, year = authority.analyse_text(text, connection=connection, tax_year=tax_year)
            if as_json:
                console.print_json(
                    json.dumps(
                        {
                            "file": "<stdin>" if str(file) == "-" else str(file),
                            "tax_year": year,
                            "regulation": authority.AUTHORITY_REGULATION,
                            "counts": report.counts,
                            "assessments": [
                                {
                                    "citation": a.citation.display,
                                    "source": a.citation.source.value,
                                    "kind": a.kind.value,
                                    "is_authority": a.is_authority,
                                    "reason": a.reason,
                                    "caveats": a.caveats,
                                }
                                for a in report.assessments
                            ],
                        }
                    )
                )
                continue
            if position:
                console.print()
            print(authority.to_markdown(report, tax_year=year))


@app.command("diff")
@friendly_errors
def diff_command(
    citation: Annotated[str, typer.Argument(help='A citation, e.g. "§ 163(j)".')],
    from_date: Annotated[str, typer.Option("--from", help="The earlier date (YYYY-MM-DD).")],
    to_date: Annotated[
        str | None,
        typer.Option("--to", help="The later date; current law if omitted."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show how a provision changed between two points in time."""
    from taxcite.index import compare

    earlier = _parse_as_of(from_date)
    later = _parse_as_of(to_date)
    parsed = api.coerce_citation(citation)
    if parsed.canonical_id is None:
        raise ConfigurationError(f"{parsed.display} is not a Code or regulation citation")

    with (
        api.open_index(as_of=earlier) as before,
        api.open_index(as_of=later) as after,
    ):
        result = compare.compare_provision(
            before, after, parsed.canonical_id, citation=parsed.display
        )
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "citation": result.citation,
                    "before_version": result.before_version,
                    "after_version": result.after_version,
                    "existed_before": result.existed_before,
                    "exists_after": result.exists_after,
                    "changes": [
                        {
                            "provision_id": change.provision_id,
                            "display": change.display,
                            "kind": change.kind,
                            "before_heading": change.before_heading,
                            "after_heading": change.after_heading,
                            "diff": change.diff,
                        }
                        for change in result.changes
                    ],
                }
            )
        )
        return
    print(compare.to_markdown(result))


@app.command()
@friendly_errors
def serve() -> None:
    """Run the MCP server over stdio, for Claude Code, Claude Desktop, or Cursor."""
    from taxcite.server import main as serve_main

    serve_main()


def main() -> None:
    """Entry point for the ``taxcite`` console script."""
    try:
        app()
    except TaxCiteError as exc:
        err_console.print(f"[red]error:[/red] {exc.friendly()}")
        raise SystemExit(EXIT_ERROR) from None


__all__ = ["DISCLAIMER", "app", "main"]


@app.command("model-382")
def model_382_command(
    value: Annotated[
        float,
        typer.Option(
            "--value",
            help="Fair market value of the loss corporation immediately before the "
            "ownership change (I.R.C. § 382(e)(1)).",
        ),
    ],
    change_date: Annotated[
        str,
        typer.Option("--change-date", help="Ownership change date, YYYY-MM-DD."),
    ],
    nol: Annotated[
        float, typer.Option("--nol", help="Pre-change NOL carryforwards subject to the limit.")
    ] = 0.0,
    years: Annotated[int, typer.Option("--years", help="Taxable years to project.")] = 0,
    income: Annotated[
        str | None,
        typer.Option(
            "--income",
            help="Comma-separated projected pre-NOL taxable income per year, e.g. 4e6,5e6.",
        ),
    ] = None,
    rbig: Annotated[
        float,
        typer.Option("--rbig", help="Recognised built-in gain, I.R.C. § 382(h)(1)(A)."),
    ] = 0.0,
    no_continuity: Annotated[
        bool,
        typer.Option(
            "--no-continuity",
            help="The continuity-of-business-enterprise requirement of § 382(c)(1) is not met.",
        ),
    ] = False,
    short_year_days: Annotated[
        int | None,
        typer.Option("--short-year-days", help="Days in the first taxable year, § 382(b)(3)(A)."),
    ] = None,
) -> None:
    """Compute an I.R.C. § 382 limitation, citing the authority for every line.

    This is a computation, not advice. It applies § 382 to figures you supply and
    names the Revenue Ruling the rate came from. It does not determine whether an
    ownership change occurred, value the corporation, or opine on any position.
    """
    from decimal import Decimal

    from taxcite.model import section382

    when = _parse_as_of(change_date)
    if when is None:
        raise ConfigurationError(
            "--change-date is required", hint="Give an ownership change date as YYYY-MM-DD."
        )
    projected = (
        [Decimal(str(float(piece))) for piece in income.split(",") if piece.strip()]
        if income
        else None
    )
    with api.open_index() as connection:
        result = section382.compute(
            connection,
            change_date=when,
            value=Decimal(str(value)),
            nol=Decimal(str(nol)),
            years=years,
            taxable_income=projected,
            rbig=Decimal(str(rbig)),
            continuity=not no_continuity,
            short_year_days=short_year_days,
        )
    print(section382.to_markdown(result))
