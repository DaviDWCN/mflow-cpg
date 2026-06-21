"""Run Behave scenarios and optionally generate Allure reports."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

ALLURE_FORMATTER = "allure_behave.formatter:AllureFormatter"


def build_behave_command(
    features_dir: Path,
    results_dir: Path,
    tags: str | None = None,
) -> list[str]:
    """Build the Behave command that emits Allure result files."""
    command = [
        sys.executable,
        "-m",
        "behave",
        "-f",
        ALLURE_FORMATTER,
        "-o",
        str(results_dir),
    ]
    if tags:
        command.extend(["--tags", tags])
    command.append(str(features_dir))
    return command


def build_allure_generate_command(results_dir: Path, report_dir: Path) -> list[str]:
    """Build the Allure static report generation command."""
    return ["allure", "generate", str(results_dir), "-o", str(report_dir), "--clean"]


def build_allure_serve_command(results_dir: Path) -> list[str]:
    """Build the Allure local server command."""
    return ["allure", "serve", str(results_dir)]


def clean_path(path: Path) -> None:
    """Remove a generated file or directory if it exists."""
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def require_allure_cli() -> None:
    """Raise a clear error when the Allure CLI is unavailable."""
    if shutil.which("allure") is None:
        raise RuntimeError(
            "Allure CLI is not installed or not on PATH. Install it before using "
            "--generate or --serve."
        )


def run_command(command: Sequence[str]) -> int:
    """Run a subprocess command and return its exit code."""
    completed = subprocess.run(command, check=False)
    return completed.returncode


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run Behave BDD scenarios and emit Allure-compatible results."
    )
    parser.add_argument("--features-dir", type=Path, default=Path("features"))
    parser.add_argument("--results-dir", type=Path, default=Path("allure-results"))
    parser.add_argument("--report-dir", type=Path, default=Path("allure-report"))
    parser.add_argument("--tags", help="Behave tag expression, for example @smoke")
    parser.add_argument(
        "--keep-results",
        action="store_true",
        help="Do not delete the existing allure-results directory before running.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate a static allure-report directory after Behave succeeds.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the Allure local server after Behave succeeds.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run Behave and optional Allure report actions."""
    args = parse_args(argv)

    if not args.keep_results:
        clean_path(args.results_dir)
    if args.generate:
        clean_path(args.report_dir)

    behave_exit = run_command(
        build_behave_command(
            features_dir=args.features_dir,
            results_dir=args.results_dir,
            tags=args.tags,
        )
    )
    if behave_exit != 0:
        return behave_exit

    try:
        if args.generate:
            require_allure_cli()
            generate_exit = run_command(
                build_allure_generate_command(args.results_dir, args.report_dir)
            )
            if generate_exit != 0:
                return generate_exit
        if args.serve:
            require_allure_cli()
            return run_command(build_allure_serve_command(args.results_dir))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
