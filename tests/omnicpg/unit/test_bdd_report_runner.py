"""Unit tests for the BDD / Allure runner helpers."""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.run_bdd import (
    ALLURE_FORMATTER,
    build_allure_generate_command,
    build_allure_serve_command,
    build_behave_command,
    clean_path,
)


def test_build_behave_command_emits_allure_results() -> None:
    """The Behave command should use the Allure formatter and output directory."""
    command = build_behave_command(
        features_dir=Path("features"),
        results_dir=Path("allure-results"),
        tags="@smoke",
    )

    assert command[:3] == [sys.executable, "-m", "behave"]
    assert ALLURE_FORMATTER in command
    assert "allure-results" in command
    assert command[-3:] == ["--tags", "@smoke", "features"]


def test_build_allure_generate_command_targets_report_dir() -> None:
    """The report generation command should cleanly target allure-report."""
    command = build_allure_generate_command(Path("allure-results"), Path("allure-report"))

    assert command == [
        "allure",
        "generate",
        "allure-results",
        "-o",
        "allure-report",
        "--clean",
    ]


def test_build_allure_serve_command_uses_results_dir() -> None:
    """The serve command should point at the raw Allure results."""
    assert build_allure_serve_command(Path("allure-results")) == [
        "allure",
        "serve",
        "allure-results",
    ]


def test_clean_path_removes_generated_directory(tmp_path: Path) -> None:
    """Generated report directories should be removable before each run."""
    generated_dir = tmp_path / "allure-results"
    generated_dir.mkdir()
    (generated_dir / "result.json").write_text("{}", encoding="utf-8")

    clean_path(generated_dir)

    assert not generated_dir.exists()
