"""Unit tests for the CodeSlicer (AI context extraction)."""

from __future__ import annotations

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.python_plugin import PythonPlugin
from omnicpg.slicer.code_slicer import CodeSlicer


@pytest.fixture()
def sample_cpg() -> tuple[list, list]:
    """Build a CPG from a small Python snippet for slicing tests."""
    plugin = PythonPlugin()
    source = (
        "def compute(x):\n"
        "    y = x + 1\n"
        "    z = y * 2\n"
        "    return z\n"
        "\n"
        "def caller():\n"
        "    result = compute(10)\n"
        "    return result\n"
    )
    nodes, ast_edges = plugin.parse_to_ast("test.py", source)
    cfg_edges = plugin.build_cfg(nodes)
    dfg_edges = plugin.build_dfg(nodes, cfg_edges)
    call_edges = plugin.build_call_graph(nodes)
    all_edges = ast_edges + cfg_edges + dfg_edges + call_edges
    return nodes, all_edges


class TestCodeSlicer:
    """Tests for :class:`CodeSlicer`."""

    def test_find_node_by_name(self, sample_cpg: tuple[list, list]) -> None:
        """Should find function nodes by name."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        found = slicer.find_node_by_name("compute")
        assert len(found) == 1
        assert found[0].has_label("Method")

    def test_find_node_by_property(self, sample_cpg: tuple[list, list]) -> None:
        """Should find nodes by arbitrary property."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        modules = slicer.find_node_by_property("type", "module")
        assert len(modules) == 1

    def test_backward_slice_returns_ancestors(self, sample_cpg: tuple[list, list]) -> None:
        """Backward slice should trace data/control dependencies."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        # Find a node that has incoming edges.
        # Pick a node that has REACHES or FLOWS_TO incoming edges.
        target_nodes = [
            n
            for n in nodes
            if n.properties.get("type") == "identifier" and n.properties.get("code") == "z"
        ]
        if not target_nodes:
            pytest.skip("No 'z' identifier node found")

        slice_nodes, _slice_edges = slicer.backward_slice(target_nodes[0].id, max_nodes=50)
        assert len(slice_nodes) >= 1  # At least the starting node.

    def test_forward_slice_returns_dependents(self, sample_cpg: tuple[list, list]) -> None:
        """Forward slice should find nodes affected by a definition."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        # Find the function definition node for 'compute'.
        compute_nodes = slicer.find_node_by_name("compute")
        assert len(compute_nodes) >= 1

        slice_nodes, _slice_edges = slicer.forward_slice(compute_nodes[0].id, max_nodes=50)
        assert len(slice_nodes) >= 1

    def test_neighbourhood_returns_nearby(self, sample_cpg: tuple[list, list]) -> None:
        """Neighbourhood should include nodes within N hops."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        compute = slicer.find_node_by_name("compute")
        assert len(compute) >= 1

        neigh_nodes, _neigh_edges = slicer.neighbourhood(compute[0].id, max_hops=2, max_nodes=100)
        assert len(neigh_nodes) >= 1

    def test_max_nodes_budget(self, sample_cpg: tuple[list, list]) -> None:
        """Slicing should respect the max_nodes budget."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        # Use a very small budget.
        compute = slicer.find_node_by_name("compute")
        assert len(compute) >= 1

        slice_nodes, _ = slicer.neighbourhood(compute[0].id, max_hops=10, max_nodes=3)
        assert len(slice_nodes) <= 3

    def test_render_slice_produces_text(self, sample_cpg: tuple[list, list]) -> None:
        """render_slice should produce a non-empty string."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        compute = slicer.find_node_by_name("compute")
        assert len(compute) >= 1

        slice_nodes, _ = slicer.neighbourhood(compute[0].id, max_hops=1, max_nodes=20)
        text = slicer.render_slice(slice_nodes)
        assert isinstance(text, str)
        assert len(text) > 0
        assert "test.py" in text  # File path should appear.

    def test_render_slice_without_metadata(self, sample_cpg: tuple[list, list]) -> None:
        """render_slice with include_metadata=False omits annotations."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        compute = slicer.find_node_by_name("compute")
        assert len(compute) >= 1

        slice_nodes, _ = slicer.neighbourhood(compute[0].id, max_hops=1, max_nodes=20)
        text = slicer.render_slice(slice_nodes, include_metadata=False)
        assert "# ---" not in text  # No file header.

    def test_backward_slice_custom_edge_types(self, sample_cpg: tuple[list, list]) -> None:
        """Backward slice should respect custom edge_types parameter."""
        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        target = slicer.find_node_by_name("compute")
        if not target:
            pytest.skip("No compute node")

        # Only follow PARENT_OF edges.
        _slice_nodes, slice_edges = slicer.backward_slice(
            target[0].id,
            edge_types=frozenset({EdgeType.PARENT_OF}),
        )
        for edge in slice_edges:
            assert edge.edge_type == EdgeType.PARENT_OF

    def test_empty_cpg(self) -> None:
        """CodeSlicer should handle empty node/edge lists gracefully."""
        slicer = CodeSlicer([], [])
        found = slicer.find_node_by_name("anything")
        assert found == []

    def test_expand_method_to_neo4j_exception_handling(
        self, sample_cpg: tuple[list, list], caplog: pytest.LogCaptureFixture
    ) -> None:
        """expand_method_to_neo4j should return nodes and edges even if Neo4j insertion fails."""
        import types
        from unittest.mock import MagicMock

        from omnicpg.interfaces.language_plugin import LanguagePlugin
        from omnicpg.models.node import CPGNode

        nodes, edges = sample_cpg
        slicer = CodeSlicer(nodes, edges)

        # Find a method to expand
        compute_nodes = slicer.find_node_by_name("compute")
        assert len(compute_nodes) >= 1
        original_node = compute_nodes[0]
        method_id = original_node.id

        # We need to recreate the node with the source_code property since it's immutable
        new_props = dict(original_node.properties)
        new_props["source_code"] = "def compute(x): return x"

        # Create new node
        new_node = CPGNode(
            id=original_node.id,
            labels=original_node.labels,
            properties=types.MappingProxyType(new_props),
        )

        # Update node in the slicer map
        slicer._node_map[method_id] = new_node

        # Mock the language plugin
        mock_plugin = MagicMock(spec=LanguagePlugin)

        # Mock parse_to_ast to return some dummy nodes and edges
        mock_plugin.parse_to_ast.return_value = (nodes[:1], edges[:1])
        mock_plugin.build_cfg.return_value = edges[1:2]
        mock_plugin.build_dfg.return_value = edges[2:3]

        # Mock the Neo4j adapter
        mock_neo4j_adapter = MagicMock()
        mock_neo4j_adapter.check_method_expanded.return_value = False

        # Configure the adapter to raise an exception when inserting nodes
        error_msg = "Database connection lost"
        mock_neo4j_adapter.insert_nodes_incremental.side_effect = Exception(error_msg)

        # Call the method
        expanded_nodes, expanded_edges = slicer.expand_method_to_neo4j(
            node_id=method_id,
            plugin=mock_plugin,
            neo4j_adapter=mock_neo4j_adapter,
        )

        # Verify the exception was caught and logged
        assert "Failed to store expanded method to Neo4j" in caplog.text
        assert error_msg in caplog.text

        # Verify the method still returned the expanded nodes and edges
        assert len(expanded_nodes) == 1
        assert len(expanded_edges) == 3  # ast_edges(1) + cfg_edges(1) + dfg_edges(1)
        assert mock_neo4j_adapter.insert_nodes_incremental.call_count == 1
        # insert_edges should not be called because insert_nodes raised an exception
        assert mock_neo4j_adapter.insert_edges_incremental.call_count == 0
