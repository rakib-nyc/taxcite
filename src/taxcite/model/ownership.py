"""Owner shift testing under I.R.C. § 382(g).

The § 382 limitation only bites if an *ownership change* has happened, and whether one
has is not a judgment call — it is arithmetic over a shareholder register. § 382(g)(1)
says an ownership change occurs when, immediately after an owner shift involving a
five-percent shareholder, the percentage of stock owned by one or more five-percent
shareholders has increased by **more than 50 percentage points** over the lowest
percentage owned by those shareholders at any time during the testing period.

Two details do most of the work and are the two most often got wrong:

* The comparison is **shareholder by shareholder**. Each five-percent shareholder's
  increase is measured against that shareholder's own low point, and the increases are
  then summed. A shareholder who sold down does not offset one who bought in —
  decreases are floored at zero, which is why ordinary trading can accumulate toward a
  change that nobody intended.
* The testing period is the **three years ending on the testing date** (§ 382(i)(1)),
  so the low point rolls forward and old increases drop out of the window.

This module computes that, from a register the user supplies, and shows the working
shareholder by shareholder. It is deliberately not a § 382 opinion:

* Attribution under § 382(l)(3) and the constructive ownership rules are **not**
  applied. Percentages are taken as given.
* The segregation and aggregation rules for public groups under
  Treas. Reg. § 1.382-2T(j) are **not** applied. Small shareholders must already be
  aggregated into public groups in the register you supply.
* Equity structure shifts, options treated as exercised, and the § 382(l)(1)
  anti-stuffing rules are not modelled.

Each of those is stated on every report rather than left to be discovered, because a
number this precise invites more trust than the inputs deserve.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

#: § 382(g)(1): more than 50 percentage points.
THRESHOLD: Final = Decimal(50)

#: § 382(i)(1): the testing period is the three years ending on the testing date.
TESTING_PERIOD_YEARS: Final = 3

#: § 382(k)(7): a five-percent shareholder holds 5% or more at some point in the period.
FIVE_PERCENT: Final = Decimal(5)

_HUNDREDTH: Final = Decimal("0.01")


def _pct(value: Decimal) -> Decimal:
    """Round a percentage the way a workpaper would."""
    return value.quantize(_HUNDREDTH, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Holding:
    """One shareholder's percentage on one date."""

    when: date
    holder: str
    percent: Decimal


@dataclass(frozen=True, slots=True)
class Shift:
    """One five-percent shareholder's contribution on a testing date."""

    holder: str
    low: Decimal
    """The lowest percentage this shareholder held during the testing period."""
    current: Decimal
    increase: Decimal
    """``current - low``, floored at zero: a decrease is not an offset."""
    low_on: date


@dataclass(frozen=True, slots=True)
class TestingDate:
    """The owner shift measured on one date."""

    when: date
    window_from: date
    total_increase: Decimal
    shifts: list[Shift] = field(default_factory=list)

    @property
    def is_ownership_change(self) -> bool:
        """§ 382(g)(1): more than 50 percentage points."""
        return self.total_increase > THRESHOLD

    @property
    def headroom(self) -> Decimal:
        """Percentage points remaining before a change occurs."""
        return _pct(THRESHOLD - self.total_increase)


@dataclass(slots=True)
class OwnerShiftReport:
    """Every testing date in a register, and the first change if there is one."""

    holdings: list[Holding]
    dates: list[TestingDate] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def change(self) -> TestingDate | None:
        """The first testing date on which an ownership change occurred."""
        for entry in self.dates:
            if entry.is_ownership_change:
                return entry
        return None

    @property
    def peak(self) -> TestingDate | None:
        """The testing date with the largest owner shift."""
        return max(self.dates, key=lambda d: d.total_increase, default=None)


def parse_register(rows: Sequence[tuple[str, str, float | str | Decimal]]) -> list[Holding]:
    """Build a register from ``(date, holder, percent)`` triples.

    Raises:
        ValueError: if a date cannot be read or a percentage is out of range.
    """
    holdings: list[Holding] = []
    for when, holder, percent in rows:
        parsed = date.fromisoformat(when.strip())
        value = Decimal(str(percent))
        if not (Decimal(0) <= value <= Decimal(100)):
            raise ValueError(f"{holder} on {when}: {value} is not a percentage")
        holdings.append(Holding(when=parsed, holder=holder.strip(), percent=value))
    return sorted(holdings, key=lambda h: (h.when, h.holder))


