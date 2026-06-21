"""Integration tests for graph analysis tools.

These tests require a running Neo4j instance with CPG data loaded.
Run with: pytest tests/integration/ -m integration
"""

from __future__ import annotations

import os
import pytest
from mcp_server_omnicpg.tools.graph_analysis import (
    analyze_function,
    get_call_graph,
    get_dependencies,
    get_file_structure,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def skip_if_no_neo4j() -> None:
    """Skip integration tests if Neo4j is not configured."""
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")

    if not all([uri, user, password]):
        pytest.skip("Neo4j not configured")


class TestGetCallGraph:
    """Test cases for get_call_graph function."""

    def test_get_call_graph_all(self) -> None:
        """Test get_call_graph for all functions."""
        result = get_call_graph()
        assert isinstance(result, dict)
        assert "callers" in result
        assert "callees" in result

    def test_get_call_graph_specific(self) -> None:
        """Test get_call_graph for specific function."""
        result = get_call_graph(function_name="calculate_sum")
        assert isinstance(result, dict)
        assert "callers" in result
        assert "callees" in result

    def test_get_call_graph_with_depth(self) -> None:
        """Test get_call_graph with custom depth."""
        result = get_call_graph(depth=3)
        assert isinstance(result, dict)
        assert result["depth"] == 3

    def test_get_call_graph_callers_only(self) -> None:
        """Test get_call_graph with callers only."""
        result = get_call_graph(include_callees=False)
        assert isinstance(result, dict)
        assert len(result["callees"]) == 0

    def test_get_call_graph_callees_only(self) -> None:
        """Test get_call_graph with callees only."""
        result = get_call_graph(include_callers=False)
        assert isinstance(result, dict)
        assert len(result["callers"]) == 0

    def test_get_call_graph_invalid_depth(self) -> None:
        """Test get_call_graph with invalid depth."""
        with pytest.raises(ValueError, match="depth must be at least 1"):
            get_call_graph(depth=0)


class TestGetDependencies:
    """Test cases for get_dependencies function."""

    def test_get_dependencies_both(self) -> None:
        """Test get_dependencies with both types."""
        result = get_dependencies("node-123", dependency_type="both")
        assert isinstance(result, list)

    def test_get_dependencies_inbound(self) -> None:
        """Test get_dependencies with inbound only."""
        result = get_dependencies("node-123", dependency_type="inbound")
        assert isinstance(result, list)

    def test_get_dependencies_outbound(self) -> None:
        """Test get_dependencies with outbound only."""
        result = get_dependencies("node-123", dependency_type="outbound")
        assert isinstance(result, list)

    def test_get_dependencies_invalid_type(self) -> None:
        """Test get_dependencies with invalid dependency_type."""
        with pytest.raises(
            ValueError, match="dependency_type must be 'inbound', 'outbound', or 'both'"
        ):
            get_dependencies("node-123", dependency_type="invalid")


class TestAnalyzeFunction:
    """Test cases for analyze_function function."""

    def test_analyze_function_valid(self) -> None:
        """Test analyze_function with valid ID."""
        result = analyze_function("method-123")
        # Returns None if function not found
        assert result is None or isinstance(result, dict)

    def test_analyze_function_empty_id(self) -> None:
        """Test analyze_function with empty ID."""
        result = analyze_function("")
        assert result is None

    def test_analyze_function_nonexistent(self) -> None:
        """Test analyze_function with nonexistent ID."""
        result = analyze_function("nonexistent-function")
        assert result is None


class TestGetFileStructure:
    """Test cases for get_file_structure function."""

    def test_get_file_structure_valid(self) -> None:
        """Test get_file_structure with valid path."""
        result = get_file_structure("test.py")
        # Returns None if file not found or no data
        assert result is None or isinstance(result, dict)

    def test_get_file_structure_empty_path(self) -> None:
        """Test get_file_structure with empty path."""
        result = get_file_structure("")
        assert result is None

    def test_get_file_structure_nonexistent(self) -> None:
        """Test get_file_structure with nonexistent path."""
        result = get_file_structure("nonexistent_file.py")
        assert result is None
