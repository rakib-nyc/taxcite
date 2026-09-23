"""Tests for the command-line interface (SPEC 10)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taxcite import __version__
from taxcite.cli import DISCLAIMER, app

runner = CliRunner()


@pytest.fixture
def cli_env(fixture_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a copy of the fixture index."""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(fixture_db_path, data / "taxcite.db")
    monkeypatch.setenv("TAXCITE_DATA_DIR", str(data))
    return data


def test_help_carries_the_disclaimer() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "research tool, not tax advice" in " ".join(result.output.split())


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    for command in ("build-index", "info", "lookup", "search", "version"):
        assert command in result.output


def test_disclaimer_text_covers_the_required_points() -> None:
    assert "not tax advice" in DISCLAIMER
    assert "exist" in DISCLAIMER
    assert "quoted language matches" in DISCLAIMER


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_lookup(cli_env: Path) -> None:
    result = runner.invoke(app, ["lookup", "§ 162(a)", "--no-children"])
    assert result.exit_code == 0
    assert "ordinary and necessary expenses" in " ".join(result.output.split())
    assert "I.R.C. § 162(a)" in result.output


def test_lookup_json(cli_env: Path) -> None:
    result = runner.invoke(app, ["lookup", "§ 7701(a)(30)(A)", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["provision"]["id"] == "/us/usc/t26/s7701/a/30/A"
    assert payload["text"] == "a citizen or resident of the United States,"


def test_lookup_reports_a_repealed_section(cli_env: Path) -> None:
    result = runner.invoke(app, ["lookup", "§ 4"])
    assert result.exit_code == 0
    assert "repealed" in result.output


def test_lookup_unknown_provision_is_a_friendly_error(cli_env: Path) -> None:
    result = runner.invoke(app, ["lookup", "§ 162(z)"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "error:" in result.output


def test_lookup_unparseable_input_is_a_friendly_error(cli_env: Path) -> None:
    result = runner.invoke(app, ["lookup", "nothing citable here"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_search(cli_env: Path) -> None:
    result = runner.invoke(app, ["search", "ordinary and necessary", "--limit", "3"])
    assert result.exit_code == 0
    assert "162" in result.output


def test_search_json(cli_env: Path) -> None:
    result = runner.invoke(app, ["search", "material participation", "--limit", "3", "--json"])
    assert result.exit_code == 0
    hits = json.loads(result.output)
    assert "/us/usc/t26/s469/h" in [hit["provision_id"] for hit in hits]


def test_search_with_no_matches(cli_env: Path) -> None:
    result = runner.invoke(app, ["search", "zzzznotawordzzzz"])
    assert result.exit_code == 0
    assert "no matches" in result.output


def test_info(cli_env: Path) -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    # rich wraps long paths, so compare against the unwrapped output.
    unwrapped = result.output.replace("\n", "")
    assert "119-110" in unwrapped
    assert str(cli_env) in unwrapped


def test_info_without_an_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAXCITE_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "taxcite build-index" in result.output


def test_build_index_rejects_regs_for_now(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAXCITE_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["build-index", "--no-irc", "--regs", "1"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------


def _memo(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / "memos" / name


def test_verify_markdown(cli_env: Path) -> None:
    result = runner.invoke(app, ["verify", str(_memo("memo01_existence.md"))])
    assert result.exit_code == 1  # planted errors
    assert "# TaxCite verification report" in result.output
    assert "not tax advice" in result.output


def test_verify_json(cli_env: Path) -> None:
    result = runner.invoke(app, ["verify", str(_memo("memo01_existence.md")), "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["summary"]["not_found"] == 1


def test_verify_github_annotations(cli_env: Path) -> None:
    result = runner.invoke(app, ["verify", str(_memo("memo01_existence.md")), "--format", "github"])
    assert result.exit_code == 1
    assert "::error file=" in result.output


def test_verify_clean_document_exits_zero(cli_env: Path, tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("The rule is in I.R.C. § 162(a).", encoding="utf-8")
    result = runner.invoke(app, ["verify", str(clean)])
    assert result.exit_code == 0


def test_verify_fail_on_never(cli_env: Path) -> None:
    result = runner.invoke(app, ["verify", str(_memo("memo01_existence.md")), "--fail-on", "never"])
    assert result.exit_code == 0


def test_verify_fail_on_warning(cli_env: Path, tmp_path: Path) -> None:
    warned = tmp_path / "warn.md"
    warned.write_text("See § 4, which was repealed.", encoding="utf-8")
    assert runner.invoke(app, ["verify", str(warned)]).exit_code == 0
    assert runner.invoke(app, ["verify", str(warned), "--fail-on", "warning"]).exit_code == 1


def test_verify_reads_standard_input(cli_env: Path) -> None:
    result = runner.invoke(app, ["verify", "-"], input="A cite to § 162A.\n")
    assert result.exit_code == 1
    assert "162A" in result.output


def test_verify_multiple_files(cli_env: Path) -> None:
    result = runner.invoke(
        app,
        [
            "verify",
            str(_memo("memo02_quotes_good.md")),
            str(_memo("memo01_existence.md")),
        ],
    )
    assert result.exit_code == 1
    assert result.output.count("# TaxCite verification report") == 2


def test_verify_rejects_an_unknown_format(cli_env: Path) -> None:
    result = runner.invoke(app, ["verify", str(_memo("memo01_existence.md")), "--format", "pdf"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_verify_rejects_an_unknown_fail_on(cli_env: Path) -> None:
    result = runner.invoke(
        app, ["verify", str(_memo("memo01_existence.md")), "--fail-on", "sometimes"]
    )
    assert result.exit_code == 2


def test_verify_missing_file_is_a_friendly_error(cli_env: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["verify", str(tmp_path / "nope.md")])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_verify_offline(cli_env: Path, tmp_path: Path) -> None:
    doc = tmp_path / "reg.md"
    doc.write_text("See Treas. Reg. § 1.9999-9(a).", encoding="utf-8")
    result = runner.invoke(app, ["verify", str(doc), "--offline", "--format", "json"])
    payload = json.loads(result.output)
    assert payload["results"][0]["status"] == "source_unavailable"
    assert "offline" in payload["results"][0]["message"]


# --------------------------------------------------------------------------------------
# xrefs, define, serve
# --------------------------------------------------------------------------------------


def test_xrefs(cli_env: Path) -> None:
    result = runner.invoke(app, ["xrefs", "§ 1411", "--direction", "outgoing"])
    assert result.exit_code == 0
    assert "cites" in result.output
    assert "469" in result.output


def test_xrefs_json(cli_env: Path) -> None:
    result = runner.invoke(app, ["xrefs", "§ 1411", "--direction", "outgoing", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["cites"]


def test_xrefs_rejects_a_bad_direction(cli_env: Path) -> None:
    result = runner.invoke(app, ["xrefs", "§ 1411", "--direction", "sideways"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_xrefs_rejects_a_non_statutory_citation(cli_env: Path) -> None:
    result = runner.invoke(app, ["xrefs", "Rev. Rul. 2019-24"])
    assert result.exit_code == 2


def test_define(cli_env: Path) -> None:
    result = runner.invoke(app, ["define", "gross income", "--limit", "2"])
    assert result.exit_code == 0
    assert "I.R.C. § 61(a)" in result.output


def test_define_json(cli_env: Path) -> None:
    result = runner.invoke(app, ["define", "gross income", "--limit", "1", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)[0]["provision_id"] == "/us/usc/t26/s61/a"


def test_define_with_no_match(cli_env: Path) -> None:
    result = runner.invoke(app, ["define", "zzzznotawordzzzz"])
    assert result.exit_code == 0
    assert "no definition" in result.output


def test_lookup_accepts_a_bare_section_number(cli_env: Path) -> None:
    result = runner.invoke(app, ["lookup", "162(a)", "--no-children"])
    assert result.exit_code == 0
    assert "ordinary and necessary" in " ".join(result.output.split())


def test_serve_is_registered() -> None:
    assert "serve" in runner.invoke(app, ["--help"]).output


def test_verify_help_says_what_case_quote_checking_sends() -> None:
    """A reader must be able to find the transmission without reading the source."""
    # Pin the width: the help text is boxed and wrapped to the terminal, so a narrow
    # one (a CI runner's, say) truncates the very flag this is checking for.
    result = runner.invoke(
        app, ["verify", "--help"], env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0
    output = " ".join(result.stdout.split())
    assert "--no-case-quotes" in output
    assert "CourtListener" in output
