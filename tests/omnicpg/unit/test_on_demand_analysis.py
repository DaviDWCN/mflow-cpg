"""Unit tests for on-demand CPG analysis MCP tools.

Tests cover:
- Tool registration in the MCP server
- MCPNeo4jAdapter write helper methods
- expand_method_on_demand logic
- Language auto-detection for method expansion
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

from omnicpg.models.edge import CPGEdge, EdgeType

# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Verify that on-demand analysis tools are registered in the MCP server."""

    def _get_tool_names(self) -> list[str]:
        """Return tool names from the MCP server's list_tools handler."""
        from mcp_server_omnicpg.mcp_server import list_tools

        tools = asyncio.run(list_tools())
        return [t.name for t in tools]

    def test_expand_method_on_demand_registered(self) -> None:
        """expand_method_on_demand should appear in the tool list."""
        assert "expand_method_on_demand" in self._get_tool_names()

    def test_get_expansion_stats_registered(self) -> None:
        """get_expansion_stats should appear in the tool list."""
        assert "get_expansion_stats" in self._get_tool_names()

    def test_find_data_flow_with_auto_expand_registered(self) -> None:
        """find_data_flow_with_auto_expand should appear in the tool list."""
        assert "find_data_flow_with_auto_expand" in self._get_tool_names()

    def test_find_control_flow_with_auto_expand_registered(self) -> None:
        """find_control_flow_with_auto_expand should appear in the tool list."""
        assert "find_control_flow_with_auto_expand" in self._get_tool_names()

    def test_analyze_path_registered(self) -> None:
        """analyze_path should appear in the tool list."""
        assert "analyze_path" in self._get_tool_names()

    def test_total_tool_count(self) -> None:
        """At least 27 tools should be registered."""
        from mcp_server_omnicpg.mcp_server import list_tools

        tools = asyncio.run(list_tools())
        assert len(tools) >= 27


# ---------------------------------------------------------------------------
# Language auto-detection
# ---------------------------------------------------------------------------


class TestLanguageAutoDetection:
    """Verify _get_plugin_for_file selects the correct plugin."""

    def test_python_file(self) -> None:
        """A .py file should yield PythonPlugin."""
        from mcp_server_omnicpg.tools.auto_expansion import _get_plugin_for_file

        plugin = _get_plugin_for_file("src/main.py")
        assert plugin.__class__.__name__ == "PythonPlugin"

    def test_java_file(self) -> None:
        """A .java file should yield JavaPlugin."""
        from mcp_server_omnicpg.tools.auto_expansion import _get_plugin_for_file

        plugin = _get_plugin_for_file("src/Main.java")
        assert plugin.__class__.__name__ == "JavaPlugin"

    def test_jsp_file(self) -> None:
        """A .jsp file should yield JavaPlugin."""
        from mcp_server_omnicpg.tools.auto_expansion import _get_plugin_for_file

        plugin = _get_plugin_for_file("web/index.jsp")
        assert plugin.__class__.__name__ == "JavaPlugin"

    def test_xml_file(self) -> None:
        """A .xml file should yield JavaPlugin."""
        from mcp_server_omnicpg.tools.auto_expansion import _get_plugin_for_file

        plugin = _get_plugin_for_file("config/beans.xml")
        assert plugin.__class__.__name__ == "JavaPlugin"

    def test_unknown_extension_defaults_to_python(self) -> None:
        """An unrecognised extension should default to PythonPlugin."""
        from mcp_server_omnicpg.tools.auto_expansion import _get_plugin_for_file

        plugin = _get_plugin_for_file("script.rb")
        assert plugin.__class__.__name__ == "PythonPlugin"

    def test_expanded_marker_defaults_to_python(self) -> None:
        """The '<expanded>' placeholder should default to PythonPlugin."""
        from mcp_server_omnicpg.tools.auto_expansion import _get_plugin_for_file

        plugin = _get_plugin_for_file("<expanded>")
        assert plugin.__class__.__name__ == "PythonPlugin"


# ---------------------------------------------------------------------------
# MCPNeo4jAdapter write helpers (unit tests with mocks)
# ---------------------------------------------------------------------------


