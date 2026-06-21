"""Integration tests for MCP Neo4j server.

These tests require a running Neo4j instance with CPG data.
"""

from __future__ import annotations

import os

import pytest

# Only run these tests if marked as integration
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def skip_if_no_neo4j() -> None:
    """Skip integration tests if Neo4j is not configured."""
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")

    if not all([uri, user, password]):
        pytest.skip("Neo4j not configured")


class TestMCPNeo4jAdapter:
    """Integration tests for MCPNeo4jAdapter."""

    def test_connection(self) -> None:
        """Test that we can connect to Neo4j."""
        from mcp_server_omnicpg.neo4j_adapter import get_adapter

        adapter = get_adapter()
        adapter.connect()
        assert adapter.is_connected()

    def test_simple_query(self) -> None:
        """Test that we can execute a simple query."""
        from mcp_server_omnicpg.neo4j_adapter import get_adapter

        adapter = get_adapter()
        result = adapter.query("MATCH (n) RETURN count(n) AS count LIMIT 1")
        assert isinstance(result, list)
        if result:
            assert "count" in result[0]


class TestBasicQueries:
    """Integration tests for basic query tools."""

    def test_query_nodes(self) -> None:
        """Test querying nodes."""
        from mcp_server_omnicpg.tools import query_nodes

        result = query_nodes(limit=5)
        assert isinstance(result, list)

    def test_query_edges(self) -> None:
        """Test querying edges."""
        from mcp_server_omnicpg.tools import query_edges

        result = query_edges(limit=5)
        assert isinstance(result, list)


class TestPathQueries:
    """Integration tests for path query tools."""

    def test_find_path(self) -> None:
        """Test finding paths."""
        from mcp_server_omnicpg.tools import find_path

        # Use any two node IDs that exist
        result = find_path("node-1", "node-2", max_depth=2)
        assert isinstance(result, list)


class TestGraphAnalysis:
    """Integration tests for graph analysis tools."""

    def test_get_call_graph(self) -> None:
        """Test getting call graph."""
        from mcp_server_omnicpg.tools import get_call_graph

        result = get_call_graph()
        assert isinstance(result, dict)
        assert "callers" in result
        assert "callees" in result

    def test_get_dependencies(self) -> None:
        """Test getting dependencies."""
        from mcp_server_omnicpg.tools import get_dependencies

        # Use any node ID that exists
        result = get_dependencies("node-1")
        assert isinstance(result, list)
