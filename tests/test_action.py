"""The shipped GitHub Action and example workflow must be valid and consistent."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "action.yml"
WORKFLOW = ROOT / "examples" / "github-workflow.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

yaml = pytest.importorskip("yaml", reason="PyYAML is not a runtime dependency")


def _load(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_action_is_a_composite_action() -> None:
    action = _load(ACTION)
    assert action["runs"]["using"] == "composite"  # type: ignore[index]


def test_action_declares_the_documented_inputs() -> None:
    inputs = _load(ACTION)["inputs"]
    assert {"files", "fail-on", "offline", "build-index"} <= set(inputs)  # type: ignore[arg-type]


def test_action_only_calls_real_cli_flags() -> None:
    from taxcite.cli import app

    text = ACTION.read_text(encoding="utf-8")
    used = set(re.findall(r"(--[a-z-]+)", text))
    known = {"--format", "--offline", "--fail-on", "--quiet", "--json", "--children"}
    cli_flags = {flag for flag in used if flag.startswith("--")}
    assert cli_flags & {"--format", "--fail-on"}
    assert cli_flags <= known | {"--directory"}
    assert app is not None


def test_example_workflow_references_the_action() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "uses: taxcite/taxcite@" in workflow


def test_ci_runs_all_four_checks() -> None:
    text = CI.read_text(encoding="utf-8")
    for command in ("ruff check", "ruff format --check", "mypy --strict", "pytest"):
        assert command in text


def test_ci_tests_both_supported_pythons() -> None:
    ci = _load(CI)
    versions = ci["jobs"]["check"]["strategy"]["matrix"]["python-version"]  # type: ignore[index]
    assert "3.11" in versions
    assert "3.12" in versions
