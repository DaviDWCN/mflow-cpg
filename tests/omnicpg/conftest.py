"""Shared test configuration and fixtures for OmniCPG."""

from pathlib import Path

import pytest


@pytest.fixture()
def fixtures_dir() -> Path:
    """Return the path to the test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture()
def sample_python_dir(fixtures_dir: Path) -> Path:
    """Return the path to the sample Python fixtures directory."""
    return fixtures_dir / "sample_python"


@pytest.fixture()
def sample_java_dir(fixtures_dir: Path) -> Path:
    """Return the path to the sample Java fixtures directory."""
    return fixtures_dir / "sample_java"
