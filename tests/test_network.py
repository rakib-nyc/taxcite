"""Live smoke tests against the official sources.

These are the only tests that touch the network. They are marked ``network`` and are
deselected by default; run them with ``uv run pytest -m network``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from taxcite.config import USCODE_DOWNLOAD_PAGE
from taxcite.errors import SourceNotFoundError
from taxcite.index import db
from taxcite.sources import courtlistener, ecfr, irb
from taxcite.sources.http import HttpClient
from taxcite.sources.uscode import discover_release_point

pytestmark = pytest.mark.network


@pytest.fixture
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[HttpClient]:
    cache = tmp_path_factory.mktemp("taxcite-http-cache")
    with HttpClient(cache_root=cache) as http:
        yield http


def test_download_page_is_reachable(client: HttpClient) -> None:
    html = client.get_text(USCODE_DOWNLOAD_PAGE)
    assert "usc26" in html


def test_release_point_discovery(client: HttpClient) -> None:
    release_point = discover_release_point(client)
    assert release_point.release
    assert release_point.url.startswith("https://uscode.house.gov/download/")
    assert release_point.url.endswith(f"xml_usc26@{release_point.release}.zip")


def test_user_agent_names_the_repository(client: HttpClient) -> None:
    assert "github.com/taxcite" in client.client.headers["User-Agent"]


# --------------------------------------------------------------------------------------
# eCFR
# --------------------------------------------------------------------------------------


def test_ecfr_reports_a_current_date(client: HttpClient) -> None:
    date = ecfr.latest_date(client)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)


def test_ecfr_serves_a_single_section(client: HttpClient) -> None:
    date = ecfr.latest_date(client)
    xml = ecfr.fetch_section(client, date, "1.162-1")
    (parsed,) = ecfr.parse_sections(xml, date)
    assert parsed.section == "1.162-1"
    assert "ordinary and necessary expenditures" in parsed.provisions[1].text


def test_ecfr_requires_compression_and_the_client_asks_for_it(client: HttpClient) -> None:
    """The endpoint answers 406 unless the request accepts a compressed response."""
    assert "gzip" in client.client.headers["Accept-Encoding"]


def test_ecfr_reports_a_missing_section_as_not_found(client: HttpClient) -> None:
    date = ecfr.latest_date(client)
    with pytest.raises(SourceNotFoundError):
        ecfr.fetch_section(client, date, "1.9999-9")


def test_ecfr_structure_lists_the_parts(client: HttpClient) -> None:
    date = ecfr.latest_date(client)
    sections = ecfr.part_sections(client, date, "301")
    assert "301.7701-3" in sections


def test_a_whole_part_arrives_in_one_request(client: HttpClient, tmp_path: Path) -> None:
    date = ecfr.latest_date(client)
    connection = db.connect(tmp_path / "regs.db")
    try:
        sections, provisions, _refs, used = ecfr.build_parts(
            connection, client=client, parts=["301"], date=date
        )
        assert used == date
        assert sections > 500
        assert provisions > sections
        stored = db.get_provision(connection, "/us/cfr/t26/s301.7701-3/b/2/i/A")
        assert stored is not None
        assert stored.text.startswith("A partnership if it has two or more members")
    finally:
        connection.close()


def test_on_demand_fetch_writes_through_to_the_index(client: HttpClient, tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "ondemand.db")
    try:
        assert ecfr.make_fetcher(client)(connection, "1.61-1")
        provision = db.get_provision(connection, "/us/cfr/t26/s1.61-1")
        assert provision is not None
        assert provision.heading
    finally:
        connection.close()


def test_on_demand_fetch_of_a_missing_section_returns_false(
    client: HttpClient, tmp_path: Path
) -> None:
    connection = db.connect(tmp_path / "missing.db")
    try:
        assert ecfr.make_fetcher(client)(connection, "1.9999-9") is False
    finally:
        connection.close()


# --------------------------------------------------------------------------------------
# CourtListener
# --------------------------------------------------------------------------------------


def test_courtlistener_finds_a_real_decision(client: HttpClient) -> None:
    record = courtlistener.lookup(client, "290 U.S. 111")
    assert record is not None
    assert record.case_name == "Welch v. Helvering"
    assert "290 U.S. 111" in record.citations


def test_courtlistener_returns_nothing_for_a_fabricated_cite(client: HttpClient) -> None:
    assert courtlistener.lookup(client, "999 U.S. 999") is None


def test_the_citation_field_query_is_what_makes_this_work(client: HttpClient) -> None:
    """A bare phrase query returns everything citing the case, not the case."""
    record = courtlistener.lookup(client, "480 U.S. 23")
    assert record is not None
    assert record.case_name == "Commissioner v. Groetzinger"


def test_courtlistener_needs_no_api_key(client: HttpClient) -> None:
    assert "Authorization" not in client.client.headers


# --------------------------------------------------------------------------------------
# Internal Revenue Bulletin
# --------------------------------------------------------------------------------------


def test_a_bulletin_can_be_fetched_and_parsed(client: HttpClient) -> None:
    documents = irb.fetch_bulletin(client, 2024, 30)
    assert documents
    assert any(doc.kind == "Notice" for doc in documents)


def test_a_bulletin_that_does_not_exist(client: HttpClient) -> None:
    assert irb.fetch_bulletin(client, 2024, 99) is None


# --------------------------------------------------------------------------------------
# Checking a quotation from a decision without an API key
# --------------------------------------------------------------------------------------
#
# The passage is from Welch v. Helvering, 290 U.S. 111, 115 (1933), a public-domain
# opinion of the Supreme Court. See tests/fixtures/SOURCES.md.

WELCH_PASSAGE = "the standard set up by the statute is not a rule of law it is rather a way of life"


def test_a_real_passage_is_found_in_the_decision(client: HttpClient) -> None:
    assert courtlistener.phrase_in_case(client, "290 U.S. 111", WELCH_PASSAGE)


def test_a_real_passage_is_not_found_in_a_different_decision(client: HttpClient) -> None:
    """The query is pinned to one case, so a famous line does not pass everywhere."""
    assert not courtlistener.phrase_in_case(client, "480 U.S. 23", WELCH_PASSAGE)


def test_an_accurate_quotation_matches_to_its_last_word(client: HttpClient) -> None:
    words = len(WELCH_PASSAGE.split())
    assert courtlistener.longest_matching_prefix(client, "290 U.S. 111", WELCH_PASSAGE) == words


def test_an_altered_quotation_stops_matching_where_it_was_altered(client: HttpClient) -> None:
    """Word 18 is changed; everything before it is genuine, and the probe says so."""
    altered = WELCH_PASSAGE.replace("a way of life", "a submarine protocol")
    matched = courtlistener.longest_matching_prefix(client, "290 U.S. 111", altered)
    assert matched is not None
    assert 10 <= matched < len(altered.split())


def test_an_invented_quotation_matches_nothing(client: HttpClient) -> None:
    invented = "quarterly submarine inspection protocols govern the deduction of periscope costs"
    assert courtlistener.longest_matching_prefix(client, "290 U.S. 111", invented) == 0


def test_a_real_decision_reports_itself_as_searchable(client: HttpClient) -> None:
    assert courtlistener.is_searchable(client, "290 U.S. 111")
    assert not courtlistener.is_searchable(client, "999 U.S. 999")


def test_citation_history_is_read_off_the_live_source(client: HttpClient) -> None:
    """Gregory v. Helvering is heavily and recently cited; the numbers say so."""
    record = courtlistener.lookup(client, "293 U.S. 465")
    assert record is not None
    courtlistener.history(client, record)
    assert record.citing_count is not None
    assert record.citing_count > 500
    assert record.last_cited is not None
    assert record.last_cited > "2020-01-01"


# --------------------------------------------------------------------------------------
# Tax Court citations against the live source
# --------------------------------------------------------------------------------------


def test_a_published_tax_court_citation_resolves(client: HttpClient) -> None:
    record = courtlistener.lookup(client, "81 T.C. 806")
    assert record is not None
    assert "Commissioner" in record.case_name


def test_a_slip_opinion_number_resolves(client: HttpClient) -> None:
    """A slip citation: how a regular opinion is cited before it is paginated."""
    record = courtlistener.lookup(client, "155 T.C. No. 8")
    assert record is not None
    assert "Deckard" in record.case_name


def test_a_memorandum_citation_resolves_through_the_source_spelling(
    client: HttpClient,
) -> None:
    """Written "T.C. Memo. 1994-323"; stored "1994 T.C. Memo. 323"."""
    record = courtlistener.lookup(client, "T.C. Memo. 1994-323")
    assert record is not None
    assert "Pasqualini" in record.case_name


def test_the_source_really_does_store_the_other_spelling(client: HttpClient) -> None:
    """Guards the translation: without it, the ordinary form finds nothing."""
    assert courtlistener.is_searchable(client, "1994 T.C. Memo. 323")
    assert not courtlistener.is_searchable(client, "T.C. Memo. 1994-323")
