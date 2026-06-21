"""Integration tests for path query tools.

These tests require a running Neo4j instance with CPG data loaded.
Run with: pytest tests/integration/ -m integration
"""

from __future__ import annotations

import pytest
from mcp_server_omnicpg.tools.path_queries import find_control_flow, find_data_flow, find_path

pytestmark = pytest.mark.integration


class TestFindPath:
    """Test cases for find_path function."""

    def test_find_path_basic(self) -> None:
        """Test find_path with basic parameters."""
        result = find_path("node-1", "node-2")
        assert isinstance(result, list)

    def test_find_path_with_depth(self) -> None:
        """Test find_path with custom max_depth."""
        result = find_path("node-1", "node-2", max_depth=3)
        assert isinstance(result, list)

    def test_find_path_with_relationship_types(self) -> None:
        """Test find_path with relationship type filter."""
        result = find_path("node-1", "node-2", relationship_types="CALLS,DEFINES")
        assert isinstance(result, list)

    def test_find_path_invalid_depth(self) -> None:
        """Test find_path with invalid max_depth."""
        with pytest.raises(ValueError, match="max_depth must be at least 1"):
            find_path("node-1", "node-2", max_depth=0)

        with pytest.raises(ValueError, match="max_depth must be at least 1"):
            find_path("node-1", "node-2", max_depth=-1)


class TestFindDataFlow:
    """Test cases for find_data_flow function."""

    def test_find_data_flow_basic(self) -> None:
        """Test find_data_flow with basic parameters."""
        result = find_data_flow("var-1", "method-2")
        assert isinstance(result, list)

    def test_find_data_flow_with_depth(self) -> None:
        """Test find_data_flow with custom max_depth."""
        result = find_data_flow("var-1", "method-2", max_depth=3)
        assert isinstance(result, list)

    def test_find_data_flow_invalid_depth(self) -> None:
        """Test find_data_flow with invalid max_depth."""
        with pytest.raises(ValueError, match="max_depth must be at least 1"):
            find_data_flow("var-1", "method-2", max_depth=0)


class TestFindControlFlow:
    """Test cases for find_control_flow function."""

    def test_find_control_flow_basic(self) -> None:
        """Test find_control_flow with basic parameters."""
        result = find_control_flow("stmt-1", "stmt-2")
        assert isinstance(result, list)

    def test_find_control_flow_with_depth(self) -> None:
        """Test find_control_flow with custom max_depth."""
        result = find_control_flow("stmt-1", "stmt-2", max_depth=3)
        assert isinstance(result, list)

    def test_find_control_flow_invalid_depth(self) -> None:
        """Test find_control_flow with invalid max_depth."""
        with pytest.raises(ValueError, match="max_depth must be at least 1"):
            find_control_flow("stmt-1", "stmt-2", max_depth=0)
