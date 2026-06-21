"""Unit tests for the Python CFG builder."""

from __future__ import annotations

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.python_plugin.cfg_builder import CFGBuilder


@pytest.fixture()
def builders() -> tuple[ASTBuilder, CFGBuilder]:
    """Return a fresh (ASTBuilder, CFGBuilder) pair."""
    return ASTBuilder(), CFGBuilder()


class TestCFGBuilder:
    """Tests for :class:`CFGBuilder`."""

    def test_sequential_flow(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """Two sequential statements produce a FLOWS_TO edge between them."""
        source = "def foo():\n    x = 1\n    y = 2\n    return y\n"
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, edges)
        assert len(cfg_edges) > 0
        assert all(e.edge_type == EdgeType.FLOWS_TO for e in cfg_edges)

    def test_if_branch_edges(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """An ``if`` statement produces True and False condition edges."""
        source = (
            "def bar(x):\n    if x > 0:\n        y = 1\n    else:\n        y = 2\n    return y\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, edges)

        conditions = [
            e.properties.get("condition") for e in cfg_edges if "condition" in e.properties
        ]
        assert "True" in conditions
        assert "False" in conditions

    def test_no_cfg_for_non_function(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """Top-level code without functions produces no CFG edges."""
        source = "x = 1\ny = 2\n"
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, edges)
        assert cfg_edges == []

    def test_all_edges_are_flows_to(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """Every CFG edge must be of type ``FLOWS_TO``."""
        source = "def f():\n    a = 1\n    if a:\n        b = 2\n    return a\n"
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, edges)
        for edge in cfg_edges:
            assert edge.edge_type == EdgeType.FLOWS_TO

    def test_try_except_flow(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A ``try`` statement produces Exception condition edges to except clause."""
        source = (
            "def baz():\n"
            "    try:\n"
            "        x = 1\n"
            "    except Exception:\n"
            "        y = 2\n"
            "    finally:\n"
            "        z = 3\n"
            "    return z\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("test.py", source)
        cfg_edges = cfg_b.build(nodes, edges)

        conditions = [
            e.properties.get("condition") for e in cfg_edges if "condition" in e.properties
        ]
        assert "Exception" in conditions
