"""Unit tests for the Java CFG builder."""

from __future__ import annotations

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.java_plugin.cfg_builder import CFGBuilder


@pytest.fixture()
def builders() -> tuple[ASTBuilder, CFGBuilder]:
    """Return a fresh (ASTBuilder, CFGBuilder) pair."""
    return ASTBuilder(), CFGBuilder()


class TestJavaCFGBuilder:
    """Tests for :class:`CFGBuilder` (Java)."""

    def test_sequential_flow(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """Two sequential statements produce a FLOWS_TO edge between them."""
        source = (
            "public class T {\n"
            "    public void foo() {\n"
            "        int x = 1;\n"
            "        int y = 2;\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)
        assert len(cfg_edges) > 0
        assert all(e.edge_type == EdgeType.FLOWS_TO for e in cfg_edges)

    def test_if_branch_edges(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """An ``if`` statement produces True and False condition edges."""
        source = (
            "public class T {\n"
            "    public void bar(int x) {\n"
            "        if (x > 0) {\n"
            "            int y = 1;\n"
            "        } else {\n"
            "            int y = 2;\n"
            "        }\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)

        conditions = [
            e.properties.get("condition") for e in cfg_edges if "condition" in e.properties
        ]
        assert "True" in conditions
        assert "False" in conditions

    def test_for_loop_back_edge(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A ``for`` loop produces a back-edge from body to condition."""
        source = (
            "public class T {\n"
            "    public void loop() {\n"
            "        for (int i = 0; i < 10; i++) {\n"
            "            System.out.println(i);\n"
            "        }\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)
        assert len(cfg_edges) > 0
        # Should have both True and False condition edges.
        conditions = [
            e.properties.get("condition") for e in cfg_edges if "condition" in e.properties
        ]
        assert "True" in conditions
        assert "False" in conditions

    def test_try_catch_edges(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A ``try/catch`` statement produces exception edges."""
        source = (
            "public class T {\n"
            "    public void safe() {\n"
            "        try {\n"
            "            System.out.println(1);\n"
            "        } catch (Exception e) {\n"
            "            e.printStackTrace();\n"
            "        }\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)
        assert len(cfg_edges) > 0
        conditions = [
            e.properties.get("condition") for e in cfg_edges if "condition" in e.properties
        ]
        assert "exception" in conditions

    def test_no_cfg_for_non_method(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A class with only field declarations produces no CFG edges."""
        source = "public class T {\n    private int x;\n    private int y;\n}\n"
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)
        assert cfg_edges == []

    def test_all_edges_are_flows_to(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """Every CFG edge must be of type ``FLOWS_TO``."""
        source = (
            "public class T {\n"
            "    public void f() {\n"
            "        int a = 1;\n"
            "        if (a > 0) {\n"
            "            int b = 2;\n"
            "        }\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)
        for edge in cfg_edges:
            assert edge.edge_type == EdgeType.FLOWS_TO

    def test_switch_case_edges(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A ``switch`` statement produces case condition edges."""
        source = (
            "public class T {\n"
            "    public void sw(int x) {\n"
            "        switch (x) {\n"
            '            case 1: System.out.println("one"); break;\n'
            '            default: System.out.println("other"); break;\n'
            "        }\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)
        conditions = [
            e.properties.get("condition") for e in cfg_edges if "condition" in e.properties
        ]
        assert "case" in conditions

    def test_try_catch_exception_edge(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A try/catch produces an exception-conditioned flow edge into the catch body."""
        source = (
            "public class T {\n"
            "    public void run() {\n"
            "        try { risky(); }\n"
            "        catch (Exception e) { handle(e); }\n"
            "        finally { cleanup(); }\n"
            "        return;\n"
            "    }\n"
            "    void risky() {}\n"
            "    void handle(Exception e) {}\n"
            "    void cleanup() {}\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)
        conditions = [
            e.properties.get("condition") for e in cfg_edges if "condition" in e.properties
        ]
        assert "exception" in conditions


class TestBreakContinueTargeting:
    """P1-5: break/continue jump to loop exit / header instead of falling through."""

    def test_break_flows_to_loop_exit(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A ``break`` inside a loop flows to the statement after the loop."""
        source = (
            "public class T {\n"
            "    public void f(int n) {\n"
            "        for (int i = 0; i < n; i++) {\n"
            "            if (i == 5) { break; }\n"
            "            work();\n"
            "        }\n"
            "        done();\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)

        break_node = next(n for n in nodes if n.properties.get("type") == "break_statement")
        done_stmt = next(
            n
            for n in nodes
            if n.properties.get("type") == "expression_statement"
            and "done()" in str(n.properties.get("code", ""))
        )
        out_targets = {e.target_id for e in cfg_edges if e.source_id == break_node.id}
        assert done_stmt.id in out_targets, "break should flow to the post-loop statement"

    def test_continue_flows_to_loop_header(self, builders: tuple[ASTBuilder, CFGBuilder]) -> None:
        """A ``continue`` inside a loop flows back to the loop header node."""
        source = (
            "public class T {\n"
            "    public void f(int n) {\n"
            "        for (int i = 0; i < n; i++) {\n"
            "            if (i == 3) { continue; }\n"
            "            work();\n"
            "        }\n"
            "        done();\n"
            "    }\n"
            "}\n"
        )
        ast_b, cfg_b = builders
        nodes, edges = ast_b.build("T.java", source)
        cfg_edges = cfg_b.build(nodes, edges)

        cont_node = next(n for n in nodes if n.properties.get("type") == "continue_statement")
        loop_node = next(n for n in nodes if n.properties.get("type") == "for_statement")
        out_targets = {e.target_id for e in cfg_edges if e.source_id == cont_node.id}
        assert loop_node.id in out_targets, "continue should flow back to the loop header"
