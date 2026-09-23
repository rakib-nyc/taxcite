"""The I.R.C. § 382 limitation on a loss corporation's pre-change attributes.

When a loss corporation undergoes an ownership change, § 382(a) caps the amount of
post-change income that its pre-change net operating losses may offset. The cap — the
"section 382 limitation" — is the value of the old loss corporation multiplied by the
long-term tax-exempt rate for the month of the change. It is the single number that
decides what a target's loss carryforwards are actually worth to a buyer, and it
routinely moves deal prices.

The computation is arithmetically simple and easy to get wrong in practice, because
its inputs come from four different places: a valuation, a rate the government
publishes monthly, a date that must be determined under § 382(g), and the taxpayer's
own attribute schedule. This module does the arithmetic, and — the point of doing it
here rather than in a spreadsheet — labels every line with the provision that
authorises it and names the Revenue Ruling the rate came from.

What this module does **not** do:

* determine whether an ownership change occurred. That is a § 382(g) factual question
  about five-percent shareholders and testing periods, and it is not arithmetic.
* value the loss corporation. § 382(e) requires fair market value immediately before
  the change; supplying it is the user's job.
* decide whether the continuity-of-business-enterprise requirement is met, or compute
  a net unrealised built-in gain. Both are accepted as stated inputs and both are
  reported as assumptions rather than findings.

Statutory basis, provision by provision:

===========================  ==================================================
§ 382(b)(1)                  limitation = value × long-term tax-exempt rate
§ 382(b)(2)                  unused limitation carries forward to the next year
§ 382(b)(3)(A)               short taxable year is prorated by days
§ 382(c)(1)                  limitation is zero without continuity of business
§ 382(e)(1)                  value is fair market value before the change
§ 382(f)                     the rate, published monthly under § 1274(d)
§ 382(h)(1)(A)               recognised built-in gain increases the limitation
===========================  ==================================================
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from taxcite.sources.rates import PublishedRate, rate_for

#: Days in the proration fraction of § 382(b)(3)(A). The statute says "365", without a
#: leap-year exception, so a leap year genuinely prorates to slightly over 100%.
DAYS_IN_YEAR: Final = Decimal(365)

_CENT: Final = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """Round to cents, the way a return does."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a computation, with the provision that authorises it."""

    label: str
    amount: Decimal | None
    authority: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Year:
    """What the limitation permits, and what it absorbs, in one taxable year."""

    year: int
    limitation: Decimal
    """The limitation available this year, including any carryforward."""
    carried_in: Decimal
    """Unused limitation brought forward from prior years, § 382(b)(2)."""
    taxable_income: Decimal | None
    """Pre-NOL taxable income, when the caller supplied a projection."""
    absorbed: Decimal
    """Pre-change NOL actually usable this year."""
    unused: Decimal
    """Limitation not used, which carries to the next year."""
    remaining_nol: Decimal
    """Pre-change NOL still unused at the end of the year."""
    days: int = 365
    """Days in the taxable year; a short year prorates under § 382(b)(3)(A)."""


@dataclass(frozen=True, slots=True)
class Limitation:
    """A computed § 382 limitation and the schedule that follows from it."""

    change_date: date
    value: Decimal
    rate: PublishedRate | None
    base: Decimal
    """The annual limitation before any built-in gain adjustment."""
    rbig: Decimal
    """Recognised built-in gain added under § 382(h)(1)(A)."""
    annual: Decimal
    """The annual limitation actually applied."""
    continuity: bool
    lines: list[Line] = field(default_factory=list)
    schedule: list[Year] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    @property
    def rate_known(self) -> bool:
        """Whether the governing rate was found in the index."""
        return self.rate is not None

    @property
    def total_absorbed(self) -> Decimal:
        """Pre-change NOL used across the projected schedule."""
        return sum((y.absorbed for y in self.schedule), Decimal(0))


