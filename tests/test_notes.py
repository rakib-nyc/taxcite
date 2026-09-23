"""Tests for statutory notes and the effective-date check (roadmap 1.2)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from taxcite.citations import parse_citation
from taxcite.models import Status
from taxcite.sources import notes as note_source
from taxcite.verify import effective
from taxcite.verify.resolver import Resolver

# --------------------------------------------------------------------------------------
# Parsing dates out of the formulas
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Dec. 31, 2017", date(2017, 12, 31)),
        ("December 31, 2017", date(2017, 12, 31)),
        ("Jan. 1, 2026", date(2026, 1, 1)),
        ("July 4, 2025", date(2025, 7, 4)),
    ],
)
def test_parse_text_date(text: str, expected: date) -> None:
    assert note_source.parse_text_date(text) == expected


@pytest.mark.parametrize("text", ["not a date", "31 December 2017", ""])
def test_parse_text_date_rejects_other_shapes(text: str) -> None:
    assert note_source.parse_text_date(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Section applicable to taxable years beginning after Dec. 31, 2017, see "
            "section 11011(e) of Pub. L. 115-97.",
            date(2017, 12, 31),
        ),
        (
            "The amendments made by this section shall apply to taxable years "
            "beginning after December 31, 2025.",
            date(2025, 12, 31),
        ),
        (
            "Amendment applicable to taxable years ending after Mar. 12, 2020.",
            date(2020, 3, 12),
        ),
        (
            "The amendments shall apply to amounts paid or incurred after Dec. 22, 2017.",
            date(2017, 12, 22),
        ),
        ("Effective Jan. 1, 2018, the rule changes.", date(2018, 1, 1)),
    ],
)
def test_applicability_date(text: str, expected: date) -> None:
    assert note_source.applicability_date(text) == expected


def test_applicability_date_of_a_note_with_no_formula() -> None:
    assert note_source.applicability_date("Amendment by Pub. L. 115-141 effective") is None


# --------------------------------------------------------------------------------------
# Extraction from the fixture
# --------------------------------------------------------------------------------------


def test_notes_are_indexed(conn: sqlite3.Connection) -> None:
    from taxcite.index import db

    rows = db.get_notes(conn, "/us/usc/t26/s199A")
    assert rows
    topics = {str(row["topic"]) for row in rows}
    assert "effectiveDate" in topics
    assert "effectiveDateOfAmendment" in topics


def test_cross_headings_are_not_notes(conn: sqlite3.Connection) -> None:
    from taxcite.index import db

    for row in db.get_notes(conn, "/us/usc/t26/s199A"):
        assert row["text"].strip()
        assert row["heading"] != "Editorial Notes"


def test_notes_never_reach_provision_text(conn: sqlite3.Connection) -> None:
    """A note is not statutory text and must never be quotable as such."""
    from taxcite.index import db

    provision = db.get_provision(conn, "/us/usc/t26/s199A")
    assert provision is not None
    assert "Effective Date" not in provision.full_text
    assert "Pub. L. 115" not in provision.full_text


def test_effective_date_is_extracted(conn: sqlite3.Connection) -> None:
    found = effective.commencement(conn, "/us/usc/t26/s199A")
    assert found is not None
    assert found.applies_after == date(2017, 12, 31)


def test_a_pinpoint_inherits_its_sections_effective_date(
    conn: sqlite3.Connection,
) -> None:
    found = effective.commencement(conn, "/us/usc/t26/s199A/c/1")
    assert found is not None
    assert found.applies_after == date(2017, 12, 31)


def test_a_section_with_no_effective_date_note(conn: sqlite3.Connection) -> None:
    assert effective.commencement(conn, "/us/usc/t26/s61") is None


@pytest.mark.parametrize(
    ("tax_year", "expected"),
    [(2016, "not_yet"), (2017, "not_yet"), (2018, "applies"), (2024, "applies")],
)
def test_status_for_a_tax_year(conn: sqlite3.Connection, tax_year: int, expected: str) -> None:
    """The formula "beginning after December 31, 2017" starts to bite in 2018."""
    found = effective.commencement(conn, "/us/usc/t26/s199A")
    assert found is not None
    assert found.status_for(tax_year) == expected


def test_unknown_when_no_date_is_stated() -> None:
    unknown = effective.EffectiveDate("/us/usc/t26/s61", None, None, "")
    assert unknown.status_for(2020) == "unknown"


def test_later_amendments_are_surfaced(conn: sqlite3.Connection) -> None:
    pending = effective.later_amendments(conn, "/us/usc/t26/s199A", 2020)
    assert any(a.applies_after == date(2025, 12, 31) for a in pending)


def test_no_later_amendments_for_a_recent_year(conn: sqlite3.Connection) -> None:
    assert effective.later_amendments(conn, "/us/usc/t26/s199A", 2030) == []


def test_describe_reads_as_a_sentence(conn: sqlite3.Connection) -> None:
    found = effective.commencement(conn, "/us/usc/t26/s199A")
    assert found is not None
    assert "did not govern tax year 2017" in effective.describe(found, 2017)
    assert "did not govern" not in effective.describe(found, 2020)


# --------------------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------------------


def test_a_provision_cited_too_early_is_an_error(conn: sqlite3.Connection) -> None:
    result = Resolver(conn, tax_year=2017).resolve(parse_citation("§ 199A(a)"))
    assert result.status is Status.NOT_YET_EFFECTIVE
    assert result.severity.value == "error"
    assert "2017-12-31" in result.message


def test_the_same_provision_verifies_for_a_later_year(conn: sqlite3.Connection) -> None:
    result = Resolver(conn, tax_year=2020).resolve(parse_citation("§ 199A(a)"))
    assert result.status is Status.VERIFIED


def test_a_pending_amendment_is_a_note_not_an_error(conn: sqlite3.Connection) -> None:
    result = Resolver(conn, tax_year=2020).resolve(parse_citation("§ 199A(a)"))
    assert result.status is Status.VERIFIED
    assert result.suggestion is not None
    assert "2025-12-31" in result.suggestion


def test_no_tax_year_means_no_timing_check(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("§ 199A(a)"))
    assert result.status is Status.VERIFIED
    assert result.suggestion is None


def test_verify_text_threads_the_tax_year(conn: sqlite3.Connection) -> None:
    from taxcite.verify import verify_text

    report = verify_text("The deduction under § 199A(a) applied.", tax_year=2017, connection=conn)
    assert report.results[0].status is Status.NOT_YET_EFFECTIVE
    assert report.source_versions["tax_year"] == "2017"


# --------------------------------------------------------------------------------------
# Temporary regulation sunset, I.R.C. § 7805(e) (roadmap 2.3)
# --------------------------------------------------------------------------------------


def test_source_credits_are_captured() -> None:
    from pathlib import Path

    from taxcite.sources.ecfr import parse_sections

    (parsed,) = parse_sections(Path("tests/fixtures/ecfr/1.469-5T.xml").read_bytes(), "2026-09-08")
    assert parsed.issued == date(1988, 2, 25)
    assert "T.D. 8175" in parsed.source_credit


@pytest.mark.parametrize(
    ("credit", "expected"),
    [
        ("T.D. 9107, 69 FR 446, Jan. 5, 2004", date(2004, 1, 5)),
        (
            "T.D. 6500, 25 FR 11402, Nov. 26, 1960, as amended by T.D. 6690, "
            "28 FR 12253, Nov. 19, 1963",
            date(1960, 11, 26),
        ),
        ("no dates here", None),
    ],
)
def test_first_issued_takes_the_earliest_date(credit: str, expected: date | None) -> None:
    """Later dates in a credit are amendments; § 7805(e) runs from issuance."""
    from taxcite.sources.ecfr import first_issued

    assert first_issued(credit) == expected


def test_a_temporary_regulation_reports_its_sunset(conn: sqlite3.Connection) -> None:
    found = effective.temporary_sunset(conn, "/us/cfr/t26/s1.469-5T/a")
    assert found is not None
    assert found.issued == date(1988, 2, 25)
    assert found.expires == date(1991, 2, 25)
    assert found.expired


def test_a_final_regulation_has_no_sunset(conn: sqlite3.Connection) -> None:
    assert effective.temporary_sunset(conn, "/us/cfr/t26/s1.162-1/a") is None


def test_a_statute_has_no_sunset(conn: sqlite3.Connection) -> None:
    assert effective.temporary_sunset(conn, "/us/usc/t26/s162/a") is None


def test_the_sunset_does_not_reach_pre_1988_regulations() -> None:
    old = effective.TemporaryRegulation(
        "/us/cfr/t26/s1.1-1T", date(1985, 1, 1), date(1988, 1, 1), "credit"
    )
    assert old.expired
    assert not old.sunset_applies


def test_a_pre_1988_temporary_regulation_is_not_called_expired(
    conn: sqlite3.Connection,
) -> None:
    """§ 1.469-5T was issued in February 1988, before § 7805(e) took effect."""
    result = Resolver(conn).resolve(parse_citation("Temp. Treas. Reg. § 1.469-5T(a)"))
    assert result.status is Status.VERIFIED
    assert result.suggestion is not None
    assert "before I.R.C. § 7805(e) applied" in result.suggestion


def test_an_expired_temporary_regulation_is_flagged(tmp_path: Path) -> None:
    """A temporary regulation issued after 1988 expires three years on."""
    import json

    from taxcite.index import db
    from taxcite.models import Provision, SourceType

    connection = db.connect(tmp_path / "temp.db")
    try:
        provision = Provision(
            id="/us/cfr/t26/s1.9999-1T",
            source=SourceType.REG,
            section="1.9999-1T",
            path=[],
            level="section",
            num="1.9999-1T",
            heading="Lorem temporary rule",
            text="Lorem text.",
            full_text="Lorem text.",
            source_version="2026-09-08",
        )
        db.insert_provisions(connection, [provision], {provision.id: 0})
        db.insert_notes(
            connection,
            [
                (
                    provision.id,
                    0,
                    "sourceCredit",
                    None,
                    "T.D. 9999, 65 FR 1, Jan. 5, 2000",
                    json.dumps(["2000-01-05"]),
                    None,
                )
            ],
        )
        connection.commit()
        result = Resolver(connection).resolve(parse_citation("Temp. Treas. Reg. § 1.9999-1T"))
        assert result.status is Status.SUPERSEDED
        assert "7805(e)" in result.message
        assert result.suggestion is not None
        assert "2003-01-05" in result.suggestion
    finally:
        connection.close()


def test_a_final_regulation_is_not_flagged(conn: sqlite3.Connection) -> None:
    result = Resolver(conn).resolve(parse_citation("Treas. Reg. § 1.162-1(a)"))
    assert result.status is Status.VERIFIED
