"""Unit tests for the Java DFG builder."""

from __future__ import annotations

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.java_plugin.cfg_builder import CFGBuilder
from omnicpg.plugins.java_plugin.dfg_builder import DFGBuilder


@pytest.fixture()
def builders() -> tuple[ASTBuilder, CFGBuilder, DFGBuilder]:
    """Return a fresh (ASTBuilder, CFGBuilder, DFGBuilder) triple."""
    return ASTBuilder(), CFGBuilder(), DFGBuilder()


class TestJavaDFGBuilder:
    """Tests for :class:`DFGBuilder` (Java)."""

    def test_simple_def_use(self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]) -> None:
        """A simple assignment followed by use produces a REACHES edge."""
        source = (
            "public class T {\n"
            "    public int foo() {\n"
            "        int x = 1;\n"
            "        return x;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)

        assert len(dfg_edges) > 0
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)

        variables = [e.properties.get("variable") for e in dfg_edges]
        assert "x" in variables

    def test_dfg_edges_reference_correct_variables(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """REACHES edges reference the correct variable names."""
        source = (
            "public class T {\n"
            "    public int foo() {\n"
            "        int x = 1;\n"
            "        int y = x + 2;\n"
            "        return y;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)

        variables = {str(e.properties.get("variable")) for e in dfg_edges}
        assert "x" in variables
        assert "y" in variables
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)

    def test_all_edges_are_reaches(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """Every DFG edge must be of type ``REACHES``."""
        source = (
            "public class T {\n"
            "    public int foo() {\n"
            "        int a = 1;\n"
            "        int b = a + 1;\n"
            "        return b;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)
        for edge in dfg_edges:
            assert edge.edge_type == EdgeType.REACHES

    def test_no_dfg_without_methods(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """A class without methods produces no DFG edges."""
        source = "public class T {\n    private int x;\n    private int y;\n}\n"
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)
        assert dfg_edges == []

    def test_branch_join_reaches(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """Definitions on both branches of an if/else reach a later use."""
        source = (
            "public class T {\n"
            "    public int foo(boolean c) {\n"
            "        int x;\n"
            "        if (c) { x = 1; } else { x = 2; }\n"
            "        return x;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)
        x_edges = [e for e in dfg_edges if e.properties.get("variable") == "x"]
        # Both branch definitions should reach the return use → ≥2 REACHES for x.
        assert len(x_edges) >= 2

    def test_loop_carried_dependency(
        self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]
    ) -> None:
        """A loop-carried definition reaches uses across iterations."""
        source = (
            "public class T {\n"
            "    public int foo() {\n"
            "        int sum = 0;\n"
            "        for (int i = 0; i < 10; i++) { sum = sum + i; }\n"
            "        return sum;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)
        variables = [e.properties.get("variable") for e in dfg_edges]
        assert "sum" in variables

    def test_field_level_flow(self, builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder]) -> None:
        """A write to ``this.field`` reaches a later read of the field."""
        source = (
            "public class T {\n"
            "    private int total;\n"
            "    public int foo(int x) {\n"
            "        this.total = x;\n"
            "        return total;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b, dfg_b = builders
        nodes, ast_edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, ast_edges)
        dfg_edges = dfg_b.build(nodes, cfg_edges, ast_edges)
        variables = [str(e.properties.get("variable", "")) for e in dfg_edges]
        assert any("total" in v for v in variables)
