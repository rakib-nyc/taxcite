"""The documentation has to keep the promises the code makes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
#: The README is hard-wrapped, so phrase assertions run against an unwrapped copy.
README_FLAT = " ".join(README.replace("**", "").replace("\n>", "\n").split())
GRAMMAR = (ROOT / "docs" / "citation-grammar.md").read_text(encoding="utf-8")


def test_readme_states_the_disclaimer() -> None:
    """The limits are the product; a reader must meet them before the features."""
    assert "not tax advice" in README_FLAT
    assert "does not judge whether a legal conclusion is correct" in README_FLAT


def test_the_disclaimer_comes_before_the_features() -> None:
    """A warning below the fold is a warning nobody reads."""
    assert README_FLAT.index("not tax advice") < README_FLAT.index("## Quickstart")


def test_readme_disclaims_warranties() -> None:
    assert "No warranty" in README
    assert "WITHOUT WARRANTIES OR CONDITIONS OF" in README
    assert "Experimental research software" in README


def test_readme_covers_every_section_the_spec_requires() -> None:
    for heading in (
        "## Why",
        "## Quickstart",
        "## Use it from an AI client",
        "## How it works",
        "## Limitations",
        "## Data sources",
        "## License",
    ):
        assert heading in README


def test_readme_quickstart_is_four_commands() -> None:
    block = README.split("## Quickstart", 1)[1].split("```", 2)[1]
    commands = [
        line
        for line in block.splitlines()
        if line.startswith("uv run") or line.startswith("uv sync")
    ]
    assert len(commands) == 4


def test_readme_documents_both_mcp_clients() -> None:
    assert "claude mcp add taxcite" in README
    assert "claude_desktop_config.json" in README


def test_readme_has_an_architecture_diagram() -> None:
    assert "```mermaid" in README


def test_readme_identifies_the_author_and_licence() -> None:
    assert "Muhammad Rakibul Islam" in README
    assert "rakib.islam@rutgers.edu" in README
    assert "Apache License, Version 2.0" in README


def test_readme_attributes_both_data_sources() -> None:
    assert "uscode.house.gov" in README
    assert "ecfr.gov" in README
    assert "public domain" in README


def test_readme_names_no_firm() -> None:
    """No accounting or publisher branding anywhere in the repository."""
    forbidden = ["PwC", "PricewaterhouseCoopers", "Deloitte", "KPMG", "Ernst & Young", "EY LLP"]
    for name in forbidden:
        assert name not in README


#: Files allowed to name proprietary research services. The project forbids *using*
#: those sources and forbids firm branding; it does not forbid naming a competitor in
#: prose, which the README does in order to say plainly what TaxCite is not.
NAMING_ALLOWED = {"README.md", "test_docs.py"}


def test_no_firm_names_anywhere_in_the_repository() -> None:
    forbidden = re.compile(
        r"\bPwC\b|PricewaterhouseCoopers|\bDeloitte\b|\bKPMG\b|Ernst\s*&\s*Young"
        r"|Westlaw|LexisNexis|Bloomberg\s+Tax|Checkpoint\b|\bCCH\b|Tax\s+Notes",
        re.IGNORECASE,
    )
    checked = 0
    for path in _published_files():
        if path.suffix not in {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".sql", ".jsonl"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        checked += 1
        hit = forbidden.search(text)
        if hit and path.name not in NAMING_ALLOWED:
            raise AssertionError(f"{path}: {hit.group(0)}")
    assert checked > 20


def test_every_cli_command_is_documented_in_the_readme() -> None:
    import typer.main

    from taxcite.cli import app

    names = {command.name or command.callback.__name__ for command in app.registered_commands}
    for name in names:
        assert f"taxcite {name}" in README, name
    assert typer.main is not None


def test_grammar_doc_lists_the_negative_cases() -> None:
    for phrase in ("Section 3 of the Agreement", "emphasis added", "Pub. L. 115-97"):
        assert phrase in GRAMMAR


@pytest.mark.parametrize(
    "form",
    [
        "`§ 162`",
        "`Treas. Reg. § 1.162-1(a)`",
        "`26 U.S.C. § 162`",
        "`Temp. Treas. Reg. § 1.469-5T`",
        "`Rev. Rul. 2019-24`",
    ],
)
def test_grammar_doc_shows_each_family(form: str) -> None:
    assert form.strip("`") in GRAMMAR


def test_no_proprietary_source_is_wired_into_the_code() -> None:
    """Naming a competitor in prose is fine; fetching from one is not (hard rule 2)."""
    proprietary = re.compile(
        r"westlaw\.com|lexisnexis\.com|bloomberglaw\.com|bloombergtax\.com"
        r"|checkpoint\.riag\.com|tax\.thomsonreuters\.com/checkpoint|cchcpelink|taxnotes\.com",
        re.IGNORECASE,
    )
    for path in (ROOT / "src").rglob("*.py"):
        assert not proprietary.search(path.read_text(encoding="utf-8")), path


def test_the_readme_keeps_the_boundaries_the_project_sets() -> None:
    """The limits are the product. They belong where a reader will meet them."""
    assert "not tax advice" in README
    assert "no warranty" in README.lower()
    assert "experimental" in README.lower()


def _published_files() -> list[Path]:
    """Return the files the repository actually publishes.

    Scanning the working tree would sweep in the project's own untracked working
    notes, which are deliberately not part of the distribution. What matters is what
    a reader who clones the repository receives.
    """
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / name for name in out.split("\0") if name and (ROOT / name).is_file()]


def test_the_published_tree_excludes_the_working_notes() -> None:
    """The spec, the work journal, and the unreviewed eval set stay unpublished."""
    published = {path.relative_to(ROOT).as_posix() for path in _published_files()}
    for private in ("SPEC.md", "PROGRESS.md", "ABOUT.md", "docs/roadmap.md"):
        assert private not in published, private
    assert not any(name.startswith("evals/") for name in published)


def test_the_published_tree_includes_what_a_user_needs() -> None:
    published = {path.relative_to(ROOT).as_posix() for path in _published_files()}
    for needed in ("README.md", "LICENSE", "pyproject.toml", "docs/privacy.md"):
        assert needed in published, needed


def test_the_user_agent_names_the_published_repository() -> None:
    """A stale placeholder here is invisible: government servers see it, not us.

    This lived only in a network-marked test, so it stayed green in CI while the
    User-Agent still named a repository that does not exist.
    """
    from taxcite.config import REPO_URL, USER_AGENT

    assert REPO_URL == "https://github.com/rakib-nyc/taxcite"
    assert REPO_URL in USER_AGENT
    assert "taxcite/" in USER_AGENT