class TestMCPAdapterWriteHelpers:
    """Test MCPNeo4jAdapter write helper methods with mocked driver."""

    def _make_adapter(self):
        """Create an MCPNeo4jAdapter with a mocked driver."""
        from mcp_server_omnicpg.neo4j_adapter import MCPNeo4jAdapter

        # Reset singleton for test isolation
        MCPNeo4jAdapter._instance = None
        adapter = MCPNeo4jAdapter()
        adapter._driver = MagicMock()
        adapter._connected = True
        return adapter

    def test_check_method_expanded_true(self) -> None:
        """Return True when the method node has expanded=true."""
        adapter = self._make_adapter()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([{"expanded": True}]))
        mock_session.run.return_value = mock_result
        adapter._driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        adapter._driver.session.return_value.__exit__ = MagicMock(return_value=False)

        assert adapter.check_method_expanded("method-123") is True

    def test_check_method_expanded_false_when_not_found(self) -> None:
        """Return False when the method node does not exist."""
        adapter = self._make_adapter()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.run.return_value = mock_result
        adapter._driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        adapter._driver.session.return_value.__exit__ = MagicMock(return_value=False)

        assert adapter.check_method_expanded("nonexistent") is False

    def test_mark_method_expanded_calls_write(self) -> None:
        """mark_method_expanded should issue a SET query."""
        adapter = self._make_adapter()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.run.return_value = mock_result
        adapter._driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        adapter._driver.session.return_value.__exit__ = MagicMock(return_value=False)

        adapter.mark_method_expanded("method-456")
        mock_session.run.assert_called_once()
        call_args = mock_session.run.call_args
        assert "SET m.expanded = true" in call_args[0][0]

    def test_insert_cpg_nodes_empty(self) -> None:
        """Inserting an empty list of nodes should not raise."""
        adapter = self._make_adapter()
        adapter.insert_cpg_nodes([])

    def test_insert_cpg_edges_empty(self) -> None:
        """Inserting an empty list of edges should not raise."""
        adapter = self._make_adapter()
        adapter.insert_cpg_edges([])

    def test_query_handles_direct_rows_without_apoc_wrapper(self) -> None:
        """Direct Cypher-style rows should still be returned correctly."""
        adapter = self._make_adapter()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([{"expanded": True}]))
        mock_session.run.return_value = mock_result
        adapter._driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        adapter._driver.session.return_value.__exit__ = MagicMock(return_value=False)

        result = adapter.query("MATCH (n) RETURN n.expanded AS expanded")

        assert result == [{"expanded": True}]

    def test_insert_cpg_edges_rejects_invalid_edge_type(self) -> None:
        """Cypher relationship types must be validated before query interpolation."""
        adapter = self._make_adapter()
        invalid_edge = MagicMock()
        invalid_edge.edge_type = "CALLS DELETE n"
        invalid_edge.source_id = "src"
        invalid_edge.target_id = "dst"
        invalid_edge.properties = {}
        with pytest.raises(ValueError, match="Invalid edge_type"):
            adapter.insert_cpg_edges([invalid_edge])

    def test_insert_cpg_edges_adds_project_id_to_properties(self) -> None:
        """Project-scoped edge writes should carry the project_id property."""
        adapter = self._make_adapter()
        mock_session = MagicMock()
        adapter._driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        adapter._driver.session.return_value.__exit__ = MagicMock(return_value=False)

        adapter.insert_cpg_edges(
            [
                CPGEdge(
                    source_id="src",
                    target_id="dst",
                    edge_type=EdgeType.CALLS,
                    properties=MappingProxyType({"callee": "foo"}),
                )
            ],
            project_id="proj-123",
        )

        assert mock_session.run.call_count == 1
        project_id = mock_session.run.call_args.kwargs["batch"][0]["properties"]["project_id"]
        assert project_id == "proj-123"


# ---------------------------------------------------------------------------
# expand_method_on_demand logic tests
# ---------------------------------------------------------------------------


