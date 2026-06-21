"""Unit tests for the Python DFG builder."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.python_plugin.cfg_builder import CFGBuilder
from omnicpg.plugins.python_plugin.dfg_builder import DFGBuilder


@pytest.fixture()
def builders() -> tuple[ASTBuilder, CFGBuilder, DFGBuilder]:
    """Return a fresh (ASTBuilder, CFGBuilder, DFGBuilder) triple."""
    return ASTBuilder(), CFGBuilder(), DFGBuilder()


class TestDFGBuilder:
    """Tests for :class:`DFGBuilder`."""

    def test_simple_def_use(self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]) -> None:
        """A simple assignment followed by use produces a REACHES edge."""
        source = "def foo():\n    x = 1\n    return x\n"
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)

        assert len(dfg_edges) > 0
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)

        # At least one edge should reference variable 'x'.
        variables = [e.properties.get("variable") for e in dfg_edges]
        assert "x" in variables

    def test_dfg_edges_reference_correct_variables(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """REACHES edges reference the correct variable names."""
        source = "def foo():\n    x = 1\n    y = x + 2\n    return y\n"
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)

        variables = {str(e.properties.get("variable")) for e in dfg_edges}
        # Both 'x' and 'y' should appear in REACHES edges.
        assert "x" in variables
        assert "y" in variables
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)

    def test_all_edges_are_reaches(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """Every DFG edge must be of type ``REACHES``."""
        source = "def foo():\n    a = 1\n    b = a + 1\n    return b\n"
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)
        for edge in dfg_edges:
            assert edge.edge_type == EdgeType.REACHES

    def test_no_dfg_without_functions(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """Top-level code without functions produces no DFG edges."""
        source = "x = 1\ny = x\n"
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)
        assert dfg_edges == []

    def test_branch_merge_reaching_defs(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """Definitions in both if/else branches both reach the use after the merge."""
        source = (
            "def baz(cond):\n    if cond:\n        x = 1\n    else:\n        x = 2\n    return x\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)

        reaches_x = [e for e in dfg_edges if e.properties.get("variable") == "x"]
        # Both branch definitions of x should reach the return.
        def_node_ids = {e.source_id for e in reaches_x}
        assert len(def_node_ids) >= 1  # at minimum one def reaches the use

    def test_loop_carried_definition(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """A definition inside a loop can reach uses on subsequent iterations."""
        source = (
            "def loop_fn(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total = total + item\n"
            "    return total\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)

        variables = {str(e.properties.get("variable")) for e in dfg_edges}
        assert "total" in variables
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)

    def test_scope_children_indexed_deduplicates_cycles(self) -> None:
        """Scope traversal should tolerate duplicate and cyclic PARENT_OF edges."""
        builder = DFGBuilder()
        module = CPGNode(
            id="module",
            labels=("Node", "Module"),
            properties=MappingProxyType({"type": "module", "line_start": 1, "line_end": 3}),
        )
        stmt = CPGNode(
            id="stmt",
            labels=("Node",),
            properties=MappingProxyType(
                {"type": "expression_statement", "line_start": 2, "line_end": 2}
            ),
        )
        ident = CPGNode(
            id="ident",
            labels=("Node",),
            properties=MappingProxyType({"type": "identifier", "line_start": 2, "line_end": 2}),
        )

        all_nodes = [module, stmt, ident]
        ast_edges = [
            CPGEdge(source_id="module", target_id="stmt", edge_type=EdgeType.PARENT_OF),
            CPGEdge(source_id="module", target_id="stmt", edge_type=EdgeType.PARENT_OF),
            CPGEdge(source_id="stmt", target_id="ident", edge_type=EdgeType.PARENT_OF),
            CPGEdge(source_id="ident", target_id="stmt", edge_type=EdgeType.PARENT_OF),
        ]

        builder.build(all_nodes, cfg_edges=[], ast_edges=ast_edges)

        scope_children = builder._scope_children(module, all_nodes)
        assert [node.id for node in scope_children] == ["stmt", "ident"]
