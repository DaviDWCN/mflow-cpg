"""Unit tests for the Python AST builder (Tree-sitter integration)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def ast_builder() -> ASTBuilder:
    """Return a fresh ASTBuilder instance."""
    return ASTBuilder()


@pytest.fixture()
def simple_source() -> str:
    """Return a small Python source snippet."""
    return "def greet(name):\n    message = 'Hello, ' + name\n    return message\n"


class TestASTBuilder:
    """Tests for :class:`ASTBuilder`."""

    def test_basic_parse_produces_nodes_and_edges(
        self, ast_builder: ASTBuilder, simple_source: str
    ) -> None:
        """Parsing valid Python produces at least one node and one edge."""
        nodes, edges = ast_builder.build("test.py", simple_source)
        assert len(nodes) > 0
        assert len(edges) > 0

    def test_root_node_is_module(self, ast_builder: ASTBuilder, simple_source: str) -> None:
        """The first node should have 'Module' label."""
        nodes, _ = ast_builder.build("test.py", simple_source)
        root = nodes[0]
        assert root.has_label("Module")
        assert root.properties["type"] == "module"

    def test_function_node_has_method_label(
        self, ast_builder: ASTBuilder, simple_source: str
    ) -> None:
        """A ``function_definition`` node carries the 'Method' label."""
        nodes, _ = ast_builder.build("test.py", simple_source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) == 1
        assert methods[0].properties["name"] == "greet"

    def test_all_edges_are_parent_of(self, ast_builder: ASTBuilder, simple_source: str) -> None:
        """AST edges are ``PARENT_OF``; skeleton edges are ``CONTAINS`` / ``DEPENDS_ON``."""
        _, edges = ast_builder.build("test.py", simple_source)
        allowed = {EdgeType.PARENT_OF, EdgeType.CONTAINS, EdgeType.DEPENDS_ON}
        assert all(e.edge_type in allowed for e in edges)
        # The classic AST sub-graph must still contain PARENT_OF edges.
        assert any(e.edge_type == EdgeType.PARENT_OF for e in edges)

    def test_edge_source_and_target_are_valid_node_ids(
        self, ast_builder: ASTBuilder, simple_source: str
    ) -> None:
        """Every edge's source and target must reference an existing node id."""
        nodes, edges = ast_builder.build("test.py", simple_source)
        node_ids = {n.id for n in nodes}
        for edge in edges:
            assert edge.source_id in node_ids
            assert edge.target_id in node_ids

    def test_nodes_have_line_info(self, ast_builder: ASTBuilder, simple_source: str) -> None:
        """Every node should have ``line_start`` and ``line_end``."""
        nodes, _ = ast_builder.build("test.py", simple_source)
        for node in nodes:
            assert "line_start" in node.properties
            assert "line_end" in node.properties
            assert node.properties["line_start"] >= 1

    def test_class_node_has_class_label(self, ast_builder: ASTBuilder) -> None:
        """A ``class_definition`` node carries the 'Class' label."""
        source = "class Foo:\n    pass\n"
        nodes, _ = ast_builder.build("test.py", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert classes[0].properties["name"] == "Foo"

    def test_fixture_file_parse(self, ast_builder: ASTBuilder, sample_python_dir: Path) -> None:
        """The sample fixture file can be parsed without errors."""
        simple_py = sample_python_dir / "simple.py"
        source = simple_py.read_text()
        nodes, edges = ast_builder.build(str(simple_py), source)
        assert len(nodes) > 10  # rough sanity check
        assert len(edges) > 5
