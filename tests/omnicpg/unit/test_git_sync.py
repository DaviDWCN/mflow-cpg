"""Tests for the sync_git_changes tool."""

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp_server_omnicpg.tools.analysis_tools import sync_git_changes, verify_graph_sync


@pytest.fixture
def mock_run_analysis_pipeline() -> Any:
    """Mock the run_analysis_pipeline function to prevent actual analysis."""
    with patch("mcp_server_omnicpg.tools.analysis_tools.run_analysis_pipeline") as mock:
        mock.return_value = {"status": "success", "total_nodes": 10, "total_edges": 15}
        yield mock


@patch("mcp_server_omnicpg.tools.analysis_tools.subprocess.run")
def test_sync_git_changes_success(
    mock_subprocess_run: MagicMock, mock_run_analysis_pipeline: MagicMock
) -> None:
    """Test successful git diff parsing and pipeline triggering."""
    # Mock git diff output with Modifications, Additions, Deletions, and Renames
    mock_stdout = (
        "M\tsrc/modified.py\nA\tsrc/added.py\nD\tsrc/deleted.py\nR100\tsrc/old.py\tsrc/new.py"
    )
    mock_result = MagicMock()
    mock_result.stdout = mock_stdout
    mock_subprocess_run.return_value = mock_result

    with patch.dict("os.environ", {"PROJECT_PATH": "/mock_workspace"}):
        result = sync_git_changes(commit_from="HEAD~1", commit_to="HEAD")

    assert result["status"] == "success"
    assert mock_subprocess_run.call_count == 1

    mock_run_analysis_pipeline.assert_called_once()
    kwargs = mock_run_analysis_pipeline.call_args[1]

    # Assert proper parsing of git diff --name-status
    assert "/mock_workspace/src/modified.py" in kwargs["specific_files"]
    assert "/mock_workspace/src/added.py" in kwargs["specific_files"]
    assert "/mock_workspace/src/new.py" in kwargs["specific_files"]

    assert "/mock_workspace/src/deleted.py" in kwargs["deleted_files"]
    assert "/mock_workspace/src/old.py" in kwargs["deleted_files"]


@patch("mcp_server_omnicpg.tools.analysis_tools.subprocess.run")
def test_sync_git_changes_no_changes(
    mock_subprocess_run: MagicMock, mock_run_analysis_pipeline: MagicMock
) -> None:
    """Test when git diff returns empty."""
    mock_result = MagicMock()
    mock_result.stdout = "\n"
    mock_subprocess_run.return_value = mock_result

    with patch.dict("os.environ", {"PROJECT_PATH": "/mock_workspace"}):
        result = sync_git_changes(commit_from="HEAD~1", commit_to="HEAD")

    assert result["status"] == "success"
    assert result["message"] == "No relevant git changes found."
    assert result["total_nodes"] == 0
    mock_run_analysis_pipeline.assert_not_called()


@patch("mcp_server_omnicpg.tools.analysis_tools.subprocess.run")
def test_sync_git_changes_git_failure(
    mock_subprocess_run: MagicMock, mock_run_analysis_pipeline: MagicMock
) -> None:
    """Test handling of subprocess failure (e.g. invalid git repository)."""
    mock_subprocess_run.side_effect = subprocess.CalledProcessError(1, "git")

    with patch.dict("os.environ", {"PROJECT_PATH": "/mock_workspace"}):
        result = sync_git_changes(commit_from="invalid_commit", commit_to="HEAD")

    assert result["status"] == "error"
    assert "Failed to get git changes" in result["message"]
    mock_run_analysis_pipeline.assert_not_called()


def test_sync_git_changes_invalid_level() -> None:
    """Test handling of invalid analysis level."""
    result = sync_git_changes(level="INVALID_LEVEL")
    assert result["status"] == "error"
    assert "Invalid analysis level" in result["message"]


@patch("mcp_server_omnicpg.tools.analysis_tools.get_adapter")
@patch("mcp_server_omnicpg.tools.analysis_tools.subprocess.run")
def test_verify_graph_sync_success(
    mock_subprocess_run: MagicMock, mock_get_adapter: MagicMock
) -> None:
    """Test successful verification where Git matches Neo4j, plus some diffs."""
    # Setup git mock
    mock_result = MagicMock()
    # Mixed extensions, some supported, some not.
    mock_result.stdout = "src/main.py\nsrc/app.java\nsrc/config.json\n"
    mock_subprocess_run.return_value = mock_result

    # Setup neo4j mock
    mock_adapter = MagicMock()
    mock_get_adapter.return_value = mock_adapter
    # Simulate a scenario where Neo4j has main.py (matching) and old.py (ghost)
    # while app.java is in Git but missing in Neo4j.
    mock_adapter.query.return_value = [
        {"file_path": "/mock_workspace/src/main.py"},
        {"file_path": "/mock_workspace/src/old.py"},
    ]

    with patch.dict("os.environ", {"PROJECT_PATH": "/mock_workspace"}):
        result = verify_graph_sync("proj-123")

    assert result["status"] == "success"
    assert result["tracked_files_count"] == 2  # main.py, app.java
    assert result["indexed_files_count"] == 2  # main.py, old.py

    assert result["missing_files_count"] == 1
    assert "/mock_workspace/src/app.java" in result["missing_files_sample"]

    assert result["ghost_files_count"] == 1
    assert "/mock_workspace/src/old.py" in result["ghost_files_sample"]

    assert result["is_synced"] is False


@patch("mcp_server_omnicpg.tools.analysis_tools.subprocess.run")
def test_verify_graph_sync_git_failure(mock_subprocess_run: MagicMock) -> None:
    """Test handling of subprocess failure for git ls-files."""
    mock_subprocess_run.side_effect = subprocess.CalledProcessError(1, "git")

    with patch.dict("os.environ", {"PROJECT_PATH": "/mock_workspace"}):
        result = verify_graph_sync("proj-123")

    assert result["status"] == "error"
    assert "Failed to list git files" in result["message"]


@patch("mcp_server_omnicpg.tools.analysis_tools.get_adapter")
@patch("mcp_server_omnicpg.tools.analysis_tools.subprocess.run")
def test_verify_graph_sync_neo4j_failure(
    mock_subprocess_run: MagicMock, mock_get_adapter: MagicMock
) -> None:
    """Test handling of Neo4j query failure."""
    mock_result = MagicMock()
    mock_result.stdout = "src/main.py\n"
    mock_subprocess_run.return_value = mock_result

    mock_adapter = MagicMock()
    mock_get_adapter.return_value = mock_adapter
    mock_adapter.query.side_effect = Exception("Neo4j disconnected")

    with patch.dict("os.environ", {"PROJECT_PATH": "/mock_workspace"}):
        result = verify_graph_sync("proj-123")

    assert result["status"] == "error"
    assert "Failed to query Neo4j" in result["message"]
