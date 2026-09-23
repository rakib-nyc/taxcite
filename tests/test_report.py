"""Tests for report rendering in every format (SPEC 10)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from taxcite.models import Severity, Status
from taxcite.verify import verify_text
from taxcite.verify.report import (
    LineIndex,
    overall_severity,
    render,
    status_counts,
    to_github,
    to_json,
    to_markdown,
)

from conftest import MEMO_DIR

MEMO = (MEMO_DIR / "memo01_existence.md").read_text(encoding="utf-8")


@pytest.fixture
def report(conn: sqlite3.Connection):
    return verify_text(MEMO, connection=conn)


def test_line_index_locates_offsets() -> None:
    index = LineIndex("one\ntwo\nthree")
    assert index.locate(0) == (1, 1)
    assert index.locate(4) == (2, 1)
    assert index.locate(6) == (2, 3)
    assert index.locate(8) == (3, 1)


def test_line_index_on_a_single_line() -> None:
    assert LineIndex("no newlines").locate(5) == (1, 6)


def test_markdown_has_a_header_table_and_details(report) -> None:
    out = to_markdown(report, text=MEMO)
    assert out.startswith("# TaxCite verification report")
    assert "| # | Citation | Line | Status | Note |" in out
    assert "## Findings" in out
    assert "I.R.C. § 162A" in out


def test_markdown_states_the_disclaimer(report) -> None:
    out = to_markdown(report, text=MEMO)
    assert "not tax advice" in out
    assert "does not judge whether a legal conclusion is correct" in out


def test_markdown_reports_source_versions(report) -> None:
    assert "irc: 119-110" in to_markdown(report, text=MEMO)


def test_markdown_includes_line_numbers(report) -> None:
    out = to_markdown(report, text=MEMO)
    assert "(line " in out


def test_markdown_without_text_omits_line_numbers(report) -> None:
    assert "(line " not in to_markdown(report)


def test_markdown_for_a_document_with_no_citations(conn: sqlite3.Connection) -> None:
    out = to_markdown(verify_text("Nothing citable here.", connection=conn))
    assert "No tax citations found." in out


def test_markdown_escapes_table_pipes(conn: sqlite3.Connection) -> None:
    out = to_markdown(verify_text("§ 162", connection=conn), text="§ 162")
    for line in out.splitlines():
        if line.startswith("| 1 "):
            assert line.count("|") == 6


def test_json_round_trips(report) -> None:
    payload = json.loads(to_json(report))
    assert payload["taxcite_version"]
    assert payload["summary"]["citations"] == len(report.results)
    assert payload["results"][0]["citation"]["display"]


def test_github_annotations_only_cover_warnings_and_errors(report) -> None:
    out = to_github(report, text=MEMO, filename="memo.md")
    lines = [line for line in out.splitlines() if line]
    assert lines
    assert all(line.startswith(("::error ", "::warning ")) for line in lines)
    assert all("file=memo.md,line=" in line for line in lines)
    assert "162A" in out


def test_github_annotations_escape_newlines(conn: sqlite3.Connection) -> None:
    text = "§ 162(z)"
    out = to_github(verify_text(text, connection=conn), text=text, filename="f.md")
    assert "\n" not in out.strip()


def test_overall_severity(report, conn: sqlite3.Connection) -> None:
    assert overall_severity(report) is Severity.ERROR
    clean = verify_text("Section 162(a) is the rule.", connection=conn)
    assert overall_severity(clean) is Severity.OK


def test_status_counts(report) -> None:
    counts = status_counts(report)
    assert counts[Status.NOT_FOUND] == 1
    assert counts[Status.PINPOINT_NOT_FOUND] == 3
    assert counts[Status.REPEALED] == 1
    assert counts[Status.RESERVED] == 1


def test_summary_counts_every_severity(report) -> None:
    for severity in Severity:
        assert severity.value in report.summary


@pytest.mark.parametrize("fmt", ["md", "markdown", "json", "github"])
def test_render_dispatch(report, fmt: str) -> None:
    assert isinstance(render(report, fmt, text=MEMO, filename="memo.md"), str)


def test_render_rejects_an_unknown_format(report) -> None:
    with pytest.raises(ValueError, match="unknown report format"):
        render(report, "pdf")
