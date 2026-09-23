"""Owner shift testing under I.R.C. § 382(g).

Whether an ownership change has occurred is arithmetic, and the arithmetic has two
details that decide most real cases: the comparison is shareholder by shareholder
against each one's own low point, and a decrease is floored at zero rather than
offsetting someone else's increase. Both are tested here against figures worked by
hand from the statute.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from taxcite.model import ownership


def register(rows: list[tuple[str, str, float]]) -> list[ownership.Holding]:
    return ownership.parse_register([(d, h, p) for d, h, p in rows])


# --------------------------------------------------------------------------------------
# The threshold
# --------------------------------------------------------------------------------------


def test_exactly_fifty_points_is_not_an_ownership_change() -> None:
    """§ 382(g)(1) says *more than* 50 percentage points. Fifty is not more than fifty."""
    report = ownership.analyse(
        register(
            [
                ("2024-01-01", "Founder", 90.0),
                ("2024-01-01", "Fund", 10.0),
                ("2025-01-01", "Founder", 40.0),
                ("2025-01-01", "Fund", 60.0),
            ]
        )
    )
    assert report.dates[-1].total_increase == Decimal("50.00")
    assert not report.dates[-1].is_ownership_change
    assert report.change is None


def test_one_hundredth_of_a_point_more_is_a_change() -> None:
    report = ownership.analyse(
        register(
            [
                ("2024-01-01", "Founder", 90.0),
                ("2024-01-01", "Fund", 9.99),
                ("2025-01-01", "Founder", 39.99),
                ("2025-01-01", "Fund", 60.01),
            ]
        )
    )
    assert report.dates[-1].is_ownership_change
    assert report.change is not None
    assert report.change.when == date(2025, 1, 1)


# --------------------------------------------------------------------------------------
# Shareholder by shareholder, decreases floored
# --------------------------------------------------------------------------------------


def test_a_seller_does_not_offset_a_buyer() -> None:
    """The founder's 60-point fall does not net against the funds' rises."""
    report = ownership.analyse(
        register(
            [
                ("2024-01-01", "Founder", 80.0),
                ("2024-01-01", "Fund A", 10.0),
                ("2024-01-01", "Fund B", 10.0),
                ("2025-01-01", "Founder", 20.0),
                ("2025-01-01", "Fund A", 40.0),
                ("2025-01-01", "Fund B", 40.0),
            ]
        )
    )
    last = report.dates[-1]
    by_holder = {s.holder: s.increase for s in last.shifts}
    assert by_holder["Founder"] == Decimal("0.00")
    assert by_holder["Fund A"] == Decimal("30.00")
    assert by_holder["Fund B"] == Decimal("30.00")
    assert last.total_increase == Decimal("60.00")
    assert last.is_ownership_change


def test_a_shareholder_never_reaching_five_percent_is_ignored() -> None:
    """§ 382(k)(7): only five-percent shareholders count."""
    report = ownership.analyse(
        register(
            [
                ("2024-01-01", "Founder", 98.0),
                ("2024-01-01", "Small", 2.0),
                ("2025-01-01", "Founder", 96.0),
                ("2025-01-01", "Small", 4.0),
            ]
        )
    )
    holders = {s.holder for s in report.dates[-1].shifts}
    assert "Small" not in holders
    assert report.dates[-1].total_increase == Decimal("0.00")


def test_a_shareholder_who_crosses_five_percent_does_count() -> None:
    report = ownership.analyse(
        register(
            [
                ("2024-01-01", "Founder", 96.0),
                ("2024-01-01", "Riser", 4.0),
                ("2025-01-01", "Founder", 70.0),
                ("2025-01-01", "Riser", 30.0),
            ]
        )
    )
    by_holder = {s.holder: s.increase for s in report.dates[-1].shifts}
    assert by_holder["Riser"] == Decimal("26.00")


# --------------------------------------------------------------------------------------
# The three-year window
# --------------------------------------------------------------------------------------


def test_the_window_is_three_years() -> None:
    report = ownership.analyse(
        register([("2026-03-15", "Founder", 60.0), ("2026-03-15", "Fund", 40.0)])
    )
    assert report.dates[0].window_from == date(2023, 3, 15)


def test_an_increase_older_than_the_window_drops_out() -> None:
    """The low point rolls forward, so old accumulation stops counting."""
    rows = [
        ("2020-01-01", "Founder", 90.0),
        ("2020-01-01", "Fund", 10.0),
        ("2021-01-01", "Founder", 40.0),
        ("2021-01-01", "Fund", 60.0),
        ("2026-01-01", "Founder", 40.0),
        ("2026-01-01", "Fund", 60.0),
    ]
    report = ownership.analyse(register(rows))
    early = next(d for d in report.dates if d.when == date(2021, 1, 1))
    late = next(d for d in report.dates if d.when == date(2026, 1, 1))
    assert early.total_increase == Decimal("50.00")
    assert late.total_increase == Decimal("0.00")
    assert report.change is None


def test_the_position_entering_the_window_is_the_starting_point() -> None:
    """A holding persists until changed, so the window opens at the last known level."""
    report = ownership.analyse(
        register(
            [
                ("2023-01-01", "Fund", 10.0),
                ("2024-06-30", "Fund", 30.0),
                ("2026-03-15", "Fund", 40.0),
            ]
        )
    )
    last = report.dates[-1]
    shift = next(s for s in last.shifts if s.holder == "Fund")
    assert shift.low == Decimal("10.00")
    assert shift.increase == Decimal("30.00")


def test_a_leap_day_testing_date_does_not_raise() -> None:
    report = ownership.analyse(register([("2024-02-29", "Founder", 100.0)]))
    assert report.dates[0].window_from == date(2021, 2, 28)


# --------------------------------------------------------------------------------------
# Input handling and honesty
# --------------------------------------------------------------------------------------


def test_an_empty_register_is_not_a_change() -> None:
    report = ownership.analyse([])
    assert report.dates == []
    assert report.change is None
    assert "No holdings supplied." in ownership.to_markdown(report)


@pytest.mark.parametrize("percent", [-1, 101, 250])
def test_an_impossible_percentage_is_rejected(percent: float) -> None:
    with pytest.raises(ValueError, match="percentage"):
        ownership.parse_register([("2024-01-01", "Fund", percent)])


def test_a_bad_date_is_rejected() -> None:
    with pytest.raises(ValueError):
        ownership.parse_register([("the first of June", "Fund", 10)])


def test_every_report_states_what_it_did_not_apply() -> None:
    """Attribution and public-group segregation could both change the answer."""
    report = ownership.analyse(register([("2024-01-01", "Founder", 100.0)]))
    rendered = ownership.to_markdown(report)
    assert "382(l)(3)" in rendered
    assert "1.382-2T(j)" in rendered
    assert "not a § 382 opinion" in rendered


def test_a_change_points_at_the_limitation_computation() -> None:
    report = ownership.analyse(
        register(
            [
                ("2024-01-01", "Founder", 90.0),
                ("2024-01-01", "Fund", 10.0),
                ("2025-06-01", "Founder", 20.0),
                ("2025-06-01", "Fund", 80.0),
            ]
        )
    )
    rendered = ownership.to_markdown(report)
    assert "model-382 --change-date 2025-06-01" in rendered


def test_headroom_is_reported_when_there_is_no_change() -> None:
    report = ownership.analyse(
        register(
            [
                ("2024-01-01", "Founder", 90.0),
                ("2024-01-01", "Fund", 10.0),
                ("2025-01-01", "Founder", 70.0),
                ("2025-01-01", "Fund", 30.0),
            ]
        )
    )
    assert report.peak is not None
    assert report.peak.headroom == Decimal("30.00")
    assert "points below the 50-point threshold" in ownership.to_markdown(report)
