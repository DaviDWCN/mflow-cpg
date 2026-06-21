"""Unit tests for Java analysis precision-quantification metrics (P2-9)."""

from __future__ import annotations

import pytest

from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.java_plugin.call_graph_builder import CallGraphBuilder
from omnicpg.plugins.java_plugin.interprocedural_dfg_builder import (
    InterProceduralDFGBuilder,
)
from omnicpg.plugins.java_plugin.metrics import compute_java_metrics


@pytest.fixture()
def builders() -> tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder]:
    """Return the AST / call-graph / inter-procedural DFG builders."""
    return ASTBuilder(), CallGraphBuilder(), InterProceduralDFGBuilder()


def _analyze(
    builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    source: str,
) -> dict[str, float | int]:
    ast_b, cg_b, ip_b = builders
    nodes, edges = ast_b.build("M.java", source)
    edges = list(edges) + list(cg_b.build(nodes, edges))
    edges = list(edges) + list(ip_b.build(nodes, edges))
    return compute_java_metrics(nodes, edges)


class TestJavaMetrics:
    """Tests for :func:`compute_java_metrics`."""

    def test_metrics_keys_present(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """All documented metric keys are always present."""
        metrics = _analyze(builders, "public class T { void f(){} }")
        for key in (
            "call_sites",
            "resolved_call_sites",
            "call_resolution_rate",
            "calls_edges",
            "typed_calls",
            "heuristic_calls",
            "typed_call_ratio",
            "interprocedural_edges",
            "security_sources",
            "security_sinks",
            "methods",
            "classes",
        ):
            assert key in metrics

    def test_ratios_safe_when_empty(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """Ratios default to 0.0 when there are no call sites / edges."""
        metrics = _analyze(builders, "public class Empty {}")
        assert metrics["call_resolution_rate"] == 0.0
        assert metrics["typed_call_ratio"] == 0.0
        assert metrics["calls_edges"] == 0

    def test_typed_call_ratio_and_resolution(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """Resolvable intra-project calls are type-resolved and counted."""
        source = (
            "package com.ex;\n"
            "interface Animal { String speak(); }\n"
            'class Dog implements Animal { public String speak(){ return "w"; } }\n'
            "class Svc {\n"
            "  void run(Animal a){ a.speak(); helper(); }\n"
            "  void helper(){}\n"
            "}\n"
        )
        metrics = _analyze(builders, source)
        assert metrics["calls_edges"] >= 2
        assert metrics["typed_calls"] >= 1
        assert 0.0 < metrics["typed_call_ratio"] <= 1.0
        assert metrics["resolved_call_sites"] >= 1

    def test_security_and_interproc_counts(
        self,
        builders: tuple[ASTBuilder, CallGraphBuilder, InterProceduralDFGBuilder],
    ) -> None:
        """Security tags and field/return inter-procedural edges are counted."""
        source = (
            "package com.ex;\n"
            "class Box {\n"
            "  private String d;\n"
            "  void set(String v){ this.d = v; }\n"
            "  String get(){ return this.d; }\n"
            "}\n"
            "class Svc {\n"
            "  void f(javax.servlet.http.HttpServletRequest req, java.sql.Statement st)"
            " throws Exception {\n"
            '    String p = req.getParameter("x");\n'
            "    st.executeQuery(p);\n"
            "  }\n"
            "  String g(Box b){ return b.get(); }\n"
            "}\n"
        )
        metrics = _analyze(builders, source)
        assert metrics["security_sources"] >= 1
        assert metrics["security_sinks"] >= 1
        assert metrics["interproc_field"] >= 1
        assert metrics["interproc_return"] >= 1
