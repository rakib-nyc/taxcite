"""Tests for the diligence record, offline sealing, closure, and scope (roadmap 2.x)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taxcite.graph import definitions as defs
from taxcite.graph import xrefs
from taxcite.models import Definition, Severity
from taxcite.sources import http
from taxcite.verify import record as diligence
from taxcite.verify import verify_text

MEMO = "The rule is § 162(a). But § 162A does not exist, and § 4 was repealed."


# --------------------------------------------------------------------------------------
# Diligence record
# --------------------------------------------------------------------------------------


@pytest.fixture
def built(conn: sqlite3.Connection) -> diligence.DiligenceRecord:
    report = verify_text(MEMO, offline=True, connection=conn)
    return diligence.build(report, document="memo.md", text=MEMO, offline=True)


def test_the_document_is_identified_by_hash_not_content(
    built: diligence.DiligenceRecord,
) -> None:
    """An engagement file should not become another copy of privileged text."""
    payload = built.to_dict()
    assert built.document_hash.startswith("sha256:")
    assert MEMO not in str(payload)
    assert "162A" in str(payload)  # the finding is recorded, the prose is not


def test_the_hash_is_stable_and_content_addressed() -> None:
    assert diligence.document_hash("abc") == diligence.document_hash("abc")
    assert diligence.document_hash("abc") != diligence.document_hash("abd")


def test_the_record_captures_what_was_checked_against(
    built: diligence.DiligenceRecord,
) -> None:
    assert built.taxcite_version
    assert built.source_versions["irc"] == "119-110"
    assert built.offline is True
    assert isinstance(built.checked_at, datetime)


def test_the_record_lists_only_real_findings(built: diligence.DiligenceRecord) -> None:
    statuses = {finding["status"] for finding in built.findings}
    assert "not_found" in statuses
    assert "repealed" in statuses
    assert "verified" not in statuses


def test_the_outcome_is_the_worst_severity(built: diligence.DiligenceRecord) -> None:
    assert built.outcome == "error"


def test_a_clean_document_records_no_findings(conn: sqlite3.Connection) -> None:
    report = verify_text("See § 162(a).", offline=True, connection=conn)
    entry = diligence.build(report, document="clean.md", text="See § 162(a).", offline=True)
    assert entry.findings == []
    assert entry.outcome == "ok"


def test_the_record_states_its_own_limits(built: diligence.DiligenceRecord) -> None:
    """A compliance artifact that overstates itself is worse than none."""
    payload = built.to_dict()
    assert "10.22" in payload["authority"]
    assert "2026-19" in payload["authority"]
    assert "does not evidence" in payload["scope"]
    assert "legal analysis is correct" in payload["scope"]


def test_the_log_is_append_only(tmp_path: Path, built: diligence.DiligenceRecord) -> None:
    log = tmp_path / "nested" / "diligence.jsonl"
    diligence.append(log, built)
    diligence.append(log, built)
    assert len(diligence.read_log(log)) == 2


def test_reading_a_log_that_does_not_exist(tmp_path: Path) -> None:
    assert diligence.read_log(tmp_path / "nope.jsonl") == []


def test_markdown_rendering(built: diligence.DiligenceRecord) -> None:
    rendered = diligence.to_markdown(built)
    assert "# Citation diligence record" in rendered
    assert built.document_hash in rendered
    assert "Network access during the check:** none" in rendered
    assert "does not evidence" in rendered


def test_worst_of(built: diligence.DiligenceRecord) -> None:
    assert diligence.worst_of([built]) is Severity.ERROR
    assert diligence.worst_of([]) is Severity.OK


def test_tax_year_and_as_of_are_recorded(conn: sqlite3.Connection) -> None:
    report = verify_text(MEMO, offline=True, tax_year=2022, connection=conn)
    entry = diligence.build(report, document="m.md", text=MEMO, offline=True)
    assert entry.tax_year == 2022
    assert "tax_year" not in entry.source_versions


def test_now_returns_utc() -> None:
    assert diligence.now().tzinfo is UTC


# --------------------------------------------------------------------------------------
# The offline seal
# --------------------------------------------------------------------------------------


def test_sealing_the_network_blocks_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--offline is a promise, so it is enforced process-wide rather than per call."""
    monkeypatch.setattr(http, "_NETWORK_SEALED", False)
    assert not http.network_is_sealed()
    http.seal_network()
    assert http.network_is_sealed()
    client = http.HttpClient()
    with pytest.raises(http.OfflineError, match="offline mode is in force"):
        client._request("https://example.invalid/x")


def test_sealing_blocks_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "_NETWORK_SEALED", True)
    with pytest.raises(http.OfflineError):
        http.HttpClient().download("https://example.invalid/x", tmp_path / "f.zip")


# --------------------------------------------------------------------------------------
# Reading closure
# --------------------------------------------------------------------------------------


