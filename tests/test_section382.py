"""Published rates and the I.R.C. § 382 limitation (tax modelling).

Two things are under test. First, that the § 382(f) long-term tax-exempt rate is read
correctly out of the Bulletin the IRS actually publishes — the fixture is Table 3 of
Rev. Rul. 2026-17, verbatim. Second, that the limitation arithmetic follows the
statute, including the parts practitioners most often get wrong: the carryforward of
unused limitation under § 382(b)(2) and the short-year proration under § 382(b)(3)(A).

Every computation here is checked against figures worked by hand from the statute.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from taxcite.index import db
from taxcite.model import section382
from taxcite.sources import rates as rate_source

FIXTURE = Path(__file__).parent / "fixtures" / "rates" / "irb-2026-37-table3.html"
URL = "https://www.irs.gov/irb/2026-37_IRB"

# --------------------------------------------------------------------------------------
# Reading the rate out of the Bulletin
# --------------------------------------------------------------------------------------


@pytest.fixture
def parsed() -> rate_source.PublishedRate:
    (found,) = rate_source.parse_rates(FIXTURE.read_bytes(), "2026-37", URL)
    return found


def test_the_published_rate_is_read(parsed: rate_source.PublishedRate) -> None:
    assert parsed.long_term_tax_exempt == 3.88
    assert parsed.month == date(2026, 9, 1)


def test_the_ruling_that_published_it_is_recorded(parsed: rate_source.PublishedRate) -> None:
    """A rate without its source is a number nobody can check."""
    assert parsed.ruling == "Rev. Rul. 2026-17"
    assert parsed.bulletin == "2026-37"


def test_the_adjusted_federal_rate_is_also_captured(
    parsed: rate_source.PublishedRate,
) -> None:
    """§ 382(f) takes the highest of three months of this rate."""
    assert parsed.adjusted_federal_long_term == 3.88


def test_the_rate_is_usable_as_a_fraction(parsed: rate_source.PublishedRate) -> None:
    assert parsed.rate == pytest.approx(0.0388)


def test_a_bulletin_with_no_rate_table_yields_nothing() -> None:
    empty = b"<html><body><p>Notice 2026-1, page 3.</p></body></html>"
    assert rate_source.parse_rates(empty, "2026-01", URL) == []


# --------------------------------------------------------------------------------------
# The limitation
# --------------------------------------------------------------------------------------


@pytest.fixture
def indexed(tmp_path: Path) -> sqlite3.Connection:
    """An index holding the September 2026 rate."""
    connection = db.connect(tmp_path / "rates.db")
    rate_source.index_rates(
        connection, rate_source.parse_rates(FIXTURE.read_bytes(), "2026-37", URL)
    )
    connection.commit()
    return connection


def test_the_rate_round_trips_through_the_index(indexed: sqlite3.Connection) -> None:
    found = rate_source.rate_for(indexed, date(2026, 9, 15))
    assert found is not None
    assert found.long_term_tax_exempt == 3.88
    assert found.ruling == "Rev. Rul. 2026-17"


def test_the_rate_is_fixed_by_the_month_of_the_change(indexed: sqlite3.Connection) -> None:
    """§ 382(f) keys on the month; a neighbouring month is not a substitute."""
    assert rate_source.rate_for(indexed, date(2026, 9, 1)) is not None
    assert rate_source.rate_for(indexed, date(2026, 9, 30)) is not None
    assert rate_source.rate_for(indexed, date(2026, 8, 31)) is None
    assert rate_source.rate_for(indexed, date(2026, 10, 1)) is None


def test_the_base_limitation(indexed: sqlite3.Connection) -> None:
    """$50,000,000 × 3.88% = $1,940,000."""
    result = section382.compute(indexed, change_date=date(2026, 9, 15), value=Decimal(50_000_000))
    assert result.base == Decimal("1940000.00")
    assert result.annual == Decimal("1940000.00")


def test_an_unindexed_month_computes_nothing(indexed: sqlite3.Connection) -> None:
    """A missing rate must never be guessed at or interpolated from a neighbour."""
    result = section382.compute(indexed, change_date=date(2019, 4, 1), value=Decimal(50_000_000))
    assert not result.rate_known
    assert result.annual == Decimal(0)
    assert any("not in the index" in note for note in result.assumptions)


def test_no_continuity_zeroes_the_limitation(indexed: sqlite3.Connection) -> None:
    """§ 382(c)(1): without continuity of business enterprise the limitation is zero."""
    result = section382.compute(
        indexed,
        change_date=date(2026, 9, 15),
        value=Decimal(50_000_000),
        continuity=False,
    )
    assert result.annual == Decimal(0)
    assert any("382(c)(1)" in line.authority for line in result.lines)


def test_recognised_built_in_gain_increases_it(indexed: sqlite3.Connection) -> None:
    """§ 382(h)(1)(A)."""
    result = section382.compute(
        indexed,
        change_date=date(2026, 9, 15),
        value=Decimal(50_000_000),
        rbig=Decimal(500_000),
    )
    assert result.annual == Decimal("2440000.00")
    assert any("supplied rather than computed" in note for note in result.assumptions)


def test_a_short_first_year_is_prorated(indexed: sqlite3.Connection) -> None:
    """§ 382(b)(3)(A): $1,940,000 × 107/365 = $568,712.33."""
    result = section382.compute(
        indexed,
        change_date=date(2026, 9, 15),
        value=Decimal(50_000_000),
        nol=Decimal(30_000_000),
        years=1,
        short_year_days=107,
    )
    assert result.schedule[0].limitation == Decimal("568712.33")


def test_unused_limitation_carries_forward(indexed: sqlite3.Connection) -> None:
    """§ 382(b)(2): income below the limitation leaves room for the next year."""
    result = section382.compute(
        indexed,
        change_date=date(2026, 9, 15),
        value=Decimal(50_000_000),
        nol=Decimal(30_000_000),
        years=2,
        taxable_income=[Decimal(1_500_000), Decimal(10_000_000)],
    )
    first, second = result.schedule
    assert first.absorbed == Decimal("1500000.00")
    assert first.unused == Decimal("440000.00")
    # 1,940,000 + 440,000 carried in
    assert second.limitation == Decimal("2380000.00")
    assert second.absorbed == Decimal("2380000.00")


def test_absorption_never_exceeds_the_remaining_loss(indexed: sqlite3.Connection) -> None:
    result = section382.compute(
        indexed,
        change_date=date(2026, 9, 15),
        value=Decimal(50_000_000),
        nol=Decimal(1_000_000),
        years=3,
    )
    assert result.total_absorbed == Decimal("1000000.00")
    assert result.schedule[-1].remaining_nol == Decimal(0)


def test_absorption_never_exceeds_income(indexed: sqlite3.Connection) -> None:
    result = section382.compute(
        indexed,
        change_date=date(2026, 9, 15),
        value=Decimal(50_000_000),
        nol=Decimal(30_000_000),
        years=1,
        taxable_income=[Decimal(100_000)],
    )
    assert result.schedule[0].absorbed == Decimal("100000.00")


def test_a_missing_income_projection_is_labelled(indexed: sqlite3.Connection) -> None:
    """Assuming income is the most favourable case, and must be said out loud."""
    result = section382.compute(
        indexed, change_date=date(2026, 9, 15), value=Decimal(50_000_000), years=2
    )
    assert any("most favourable case" in note for note in result.assumptions)


def test_the_ownership_change_itself_is_never_claimed(indexed: sqlite3.Connection) -> None:
    """§ 382(g) is a factual question this tool does not answer."""
    result = section382.compute(indexed, change_date=date(2026, 9, 15), value=Decimal(50_000_000))
    assert any("382(g)" in note for note in result.assumptions)


# --------------------------------------------------------------------------------------
# The workpaper
# --------------------------------------------------------------------------------------


def test_every_line_carries_an_authority(indexed: sqlite3.Connection) -> None:
    result = section382.compute(
        indexed,
        change_date=date(2026, 9, 15),
        value=Decimal(50_000_000),
        nol=Decimal(30_000_000),
        years=3,
    )
    assert result.lines
    for line in result.lines:
        assert line.authority.startswith("I.R.C. §"), line


def test_the_workpaper_names_the_rate_source(indexed: sqlite3.Connection) -> None:
    result = section382.compute(
        indexed, change_date=date(2026, 9, 15), value=Decimal(50_000_000), years=2
    )
    rendered = section382.to_markdown(result)
    assert "Rev. Rul. 2026-17" in rendered
    assert "3.88%" in rendered
    assert "I.R.C. § 382(b)(1)" in rendered


def test_the_workpaper_says_it_is_not_advice(indexed: sqlite3.Connection) -> None:
    result = section382.compute(indexed, change_date=date(2026, 9, 15), value=Decimal(50_000_000))
    assert "not advice" in section382.to_markdown(result)


# --------------------------------------------------------------------------------------
# Attribution: the bug that only a live run exposed
# --------------------------------------------------------------------------------------
#
# Reading the month and the ruling from the document as a whole rather than from the
# table's own caption filed August's rates under June and named a ruling from 2003 as
# the source. A rate under the wrong month is the worst kind of error here: § 382(f)
# keys on the month, so the limitation comes out wrong while still carrying a citation
# that looks authoritative.

SYNTHETIC_DECOYS = b"""<!doctype html><html><body>
  <p>Findings List of Current Actions on Previously Published Items</p>
  <p>Rev. Rul. 2003-99, tables set forth the rates for June 2026.</p>
  <p>Modified by Rev. Rul. 2026-1, rates for January 2026.</p>
  <p>REV. RUL. 2026-13 TABLE 3 Rates Under Section 382 for August 2026</p>
  <table>
    <tr><td>Adjusted federal long-term rate for the current month</td><td>3.72%</td></tr>
    <tr><td>Long-term tax-exempt rate for ownership changes during the current month
        (the highest of the adjusted federal long-term rates for the current month
        and the prior two months.)</td><td>3.77%</td></tr>
  </table>
