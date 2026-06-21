"""Integration tests for basic MCP query tools.

These tests require a running Neo4j instance with CPG data loaded.
Run with: pytest tests/integration/ -m integration
"""

from __future__ import annotations

import os
import mcp_server_omnicpg.tools.analysis_tools as analysis_tools
import pytest
from mcp_server_omnicpg.tools.analysis_tools import analyze_path
from mcp_server_omnicpg.tools.basic_queries import get_node_by_id, query_edges, query_nodes

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def skip_if_no_neo4j() -> None:
    """Skip integration tests if Neo4j is not configured."""
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")

    if not all([uri, user, password]):
        pytest.skip("Neo4j not configured")


class TestQueryNodes:
    """Test cases for query_nodes function."""

    def test_query_nodes_empty_params(self) -> None:
        """Test query_nodes with no filters."""
        # Returns empty list if Neo4j is connected but no matching nodes
        result = query_nodes()
        assert isinstance(result, list)

    def test_query_nodes_with_limit(self) -> None:
        """Test query_nodes with custom limit."""
        result = query_nodes(limit=5)
        assert isinstance(result, list)

    def test_query_nodes_with_node_type(self) -> None:
        """Test query_nodes with node_type filter."""
        result = query_nodes(node_type="Method")
        assert isinstance(result, list)

    def test_query_nodes_with_name(self) -> None:
        """Test query_nodes with name filter."""
        result = query_nodes(name="calculate_sum")
        assert isinstance(result, list)

    def test_query_nodes_with_file_path(self) -> None:
        """Test query_nodes with file_path filter."""
        result = query_nodes(file_path="test.py")
        assert isinstance(result, list)

    def test_query_nodes_invalid_limit(self) -> None:
        """Test query_nodes with invalid limit."""
        with pytest.raises(ValueError, match="limit must be at least 1"):
            query_nodes(limit=0)

        with pytest.raises(ValueError, match="limit must be at least 1"):
            query_nodes(limit=-1)


class TestQueryEdges:
    """Test cases for query_edges function."""

    def test_query_edges_empty_params(self) -> None:
        """Test query_edges with no filters."""
        result = query_edges()
        assert isinstance(result, list)

    def test_query_edges_with_limit(self) -> None:
        """Test query_edges with custom limit."""
        result = query_edges(limit=5)
        assert isinstance(result, list)

    def test_query_edges_with_edge_type(self) -> None:
        """Test query_edges with edge_type filter."""
        result = query_edges(edge_type="CALLS")
        assert isinstance(result, list)

    def test_query_edges_with_source_id(self) -> None:
        """Test query_edges with source_id filter."""
        result = query_edges(source_id="node-123")
        assert isinstance(result, list)

    def test_query_edges_with_target_id(self) -> None:
        """Test query_edges with target_id filter."""
        result = query_edges(target_id="node-456")
        assert isinstance(result, list)

    def test_query_edges_invalid_limit(self) -> None:
        """Test query_edges with invalid limit."""
        with pytest.raises(ValueError, match="limit must be at least 1"):
            query_edges(limit=0)


class TestGetNodeById:
    """Test cases for get_node_by_id function."""

    def test_get_node_by_id_valid(self) -> None:
        """Test get_node_by_id with valid ID."""
        result = get_node_by_id("node-123")
        # Returns None if node not found
        assert result is None or isinstance(result, dict)

    def test_get_node_by_id_empty_string(self) -> None:
        """Test get_node_by_id with empty string."""
        with pytest.raises(ValueError, match="node_id must not be empty"):
            get_node_by_id("")


class TestAnalyzePath:
    """Test cases for analyze_path policy and dispatch."""

    def test_reject_project_root_full_rebuild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project root path should be rejected to enforce incremental-only MCP policy."""
        monkeypatch.setenv("PROJECT_PATH", "/workspace")

        result = analyze_path("/workspace")

        assert result["status"] == "error"
        assert "增量分析" in result["message"]

    def test_analyze_subdirectory_calls_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """Subdirectory analysis remains allowed and must keep clear_db=False."""
        project_root = tmp_path
        subdir = project_root / "src"
        subdir.mkdir()
        monkeypatch.setenv("PROJECT_PATH", str(project_root))

        captured: dict[str, object] = {}

        def _fake_pipeline(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"status": "success", "total_nodes": 1, "total_edges": 2}

        monkeypatch.setattr(analysis_tools, "run_analysis_pipeline", _fake_pipeline)

        result = analyze_path(path=str(subdir), level="FULL", language="python")

        assert result["status"] == "success"
        assert captured["path"] == str(subdir)
        assert captured["clear_db"] is False


class TestQueryNodesNewFilters:
    """Tests for the project_id and language filters added to query_nodes."""

    def test_query_nodes_with_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id filter should be forwarded to the Cypher query."""
        import mcp_server_omnicpg.neo4j_adapter as _mod

        captured: dict[str, object] = {}

        class _FakeAdapter:
            def ensure_connected(self) -> None:
                pass

            def query(self, q: str, **kwargs: object) -> list:
                captured.update(kwargs)
                # Return nothing — we just want to verify the param was forwarded.
                return []

        monkeypatch.setattr(_mod, "_adapter", _FakeAdapter())

        result = query_nodes(project_id="proj-abc", limit=5)

        assert isinstance(result, list)
        assert captured.get("project_id") == "proj-abc"

    def test_query_nodes_with_language(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Language filter should be forwarded to the Cypher query."""
        import mcp_server_omnicpg.neo4j_adapter as _mod

        captured: dict[str, object] = {}

        class _FakeAdapter:
            def ensure_connected(self) -> None:
                pass

            def query(self, q: str, **kwargs: object) -> list:
                captured.update(kwargs)
                return []

        monkeypatch.setattr(_mod, "_adapter", _FakeAdapter())

        result = query_nodes(language="python", limit=5)

        assert isinstance(result, list)
        assert captured.get("language") == "python"

    def test_query_nodes_combined_filters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All filters should be forwarded together without conflict."""
        import mcp_server_omnicpg.neo4j_adapter as _mod

        captured: dict[str, object] = {}

        class _FakeAdapter:
            def ensure_connected(self) -> None:
                pass

            def query(self, q: str, **kwargs: object) -> list:
                captured.update(kwargs)
                return []

        monkeypatch.setattr(_mod, "_adapter", _FakeAdapter())

        result = query_nodes(
            node_type="Method",
            project_id="proj-xyz",
            language="java",
            limit=3,
        )

        assert isinstance(result, list)
        assert captured.get("project_id") == "proj-xyz"
        assert captured.get("language") == "java"
        assert captured.get("node_type") == "Method"


class TestQueryEdgesProjectIdFilter:
    """Tests for the project_id filter added to query_edges."""

    def test_query_edges_with_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id filter should be forwarded to the Cypher query."""
        import mcp_server_omnicpg.neo4j_adapter as _mod

        captured: dict[str, object] = {}

        class _FakeAdapter:
            def ensure_connected(self) -> None:
                pass

            def query(self, q: str, **kwargs: object) -> list:
                captured.update(kwargs)
                return []

        monkeypatch.setattr(_mod, "_adapter", _FakeAdapter())

        result = query_edges(project_id="proj-abc", limit=5)

        assert isinstance(result, list)
        assert captured.get("project_id") == "proj-abc"
