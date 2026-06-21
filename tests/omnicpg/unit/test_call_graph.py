"""Unit tests for the CallGraphBuilder (cross-file CALLS edges)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator
from omnicpg.plugins.python_plugin import PythonPlugin
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.python_plugin.call_graph_builder import CallGraphBuilder

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def call_graph_builder() -> CallGraphBuilder:
    """Return a fresh CallGraphBuilder."""
    return CallGraphBuilder()


@pytest.fixture()
def ast_builder() -> ASTBuilder:
    """Return a fresh ASTBuilder."""
    return ASTBuilder()


class TestCallGraphBuilder:
    """Tests for :class:`CallGraphBuilder`."""

    def test_single_file_call(
        self, ast_builder: ASTBuilder, call_graph_builder: CallGraphBuilder
    ) -> None:
        """A call within the same file should generate a CALLS edge."""
        source = "def helper():\n    return 42\n\ndef main():\n    x = helper()\n    return x\n"
        nodes, _ = ast_builder.build("test.py", source)
        edges = call_graph_builder.build(nodes)

        assert len(edges) > 0
        assert all(e.edge_type == EdgeType.CALLS for e in edges)
        # At least one edge should reference "helper" as the callee.
        callee_names = {str(e.properties.get("callee")) for e in edges}
        assert "helper" in callee_names

    def test_cross_file_call(
        self, ast_builder: ASTBuilder, call_graph_builder: CallGraphBuilder
    ) -> None:
        """Calls across two files should produce CALLS edges."""
        utils_source = "def format_name(name):\n    return name.upper()\n"
        service_source = (
            "def greet(name):\n    formatted = format_name(name)\n    return formatted\n"
        )
        utils_nodes, _ = ast_builder.build("utils.py", utils_source)
        service_nodes, _ = ast_builder.build("service.py", service_source)

        all_nodes = utils_nodes + service_nodes
        edges = call_graph_builder.build(all_nodes)

        # There should be a CALLS edge from service.py to utils.py's format_name
        cross_file_edges = [
            e
            for e in edges
            if e.properties.get("callee") == "format_name"
            and "service.py" in str(e.properties.get("caller_file", ""))
            and "utils.py" in str(e.properties.get("target_file", ""))
        ]
        assert len(cross_file_edges) >= 1

    def test_no_calls_yields_empty(
        self, ast_builder: ASTBuilder, call_graph_builder: CallGraphBuilder
    ) -> None:
        """Code with no function calls should produce no CALLS edges."""
        source = "x = 1\ny = 2\n"
        nodes, _ = ast_builder.build("test.py", source)
        edges = call_graph_builder.build(nodes)
        assert edges == []

    def test_all_edges_are_calls(
        self, ast_builder: ASTBuilder, call_graph_builder: CallGraphBuilder
    ) -> None:
        """All edges from the call graph builder must be of type CALLS."""
        source = "def foo():\n    return bar()\n\ndef bar():\n    return 1\n"
        nodes, _ = ast_builder.build("test.py", source)
        edges = call_graph_builder.build(nodes)
        assert all(e.edge_type == EdgeType.CALLS for e in edges)

    def test_orchestrator_includes_calls_edges(self) -> None:
        """The orchestrator should include CALLS edges in the output."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "a.py").write_text("def helper():\n    return 1\n")
            (Path(tmpdir) / "b.py").write_text("def main():\n    x = helper()\n    return x\n")
            orchestrator = ProjectOrchestrator(plugins=[PythonPlugin()])
            _nodes, edges = orchestrator.analyze(tmpdir)

            edge_types = {e.edge_type for e in edges}
            assert EdgeType.CALLS in edge_types

    def test_multi_file_fixture(self, sample_python_dir: Path) -> None:
        """End-to-end: analyse the multi-file fixture directory."""
        multi_dir = sample_python_dir.parent / "multi_file_python"
        orchestrator = ProjectOrchestrator(plugins=[PythonPlugin()])
        nodes, edges = orchestrator.analyze(str(multi_dir))

        # Should have nodes from both files.
        files_seen = {str(n.properties.get("file_path", "")) for n in nodes}
        assert any("utils.py" in f for f in files_seen)
        assert any("service.py" in f for f in files_seen)

        # Should have CALLS edges.
        calls_edges = [e for e in edges if e.edge_type == EdgeType.CALLS]
        assert len(calls_edges) > 0