def test_closure_walks_outward(conn: sqlite3.Connection) -> None:
    entries = xrefs.closure(conn, "/us/usc/t26/s1411", max_depth=1, limit=20)
    assert entries
    assert all(entry.depth == 1 for entry in entries)
    assert all(entry.excerpt for entry in entries)


def test_closure_respects_depth_and_limit(conn: sqlite3.Connection) -> None:
    assert len(xrefs.closure(conn, "/us/usc/t26/s1411", max_depth=2, limit=3)) <= 3
    depths = {e.depth for e in xrefs.closure(conn, "/us/usc/t26/s1411", max_depth=1)}
    assert depths <= {1}


def test_closure_never_repeats_a_provision(conn: sqlite3.Connection) -> None:
    entries = xrefs.closure(conn, "/us/usc/t26/s1411", max_depth=2, limit=60)
    ids = [entry.provision_id for entry in entries]
    assert len(ids) == len(set(ids))


def test_closure_of_a_provision_that_cites_nothing(conn: sqlite3.Connection) -> None:
    assert xrefs.closure(conn, "/us/usc/t26/s7701/a/30/A", max_depth=1) == []


def test_closure_records_how_it_got_there(conn: sqlite3.Connection) -> None:
    entries = xrefs.closure(conn, "/us/usc/t26/s1411", max_depth=1, limit=5)
    assert all("§ 1411" in entry.reached_via for entry in entries)


# --------------------------------------------------------------------------------------
# Definition scope
# --------------------------------------------------------------------------------------


def _definition(scope: str | None, provision_id: str) -> Definition:
    return Definition(
        term_display="trade or business",
        term_norm="trade or business",
        provision_id=provision_id,
        display=provision_id,
        scope=scope,
        definition_text="...",
    )


@pytest.mark.parametrize(
    ("scope", "defined_at", "used_at", "expected"),
    [
        ("this title", "/us/usc/t26/s7701/a/26", "/us/usc/t26/s162/a", True),
        ("this section", "/us/usc/t26/s513/c", "/us/usc/t26/s162/a", False),
        ("this section", "/us/usc/t26/s513/c", "/us/usc/t26/s513/a", True),
        ("this subsection", "/us/usc/t26/s163/j/7", "/us/usc/t26/s163/j/1", True),
        ("this subsection", "/us/usc/t26/s163/j/7", "/us/usc/t26/s163/h", False),
        ("paragraph (1)", "/us/usc/t26/s469/c/5", "/us/usc/t26/s162/a", False),
        ("paragraph (1)", "/us/usc/t26/s469/c/5", "/us/usc/t26/s469/c/1", True),
        (None, "/us/usc/t26/s61/a", "/us/usc/t26/s162/a", True),
    ],
)
def test_reaches(scope: str | None, defined_at: str, used_at: str, expected: bool) -> None:
    assert defs.reaches(_definition(scope, defined_at), used_at) is expected


def test_scope_conflicts_names_the_mismatch(conn: sqlite3.Connection) -> None:
    conflicts = defs.scope_conflicts(conn, "trade or business", "/us/usc/t26/s162/a")
    assert conflicts
    reasons = " ".join(c.reason for c in conflicts)
    assert "does not reach" in reasons
    assert "§ 162(a)" in reasons


def test_governing_keeps_only_what_applies(conn: sqlite3.Connection) -> None:
    governing = defs.governing(conn, "trade or business", "/us/usc/t26/s162/a")
    assert governing
    assert all(d.scope in {None, "this title"} for d in governing)
    assert any("7701" in d.provision_id for d in governing)


def test_an_unknown_term_has_no_conflicts(conn: sqlite3.Connection) -> None:
    assert defs.scope_conflicts(conn, "zzzznotawordzzzz", "/us/usc/t26/s162/a") == []


# --------------------------------------------------------------------------------------
# The privacy statement
# --------------------------------------------------------------------------------------


def _privacy_text() -> str:
    return (Path(__file__).resolve().parents[1] / "docs" / "privacy.md").read_text(encoding="utf-8")


def test_privacy_document_covers_the_claims_the_code_makes() -> None:
    text = _privacy_text()
    assert "7216" in text
    assert "does not leave your machine" in text
    assert "seals the process" in text
    assert "SHA-256" in text


def test_the_privacy_document_discloses_the_one_thing_that_is_sent() -> None:
    """Checking a case quotation transmits the quotation. That must be stated."""
    text = _privacy_text()
    assert "courtlistener.com" in text
    assert "quoted passage" in text
    assert "COURTLISTENER_TOKEN" in text


def test_the_privacy_document_does_not_overclaim() -> None:
    """The blanket promise was true before case quotations were checked; it is not now."""
    assert "never leaves your machine" not in _privacy_text()