class TestExpandMethodOnDemand:
    """Test expand_method_on_demand with mocked Neo4j."""

    @patch("mcp_server_omnicpg.tools.auto_expansion.get_adapter")
    def test_already_expanded(self, mock_get_adapter) -> None:
        """Return 'already_expanded' when the method is already expanded."""
        mock_adapter = MagicMock()
        mock_adapter.check_method_expanded.return_value = True
        mock_get_adapter.return_value = mock_adapter

        from mcp_server_omnicpg.tools.auto_expansion import expand_method_on_demand

        result = expand_method_on_demand("method-already-done")
        assert result["status"] == "already_expanded"

    @patch("mcp_server_omnicpg.tools.auto_expansion.get_adapter")
    def test_method_not_found(self, mock_get_adapter) -> None:
        """Return 'not_found' when no method matches the given ID."""
        mock_adapter = MagicMock()
        mock_adapter.check_method_expanded.return_value = False
        mock_adapter.query.return_value = []
        mock_get_adapter.return_value = mock_adapter

        from mcp_server_omnicpg.tools.auto_expansion import expand_method_on_demand

        result = expand_method_on_demand("nonexistent-id")
        assert result["status"] == "not_found"

    @patch("mcp_server_omnicpg.tools.auto_expansion.get_adapter")
    def test_no_source_code(self, mock_get_adapter) -> None:
        """Return 'no_source_code' when the method has no source_code property."""
        mock_adapter = MagicMock()
        mock_adapter.check_method_expanded.return_value = False
        mock_adapter.query.return_value = [
            {"source_code": None, "file_path": "test.py", "name": "foo"}
        ]
        mock_get_adapter.return_value = mock_adapter

        from mcp_server_omnicpg.tools.auto_expansion import expand_method_on_demand

        result = expand_method_on_demand("no-source-id")
        assert result["status"] == "no_source_code"

    @patch("mcp_server_omnicpg.tools.auto_expansion.get_adapter")
    def test_successful_expansion(self, mock_get_adapter) -> None:
        """A method with source_code should be expanded and stored."""
        mock_adapter = MagicMock()
        mock_adapter.check_method_expanded.return_value = False
        mock_adapter.query.return_value = [
            {
                "source_code": "def hello():\n    return 42\n",
                "file_path": "test.py",
                "name": "hello",
            }
        ]
        mock_adapter.insert_cpg_nodes = MagicMock()
        mock_adapter.insert_cpg_edges = MagicMock()
        mock_adapter.mark_method_expanded = MagicMock()
        mock_get_adapter.return_value = mock_adapter

        from mcp_server_omnicpg.tools.auto_expansion import expand_method_on_demand

        result = expand_method_on_demand("method-to-expand")

        assert result["status"] == "success"
        assert result["method_name"] == "hello"
        assert result["nodes_count"] > 0
        assert result["edges_count"] >= 0
        mock_adapter.insert_cpg_nodes.assert_called_once()
        mock_adapter.insert_cpg_edges.assert_called_once()
        mock_adapter.mark_method_expanded.assert_called_once_with("method-to-expand")

    @patch("mcp_server_omnicpg.tools.auto_expansion.get_adapter")
    def test_expansion_error_handling(self, mock_get_adapter) -> None:
        """Return 'error' when node insertion fails."""
        mock_adapter = MagicMock()
        mock_adapter.check_method_expanded.return_value = False
        mock_adapter.query.return_value = [
            {
                "source_code": "def hello():\n    return 42\n",
                "file_path": "test.py",
                "name": "hello",
            }
        ]
        mock_adapter.insert_cpg_nodes.side_effect = RuntimeError("DB error")
        mock_get_adapter.return_value = mock_adapter

        from mcp_server_omnicpg.tools.auto_expansion import expand_method_on_demand

        result = expand_method_on_demand("method-fails")
        assert result["status"] == "error"
        assert "DB error" in result["message"]


# ---------------------------------------------------------------------------
# analyze_path tests
# ---------------------------------------------------------------------------


class TestAnalyzePath:
    """Test analyze_path parameter handling."""

    @patch("mcp_server_omnicpg.tools.analysis_tools.run_analysis_pipeline")
    def test_relative_path_resolved(self, mock_pipeline) -> None:
        """A relative path should be joined with PROJECT_PATH."""
        mock_pipeline.return_value = {
            "status": "success",
            "total_nodes": 10,
            "total_edges": 5,
        }

        from mcp_server_omnicpg.tools.analysis_tools import analyze_path

        with patch.dict("os.environ", {"PROJECT_PATH": "/workspace"}):
            result = analyze_path("src/main.py", level="FULL")

        call_args = mock_pipeline.call_args
        assert call_args.kwargs["path"] == "/workspace/src/main.py"
        assert result["status"] == "success"

    @patch("mcp_server_omnicpg.tools.analysis_tools.run_analysis_pipeline")
    def test_absolute_path_unchanged(self, mock_pipeline) -> None:
        """An absolute path should be passed through unchanged."""
        mock_pipeline.return_value = {
            "status": "success",
            "total_nodes": 10,
            "total_edges": 5,
        }

        from mcp_server_omnicpg.tools.analysis_tools import analyze_path

        analyze_path("/absolute/path/to/file.py", level="ARCHITECTURAL")

        call_args = mock_pipeline.call_args
        assert call_args.kwargs["path"] == "/absolute/path/to/file.py"

    def test_invalid_level_returns_error(self) -> None:
        """An invalid analysis level should return an error dict."""
        from mcp_server_omnicpg.tools.analysis_tools import analyze_path

        result = analyze_path("/some/path", level="INVALID_LEVEL")
        assert result["status"] == "error"
        assert "Invalid analysis level" in result["message"]