class TestMethodToMethodCalls:
    """Tests that CALLS edges connect Method→Method when edges are provided."""

    def test_calls_source_is_method_with_edges(self) -> None:
        """When all_edges (PARENT_OF) are passed, CALLS source should be a Method node."""
        ast_builder = ASTBuilder()
        builder = CallGraphBuilder()
        source = "def helper():\n    return 42\n\ndef main():\n    x = helper()\n    return x\n"
        nodes, ast_edges = ast_builder.build("test.py", source)
        edges = builder.build(nodes, ast_edges)

        assert len(edges) > 0
        node_index = {n.id: n for n in nodes}
        for edge in edges:
            source_node = node_index[edge.source_id]
            target_node = node_index[edge.target_id]
            assert source_node.has_label("Method"), (
                f"CALLS source should be Method, got {source_node.labels}"
            )
            assert target_node.has_label("Method"), (
                f"CALLS target should be Method, got {target_node.labels}"
            )
            # Should have caller property
            assert edge.properties.get("caller") == "main"
            assert edge.properties.get("callee") == "helper"

    def test_calls_deduplicated(self) -> None:
        """Multiple calls to the same function should produce one Method→Method edge."""
        ast_builder = ASTBuilder()
        builder = CallGraphBuilder()
        source = (
            "def helper():\n"
            "    return 42\n"
            "\n"
            "def main():\n"
            "    a = helper()\n"
            "    b = helper()\n"
            "    return a + b\n"
        )
        nodes, ast_edges = ast_builder.build("test.py", source)
        edges = builder.build(nodes, ast_edges)

        # Should have exactly one Method→Method CALLS edge (deduplicated)
        assert len(edges) == 1
        assert edges[0].properties.get("caller") == "main"
        assert edges[0].properties.get("callee") == "helper"

    def test_cross_file_method_to_method(self) -> None:
        """Cross-file calls should also be Method→Method."""
        ast_builder = ASTBuilder()
        builder = CallGraphBuilder()

        utils_src = "def format_name(name):\n    return name.upper()\n"
        svc_src = "def greet(name):\n    formatted = format_name(name)\n    return formatted\n"

        utils_nodes, utils_edges = ast_builder.build("utils.py", utils_src)
        svc_nodes, svc_edges = ast_builder.build("service.py", svc_src)

        all_nodes = utils_nodes + svc_nodes
        all_edges = utils_edges + svc_edges
        edges = builder.build(all_nodes, all_edges)

        node_index = {n.id: n for n in all_nodes}
        cross_file = [e for e in edges if e.properties.get("callee") == "format_name"]
        assert len(cross_file) == 1
        src = node_index[cross_file[0].source_id]
        assert src.has_label("Method")
        assert src.properties.get("name") == "greet"

    def test_orchestrator_method_to_method(self) -> None:
        """The orchestrator should produce Method→Method CALLS edges."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "a.py").write_text("def helper():\n    return 1\n")
            (Path(tmpdir) / "b.py").write_text("def main():\n    x = helper()\n    return x\n")
            orchestrator = ProjectOrchestrator(plugins=[PythonPlugin()])
            nodes, edges = orchestrator.analyze(tmpdir)

            node_index = {n.id: n for n in nodes}
            calls_edges = [e for e in edges if e.edge_type == EdgeType.CALLS]
            assert len(calls_edges) > 0
            for edge in calls_edges:
                src = node_index[edge.source_id]
                tgt = node_index[edge.target_id]
                assert src.has_label("Method"), f"source should be Method, got {src.labels}"
                assert tgt.has_label("Method"), f"target should be Method, got {tgt.labels}"


class TestCallerIdentityFix:
    """Tests that CALLS edges always carry a ``caller`` property and resolve to Methods."""

    def test_caller_property_without_edges(self) -> None:
        """Even without PARENT_OF edges, line-range fallback should set caller."""
        ast_builder = ASTBuilder()
        builder = CallGraphBuilder()
        source = "def helper():\n    return 42\n\ndef main():\n    x = helper()\n    return x\n"
        nodes, _ast_edges = ast_builder.build("test.py", source)
        # Pass *no* edges — forces the line-range fallback.
        edges = builder.build(nodes, all_edges=None)

        assert len(edges) > 0
        node_index = {n.id: n for n in nodes}
        for edge in edges:
            source_node = node_index[edge.source_id]
            # Source should be resolved to a Method, not a call-site.
            assert source_node.has_label("Method"), (
                f"CALLS source should be Method (line-range fallback), got {source_node.labels}"
            )
            # The caller property should always be present.
            assert edge.properties.get("caller") == "main"
            assert edge.properties.get("callee") == "helper"

    def test_line_range_finds_innermost_method(self) -> None:
        """Nested functions: the innermost enclosing method should be the caller."""
        ast_builder = ASTBuilder()
        builder = CallGraphBuilder()
        source = (
            "def target():\n"
            "    return 1\n"
            "\n"
            "def outer():\n"
            "    def inner():\n"
            "        x = target()\n"
            "        return x\n"
            "    return inner\n"
        )
        nodes, _ast_edges = ast_builder.build("test.py", source)
        # No edges — exercises line-range fallback.
        edges = builder.build(nodes, all_edges=None)

        node_index = {n.id: n for n in nodes}
        call_to_target = [e for e in edges if e.properties.get("callee") == "target"]
        assert len(call_to_target) >= 1
        for edge in call_to_target:
            src = node_index[edge.source_id]
            assert src.has_label("Method")
            # Should resolve to *inner*, not *outer*.
            assert src.properties.get("name") == "inner"
            assert edge.properties.get("caller") == "inner"

    def test_contains_edges_resolve_caller(self) -> None:
        """CONTAINS edges (from skeleton) should also be usable for caller resolution."""
        from types import MappingProxyType

        from omnicpg.models.node import CPGNode as _CPGNode

        builder = CallGraphBuilder()

        method_node = _CPGNode(
            id="m1",
            labels=("Node", "Method"),
            properties=MappingProxyType(
                {
                    "type": "function_definition",
                    "name": "my_func",
                    "file_path": "a.py",
                    "line_start": 1,
                    "line_end": 5,
                    "code": "def my_func():\n    helper()\n",
                }
            ),
        )
        call_node = _CPGNode(
            id="c1",
            labels=("Node",),
            properties=MappingProxyType(
                {
                    "type": "call",
                    "code": "helper()",
                    "file_path": "a.py",
                    "line_start": 2,
                    "line_end": 2,
                }
            ),
        )
        target_node = _CPGNode(
            id="t1",
            labels=("Node", "Method"),
            properties=MappingProxyType(
                {
                    "type": "function_definition",
                    "name": "helper",
                    "file_path": "b.py",
                    "line_start": 1,
                    "line_end": 3,
                    "code": "def helper():\n    pass\n",
                }
            ),
        )
        # Use CONTAINS edge (not PARENT_OF) between method and call.
        contains_edge = CPGEdge(
            source_id="m1",
            target_id="c1",
            edge_type=EdgeType.CONTAINS,
        )
        edges = builder.build(
            [method_node, call_node, target_node],
            [contains_edge],
        )
        assert len(edges) == 1
        assert edges[0].source_id == "m1"
        assert edges[0].properties.get("caller") == "my_func"
        assert edges[0].properties.get("callee") == "helper"