</body></html>"""


def test_the_month_comes_from_the_table_caption_not_the_document() -> None:
    """Decoy months elsewhere in the bulletin must not win."""
    (found,) = rate_source.parse_rates(SYNTHETIC_DECOYS, "2026-32", URL)
    assert found.month == date(2026, 8, 1)


def test_the_ruling_comes_from_the_table_caption_too() -> None:
    """A cross-referenced ruling in a findings list is not the publishing ruling."""
    (found,) = rate_source.parse_rates(SYNTHETIC_DECOYS, "2026-32", URL)
    assert found.ruling == "Rev. Rul. 2026-13"
    assert "2003" not in found.ruling


def test_the_tax_exempt_rate_is_not_the_adjusted_rate() -> None:
    """They differ: § 382(f) takes the highest of three months."""
    (found,) = rate_source.parse_rates(SYNTHETIC_DECOYS, "2026-32", URL)
    assert found.long_term_tax_exempt == 3.77
    assert found.adjusted_federal_long_term == 3.72


def test_an_uncaptioned_rate_table_is_ignored() -> None:
    """Without a caption the month is unknown, and a guess would be worse than nothing."""
    uncaptioned = b"""<!doctype html><html><body>
      <p>tables set forth the rates for August 2026.</p>
      <table><tr>
        <td>Long-term tax-exempt rate for ownership changes during the current month</td>
        <td>3.77%</td>
      </tr></table>
    </body></html>"""
    assert rate_source.parse_rates(uncaptioned, "2026-32", URL) == []


def test_html_comments_do_not_break_parsing() -> None:
    """IRS bulletins contain comments, which raise rather than yielding text."""
    commented = SYNTHETIC_DECOYS.replace(b"<body>", b"<body><!-- a comment -->")
    (found,) = rate_source.parse_rates(commented, "2026-32", URL)
    assert found.month == date(2026, 8, 1)


def test_one_month_is_emitted_once() -> None:
    """A bulletin repeating a table must not produce two rows for the same month."""
    doubled = SYNTHETIC_DECOYS.replace(b"</body>", SYNTHETIC_DECOYS.split(b"<body>")[1])
    found = rate_source.parse_rates(doubled, "2026-32", URL)
    assert len({r.month for r in found}) == len(found)