def compute(
    connection: sqlite3.Connection,
    *,
    change_date: date,
    value: Decimal,
    nol: Decimal = Decimal(0),
    years: int = 0,
    taxable_income: list[Decimal] | None = None,
    rbig: Decimal = Decimal(0),
    continuity: bool = True,
    short_year_days: int | None = None,
) -> Limitation:
    """Compute a § 382 limitation for an ownership change.

    Args:
        connection: An open index, used to look up the § 382(f) rate.
        change_date: The ownership change date, determined under § 382(g). Its
            *month* fixes the rate.
        value: Fair market value of the old loss corporation immediately before the
            ownership change, § 382(e)(1).
        nol: Pre-change net operating loss carryforwards subject to the limitation.
        years: How many taxable years to project.
        taxable_income: Projected pre-NOL taxable income per year. Where it is
            omitted, the schedule assumes income at least equal to the limitation,
            which is the most favourable case and is labelled as such.
        rbig: Recognised built-in gain for the year, § 382(h)(1)(A).
        continuity: Whether the continuity-of-business-enterprise requirement of
            § 382(c)(1) is met. If not, the limitation is zero.
        short_year_days: Days in the first taxable year, for § 382(b)(3)(A)
            proration. Omit for a full year.

    Returns:
        A :class:`Limitation` carrying the arithmetic, a citation for every line, and
        the assumptions it was forced to make.
    """
    published = rate_for(connection, change_date)
    assumptions: list[str] = []
    lines: list[Line] = [
        Line(
            "Value of the old loss corporation",
            _money(value),
            "I.R.C. § 382(e)(1)",
            "fair market value immediately before the ownership change, as supplied",
        )
    ]

    if published is None:
        lines.append(
            Line(
                "Long-term tax-exempt rate",
                None,
                "I.R.C. § 382(f)",
                f"not indexed for {change_date:%B %Y}; the limitation cannot be computed",
            )
        )
        assumptions.append(
            f"The long-term tax-exempt rate for {change_date:%B %Y} is not in the index, "
            f"so no limitation is computed. Run `taxcite build-index --rates` for the "
            f"year of the ownership change."
        )
        return Limitation(
            change_date=change_date,
            value=_money(value),
            rate=None,
            base=Decimal(0),
            rbig=_money(rbig),
            annual=Decimal(0),
            continuity=continuity,
            lines=lines,
            schedule=[],
            assumptions=assumptions,
        )

    rate = Decimal(str(published.long_term_tax_exempt)) / Decimal(100)
    base = _money(value * rate)
    lines.append(
        Line(
            "Long-term tax-exempt rate",
            None,
            "I.R.C. § 382(f)",
            f"{published.long_term_tax_exempt:.2f}% for {published.month:%B %Y}, "
            f"published in {published.ruling} (I.R.B. {published.bulletin})",
        )
    )
    lines.append(Line("Base annual limitation", base, "I.R.C. § 382(b)(1)"))

    annual = base
    if rbig:
        annual = _money(annual + rbig)
        lines.append(
            Line(
                "Recognised built-in gain",
                _money(rbig),
                "I.R.C. § 382(h)(1)(A)",
                "increases the limitation; accepted as supplied, not computed here",
            )
        )
        assumptions.append(
            "Recognised built-in gain was supplied rather than computed. § 382(h) "
            "requires a net unrealised built-in gain determination that this tool "
            "does not perform."
        )

    if not continuity:
        lines.append(
            Line(
                "Continuity of business enterprise not met",
                Decimal(0),
                "I.R.C. § 382(c)(1)",
                "the limitation is zero for the year of the change and thereafter",
            )
        )
        annual = Decimal(0)

    lines.append(Line("Annual § 382 limitation", annual, "I.R.C. § 382(b)"))

    if taxable_income is None and years:
        assumptions.append(
            "No taxable income projection was supplied, so the schedule assumes income "
            "at least equal to the limitation in every year. This is the most "
            "favourable case; actual absorption cannot exceed actual income."
        )

    schedule = _schedule(
        annual=annual,
        nol=nol,
        years=years,
        taxable_income=taxable_income,
        start_year=change_date.year,
        short_year_days=short_year_days,
        lines=lines,
    )

    assumptions.append(
        "Whether an ownership change occurred under § 382(g) is a factual question "
        "about five-percent shareholders over the testing period. It is assumed here, "
        "not determined."
    )

    return Limitation(
        change_date=change_date,
        value=_money(value),
        rate=published,
        base=base,
        rbig=_money(rbig),
        annual=annual,
        continuity=continuity,
        lines=lines,
        schedule=schedule,
        assumptions=assumptions,
    )


