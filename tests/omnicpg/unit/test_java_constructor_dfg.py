"""Unit tests for Java constructor data flow / object wrapping."""

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


class TestJavaConstructorCallGraph:
    """Tests for constructor call graph resolution."""

    def test_object_creation_produces_calls_edge(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """``new DataWrapper(tainted)`` produces a CALLS edge to the constructor."""
        ast_b, cg_b, _ = builders
        source = (
            "package com.ex;\n"
            "public class Main {\n"
            "    public void run() {\n"
            "        DataWrapper w = new DataWrapper(\"x\");\n"
            "    }\n"
            "}\n"
            "class DataWrapper {\n"
            "    public DataWrapper(String val) {}\n"
            "}\n"
        )
        nodes, ast_edges = ast_b.build("Main.java", source)
        calls_edges = cg_b.build(nodes, ast_edges)

        constructor_calls = [
            e for e in calls_edges if e.properties.get("callee") == "DataWrapper"
        ]
        assert constructor_calls, "Expected CALLS edge for constructor invocation"
        # The target must be the constructor_declaration (Method-labelled) node.
        nm = {n.id: n for n in nodes}
        for edge in constructor_calls:
            target = nm[edge.target_id]
            assert target.has_label("Method")
            assert target.properties.get("type") == "constructor_declaration"

    def test_object_creation_arg_count_and_exprs(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """``new Foo(a, b)`` has ``arg_count=2`` and ``arg_exprs`` on the AST node."""
        ast_b, _, _ = builders
        source = (
            "package com.ex;\n"
            "class Svc {\n"
            "    void run() { new Pair(1, \"hello\"); }\n"
            "}\n"
        )
        nodes, _ = ast_b.build("Svc.java", source)
        oce = [n for n in nodes if n.properties.get("type") == "object_creation_expression"]
        assert oce
        assert oce[0].properties.get("arg_count") == 2
        assert oce[0].properties.get("arg_exprs") == ("1", '"hello"')


class TestJavaConstructorDFG:
    """Tests for Object Wrapping data flow via constructors (Phase 3)."""

    def test_constructor_field_sensitive_flow(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """Taint flows from constructor argument, to field, and is read via getter."""
        ast_b, cg_b, ip_b = builders
        source = (
            "package com.ex;\n"
            "public class Main {\n"
            "    public void run() {\n"
            "        String tainted = getTaintedInput();\n"
            "        DataWrapper wrapper = new DataWrapper(tainted);\n"
            "        String result = wrapper.getValue();\n"
            "        sink(result);\n"
            "    }\n"
            "    String getTaintedInput() { return \"taint\"; }\n"
            "    void sink(String s) {}\n"
            "}\n"
            "class DataWrapper {\n"
            "    private String value;\n"
            "    public DataWrapper(String val) {\n"
            "        this.value = val;\n"
            "    }\n"
            "    public String getValue() {\n"
            "        return this.value;\n"
            "    }\n"
            "}\n"
        )
        nodes, ast_edges = ast_b.build("Main.java", source)
        calls_edges = cg_b.build(nodes, ast_edges)
        nm = {n.id: n for n in nodes}

        # 1. Verify CALLS edge to constructor
        constructor_calls = [
            e for e in calls_edges if e.properties.get("callee") == "DataWrapper"
        ]
        assert constructor_calls, "Expected CALLS edge for constructor invocation"

        all_edges = list(ast_edges) + list(calls_edges)
        ip_edges = ip_b.build(nodes, all_edges)

        # 2. Argument → Parameter binding (constructor)
        #    The argument "tainted" should reach the formal_parameter "String val".
        arg_edges = [
            e for e in ip_edges
            if e.edge_type == EdgeType.REACHES
            and e.properties.get("interprocedural") == "argument"
        ]
        # Find the specific arg edge where the source is the "tainted" identifier
        # and the target is the constructor's formal_parameter.
        ctor_arg_edges = [
            e for e in arg_edges
            if nm[e.source_id].properties.get("code", "").strip() == "tainted"
            and nm[e.target_id].properties.get("code", "").strip() == "String val"
        ]
        assert ctor_arg_edges, "Expected constructor argument to bind to constructor parameter"

        # 3. Field write inside constructor → field read inside getter
        field_edges = [
            e for e in ip_edges
            if e.edge_type == EdgeType.REACHES
            and e.properties.get("interprocedural") == "field"
            and e.properties.get("variable") == "value"
        ]
        assert field_edges, "Expected constructor field assignment to reach field read in getter"

        # 4. Return from getter → getter callsite
        return_edges = [
            e for e in ip_edges
            if e.edge_type == EdgeType.REACHES
            and e.properties.get("interprocedural") == "return"
        ]
        # Find return edge from getValue's return statement to the getValue() callsite
        getter_return_edges = [
            e for e in return_edges
            if "this.value" in nm[e.source_id].properties.get("code", "")
            and "getValue" in nm[e.target_id].properties.get("code", "")
        ]
        assert getter_return_edges, "Expected getter return to bind to getter callsite"