def analyse(holdings: list[Holding]) -> OwnerShiftReport:
    """Measure the owner shift on every date in the register.

    Every date on which any holding is stated is treated as a testing date, which is
    the conservative reading: § 382(i) makes a testing date of any date on which an
    owner shift occurs, and a register records the dates its author thought mattered.
    """
    report = OwnerShiftReport(holdings=holdings, caveats=list(CAVEATS))
    if not holdings:
        return report

    holders = sorted({h.holder for h in holdings})
    dates = sorted({h.when for h in holdings})

    for testing_date in dates:
        window_from = _window_start(testing_date)
        shifts: list[Shift] = []
        for holder in holders:
            series = [h for h in holdings if h.holder == holder and h.when <= testing_date]
            if not series:
                continue
            current = series[-1].percent
            in_window = [h for h in series if h.when >= window_from] or [series[-1]]
            # The starting point matters: a holder's position entering the window is
            # their last holding at or before it, not their first one inside it.
            before = [h for h in series if h.when < window_from]
            candidates = in_window + ([before[-1]] if before else [])
            low_holding = min(candidates, key=lambda h: (h.percent, h.when))
            if max(h.percent for h in series) < FIVE_PERCENT and current < FIVE_PERCENT:
                # Never a five-percent shareholder, so § 382(g) does not count them.
                continue
            increase = max(Decimal(0), current - low_holding.percent)
            shifts.append(
                Shift(
                    holder=holder,
                    low=_pct(low_holding.percent),
                    current=_pct(current),
                    increase=_pct(increase),
                    low_on=low_holding.when,
                )
            )
        total = _pct(sum((s.increase for s in shifts), Decimal(0)))
        report.dates.append(
            TestingDate(
                when=testing_date,
                window_from=window_from,
                total_increase=total,
                shifts=sorted(shifts, key=lambda s: -s.increase),
            )
        )
    return report


def _window_start(testing_date: date) -> date:
    """The first day of the three-year testing period ending on ``testing_date``."""
    try:
        return testing_date.replace(year=testing_date.year - TESTING_PERIOD_YEARS)
    except ValueError:  # 29 February
        return testing_date.replace(year=testing_date.year - TESTING_PERIOD_YEARS, day=28)


#: Stated on every report. Each is a real rule this module does not apply, and each
#: could change the answer.
CAVEATS: Final[tuple[str, ...]] = (
    "Percentages are taken as supplied. Constructive ownership and the attribution "
    "rules of I.R.C. § 382(l)(3) are not applied.",
    "Public groups are not segregated or aggregated. Treas. Reg. § 1.382-2T(j) "
    "requires small shareholders to be grouped, and the grouping affects the result; "
    "the register must already reflect it.",
    "Options are not treated as exercised, and equity structure shifts under "
    "I.R.C. § 382(g)(3) are not modelled.",
    "Every date in the register is treated as a testing date. Whether a date is one "
    "under I.R.C. § 382(i) depends on whether an owner shift occurred on it.",
)


def to_markdown(report: OwnerShiftReport, *, detail: bool = True) -> str:
    """Render an owner shift study."""
    out: list[str] = ["# I.R.C. § 382(g) owner shift testing", ""]
    if not report.dates:
        return "\n".join([*out, "No holdings supplied."])

    change = report.change
    if change is not None:
        out += [
            f"**Ownership change on {change.when.isoformat()}** — cumulative owner "
            f"shift {change.total_increase}%, over the 50-point threshold of "
            f"I.R.C. § 382(g)(1).",
            "",
            "A § 382 limitation applies from that date. Compute it with "
            "`taxcite model-382 --change-date "
            f"{change.when.isoformat()}`.",
            "",
        ]
    else:
        peak = report.peak
        assert peak is not None  # dates is non-empty
        out += [
            f"**No ownership change.** The largest cumulative owner shift is "
            f"{peak.total_increase}% on {peak.when.isoformat()}, "
            f"{peak.headroom} points below the 50-point threshold.",
            "",
        ]

    out += [
        "## Owner shift by testing date",
        "",
        "| Testing date | Window opens | Shift | Change? |",
        "|---|---|---:|---|",
    ]
    for entry in report.dates:
        mark = "**YES**" if entry.is_ownership_change else "no"
        out.append(
            f"| {entry.when.isoformat()} | {entry.window_from.isoformat()} "
            f"| {entry.total_increase}% | {mark} |"
        )
    out.append("")

    focus = change or report.peak
    if detail and focus is not None and focus.shifts:
        out += [
            f"## How {focus.when.isoformat()} is made up",
            "",
            "Each five-percent shareholder is measured against their own low point in "
            "the window, and the increases are summed. A decrease is floored at zero: "
            "a shareholder who sold down does not offset one who bought in.",
            "",
            "| Shareholder | Low in window | On | Now | Increase |",
            "|---|---:|---|---:|---:|",
        ]
        for shift in focus.shifts:
            out.append(
                f"| {shift.holder} | {shift.low}% | {shift.low_on.isoformat()} "
                f"| {shift.current}% | {shift.increase}% |"
            )
        out.append("")
        out.append(f"**Cumulative owner shift: {focus.total_increase}%**")
        out.append("")

    out += ["## What this does not determine", ""]
    out += [f"- {caveat}" for caveat in report.caveats]
    out.append("")
    out.append(
        "> Arithmetic over the register you supplied, not a § 382 opinion. Whether "
        "these are the right percentages, and whether the rules above would change "
        "them, is the question this cannot answer."
    )
    return "\n".join(out)


__all__ = [
    "CAVEATS",
    "FIVE_PERCENT",
    "TESTING_PERIOD_YEARS",
    "THRESHOLD",
    "Holding",
    "OwnerShiftReport",
    "Shift",
    "TestingDate",
    "analyse",
    "parse_register",
    "to_markdown",
]