def _schedule(
    *,
    annual: Decimal,
    nol: Decimal,
    years: int,
    taxable_income: list[Decimal] | None,
    start_year: int,
    short_year_days: int | None,
    lines: list[Line],
) -> list[Year]:
    """Project absorption of the pre-change loss across ``years`` taxable years."""
    if years <= 0:
        return []
    out: list[Year] = []
    remaining = nol
    carried = Decimal(0)
    for offset in range(years):
        days = short_year_days if offset == 0 and short_year_days is not None else 365
        available = annual
        if days != 365:
            available = _money(annual * Decimal(days) / DAYS_IN_YEAR)
            if offset == 0:
                lines.append(
                    Line(
                        f"Short taxable year ({days} days)",
                        available,
                        "I.R.C. § 382(b)(3)(A)",
                        "the limitation is prorated by days over 365",
                    )
                )
        ceiling = _money(available + carried)
        income = None
        if taxable_income is not None and offset < len(taxable_income):
            income = _money(taxable_income[offset])
        usable = ceiling if income is None else min(ceiling, income)
        absorbed = _money(min(usable, remaining))
        remaining = _money(remaining - absorbed)
        unused = _money(ceiling - absorbed)
        out.append(
            Year(
                year=start_year + offset,
                limitation=ceiling,
                carried_in=carried,
                taxable_income=income,
                absorbed=absorbed,
                unused=unused,
                remaining_nol=remaining,
                days=days,
            )
        )
        carried = unused
    if any(y.carried_in for y in out):
        lines.append(
            Line(
                "Unused limitation carried forward",
                None,
                "I.R.C. § 382(b)(2)",
                "limitation not used in a year increases the next year's limitation",
            )
        )
    return out


def to_markdown(result: Limitation) -> str:
    """Render a limitation as a workpaper a reviewer can follow."""
    out: list[str] = ["# I.R.C. § 382 limitation", ""]
    out.append(f"Ownership change: **{result.change_date.isoformat()}**")
    out.append("")

    out += ["## Computation", "", "| Line | Amount | Authority |", "|---|---:|---|"]
    for line in result.lines:
        amount = "" if line.amount is None else f"${line.amount:,.2f}"
        detail = f"{line.label}<br><sub>{line.note}</sub>" if line.note else line.label
        out.append(f"| {detail} | {amount} | {line.authority} |")
    out.append("")

    if result.rate is not None:
        out.append(
            f"Rate source: {result.rate.ruling}, I.R.B. {result.rate.bulletin} — "
            f"<{result.rate.url}>"
        )
        out.append("")

    if result.schedule:
        out += [
            "## Projected absorption",
            "",
            "| Year | Days | Limitation | Carried in | Taxable income "
            "| Absorbed | Unused | NOL remaining |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for year in result.schedule:
            income = "—" if year.taxable_income is None else f"${year.taxable_income:,.0f}"
            out.append(
                f"| {year.year} | {year.days} | ${year.limitation:,.0f} "
                f"| ${year.carried_in:,.0f} | {income} | ${year.absorbed:,.0f} "
                f"| ${year.unused:,.0f} | ${year.remaining_nol:,.0f} |"
            )
        out.append("")
        out.append(f"**Total absorbed over the projection: ${result.total_absorbed:,.0f}**")
        out.append("")

    if result.assumptions:
        out += ["## Assumptions and what was not determined", ""]
        out += [f"- {item}" for item in result.assumptions]
        out.append("")

    out.append(
        "> This is a computation, not advice. It applies § 382 to figures you supplied "
        "and cites the authority for each step. It does not determine whether an "
        "ownership change occurred, value the corporation, or opine on any position."
    )
    return "\n".join(out)


__all__ = ["DAYS_IN_YEAR", "Limitation", "Line", "Year", "compute", "to_markdown"]
