"""Unit tests for the Java inter-procedural DFG builder (argument → parameter)."""

from __future__ import annotations

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.java_plugin.call_graph_builder import CallGraphBuilder
from omnicpg.plugins.java_plugin.interprocedural_dfg_builder import (
    InterProceduralDFGBuilder,
)


@pytest.fixture()
def builders() -> tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder]:
    """Return the AST / call-graph / inter-procedural DFG builders."""
    return ASTBuilder(), CallGraphBuilder(), InterProceduralDFGBuilder()


class TestJavaInterProceduralDFG:
    """Tests for :class:`InterProceduralDFGBuilder` (Java)."""

    def test_argument_binds_to_parameter(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """A call argument flows into the callee's parameter (interprocedural=argument)."""
        ast_b, cg_b, ip_b = builders
        source = (
            "package com.ex;\n"
            "public class Svc {\n"
            "    public void caller() {\n"
            "        String x = source();\n"
            "        callee(x);\n"
            "    }\n"
            "    public void callee(String p) { sink(p); }\n"
            "    String source() { return null; }\n"
            "    void sink(String s) {}\n"
            "}\n"
        )
        nodes, ast_edges = ast_b.build("Svc.java", source)
        calls_edges = cg_b.build(nodes, ast_edges)
        all_edges = list(ast_edges) + list(calls_edges)
        ip_edges = ip_b.build(nodes, all_edges)

        arg_edges = [
            e
            for e in ip_edges
            if e.edge_type == EdgeType.REACHES
            and e.properties.get("interprocedural") == "argument"
        ]
        assert arg_edges, "expected at least one argument→parameter REACHES edge"

    def test_callsite_id_present_on_calls_edges(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """V2 CALLS edges carry a ``callsite_id`` for accurate arg→param binding."""
        ast_b, cg_b, _ = builders
        source = (
            "package com.ex;\n"
            "public class Svc {\n"
            "    public void caller() { callee(1); }\n"
            "    public void callee(int p) {}\n"
            "}\n"
        )
        nodes, ast_edges = ast_b.build("Svc.java", source)
        calls_edges = cg_b.build(nodes, ast_edges)
        callee_calls = [e for e in calls_edges if e.properties.get("callee") == "callee"]
        assert callee_calls
        assert all(e.properties.get("callsite_id") for e in callee_calls)


class TestFieldPropagation:
    """Tests for field-sensitive interprocedural propagation (P0-3)."""

    def test_setter_field_reaches_getter(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """A field write in a setter reaches the field read in a getter."""
        ast_b, cg_b, ip_b = builders
        source = (
            "package com.ex;\n"
            "public class Box {\n"
            "    private String data;\n"
            "    public void set(String v) { this.data = v; }\n"
            "    public String get() { return this.data; }\n"
            "}\n"
        )
        nodes, ast_edges = ast_b.build("Box.java", source)
        all_edges = list(ast_edges) + list(cg_b.build(nodes, ast_edges))
        ip_edges = ip_b.build(nodes, all_edges)

        field_edges = [e for e in ip_edges if e.properties.get("interprocedural") == "field"]
        assert field_edges, "expected a field write→read REACHES edge"
        assert all(e.properties.get("variable") == "data" for e in field_edges)
        nm = {n.id: n for n in nodes}
        assert any(
            nm[e.source_id].properties.get("assign_kind") == "field"
            and nm[e.target_id].properties.get("type") == "field_access"
            for e in field_edges
        )

    def test_write_target_not_treated_as_read(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """The LHS of a field assignment is not counted as a read sink."""
        ast_b, cg_b, ip_b = builders
        source = (
            "package com.ex;\n"
            "public class Box {\n"
            "    private String data;\n"
            "    public void set(String v) { this.data = v; }\n"
            "}\n"
        )
        nodes, ast_edges = ast_b.build("Box.java", source)
        all_edges = list(ast_edges) + list(cg_b.build(nodes, ast_edges))
        ip_edges = ip_b.build(nodes, all_edges)
        # No getter/read exists, so there must be no field bridge edge.
        field_edges = [e for e in ip_edges if e.properties.get("interprocedural") == "field"]
        assert not field_edges

    def test_fields_isolated_per_class(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """A field write in one class does not reach a same-named read elsewhere."""
        ast_b, cg_b, ip_b = builders
        source = (
            "package com.ex;\n"
            "class A {\n"
            "    private String data;\n"
            "    void set(String v) { this.data = v; }\n"
            "}\n"
            "class B {\n"
            "    private String data;\n"
            "    String get() { return this.data; }\n"
            "}\n"
        )
        nodes, ast_edges = ast_b.build("AB.java", source)
        all_edges = list(ast_edges) + list(cg_b.build(nodes, ast_edges))
        ip_edges = ip_b.build(nodes, all_edges)
        field_edges = [e for e in ip_edges if e.properties.get("interprocedural") == "field"]
        assert not field_edges, "field bridge must not cross class boundaries"
