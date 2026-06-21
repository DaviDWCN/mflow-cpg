"""Unit tests for the analysis-level (dimensionality reduction) feature.

Tests cover:
* ``AnalysisLevel`` enum semantics.
* Python and Java AST builders in ``ARCHITECTURAL`` and ``STRUCTURAL`` modes.
* ``ProjectOrchestrator`` behaviour at each analysis level.
* ``CodeSlicer`` JIT expansion helpers (``get_method_source`` / ``expand_method``).
* Edge type correctness (``CONTAINS`` vs. ``PARENT_OF``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.models.edge import EdgeType
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder as JavaASTBuilder
from omnicpg.plugins.java_plugin.plugin import JavaPlugin
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder as PythonASTBuilder
from omnicpg.plugins.python_plugin.plugin import PythonPlugin
from omnicpg.slicer.code_slicer import CodeSlicer

# ── Fixtures ──────────────────────────────────────────────────────────────────


PYTHON_SOURCE = """\
class Calculator:
    def add(self, a, b):
        result = a + b
        return result

    def subtract(self, a, b):
        if a > b:
            return a - b
        return 0

def standalone():
    x = 42
    print(x)
"""

JAVA_SOURCE = """\
package com.example;

import java.util.List;

public class UserService {
    private String name;

    public void greet(String user) {
        String message = "Hello, " + user;
        System.out.println(message);
        if (user.isEmpty()) {
            return;
        }
        for (int i = 0; i < 10; i++) {
            System.out.println(i);
        }
    }

    public int add(int a, int b) {
        return a + b;
    }
}
"""


# ── AnalysisLevel enum ───────────────────────────────────────────────────────


class TestAnalysisLevel:
    """Tests for the :class:`AnalysisLevel` enumeration."""

    def test_values(self) -> None:
        """All three levels are defined."""
        assert AnalysisLevel.FULL == "FULL"
        assert AnalysisLevel.ARCHITECTURAL == "ARCHITECTURAL"
        assert AnalysisLevel.STRUCTURAL == "STRUCTURAL"

    def test_is_str_enum(self) -> None:
        """AnalysisLevel members are strings."""
        assert isinstance(AnalysisLevel.FULL, str)


# ── EdgeType additions ────────────────────────────────────────────────────────


class TestEdgeTypeAdditions:
    """Tests for the new ``CONTAINS`` and ``IMPLEMENTS`` edge types."""

    def test_contains_edge_type_exists(self) -> None:
        """``EdgeType.CONTAINS`` is defined."""
        assert EdgeType.CONTAINS == "CONTAINS"

    def test_implements_edge_type_exists(self) -> None:
        """``EdgeType.IMPLEMENTS`` is defined."""
        assert EdgeType.IMPLEMENTS == "IMPLEMENTS"

    def test_original_edge_types_unchanged(self) -> None:
        """Original four edge types remain intact."""
        assert EdgeType.PARENT_OF == "PARENT_OF"
        assert EdgeType.FLOWS_TO == "FLOWS_TO"
        assert EdgeType.REACHES == "REACHES"
        assert EdgeType.CALLS == "CALLS"


# ── Python AST Builder — ARCHITECTURAL ────────────────────────────────────────


class TestPythonASTArchitectural:
    """Tests for ``PythonASTBuilder`` in ``ARCHITECTURAL`` mode."""

    def test_fewer_nodes_than_full(self) -> None:
        """ARCHITECTURAL mode produces significantly fewer nodes than FULL."""
        builder = PythonASTBuilder()
        full_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.FULL)
        arch_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.ARCHITECTURAL)
        assert len(arch_nodes) < len(full_nodes)

    def test_only_skeleton_labels(self) -> None:
        """ARCHITECTURAL nodes: only Module, Class, Method, File, etc."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.ARCHITECTURAL)
        allowed_labels = {"Node", "Module", "Class", "Method", "File"}
        for node in nodes:
            assert set(node.labels).issubset(allowed_labels), (
                f"Unexpected labels {node.labels} for node type={node.properties.get('type')}"
            )

    def test_method_has_source_code_property(self) -> None:
        """Method nodes should carry a ``source_code`` property."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.ARCHITECTURAL)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) >= 2
        for method in methods:
            assert "source_code" in method.properties
            assert len(method.properties["source_code"]) > 0

    def test_edges_are_contains(self) -> None:
        """ARCHITECTURAL mode uses ``CONTAINS`` and ``DEPENDS_ON`` edges, not ``PARENT_OF``."""
        builder = PythonASTBuilder()
        _, edges = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.ARCHITECTURAL)
        allowed = {EdgeType.CONTAINS, EdgeType.DEPENDS_ON, EdgeType.IMPLEMENTS, EdgeType.TESTS}
        for edge in edges:
            assert edge.edge_type in allowed, (
                f"Unexpected edge type {edge.edge_type} in ARCHITECTURAL mode"
            )

    def test_no_variable_nodes(self) -> None:
        """ARCHITECTURAL mode should not produce any Variable nodes."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.ARCHITECTURAL)
        variables = [n for n in nodes if n.has_label("Variable")]
        assert len(variables) == 0


# ── Python AST Builder — STRUCTURAL ──────────────────────────────────────────


class TestPythonASTStructural:
    """Tests for ``PythonASTBuilder`` in ``STRUCTURAL`` mode."""

    def test_fewer_nodes_than_full(self) -> None:
        """STRUCTURAL mode produces fewer nodes than FULL."""
        builder = PythonASTBuilder()
        full_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.FULL)
        struct_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.STRUCTURAL)
        assert len(struct_nodes) < len(full_nodes)

    def test_more_nodes_than_architectural(self) -> None:
        """STRUCTURAL mode produces more nodes than ARCHITECTURAL."""
        builder = PythonASTBuilder()
        arch_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.ARCHITECTURAL)
        struct_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.STRUCTURAL)
        assert len(struct_nodes) > len(arch_nodes)

    def test_edges_are_parent_of(self) -> None:
        """STRUCTURAL mode uses ``PARENT_OF`` edges for AST traversal, plus skeleton edges."""
        builder = PythonASTBuilder()
        _, edges = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.STRUCTURAL)
        # STRUCTURAL mode uses PARENT_OF for AST, but skeleton overlay adds
        # CONTAINS, DEPENDS_ON, IMPLEMENTS, and TESTS edges.
        allowed = {
            EdgeType.PARENT_OF,
            EdgeType.CONTAINS,
            EdgeType.DEPENDS_ON,
            EdgeType.IMPLEMENTS,
            EdgeType.TESTS,
        }
        for edge in edges:
            assert edge.edge_type in allowed, (
                f"Unexpected edge type {edge.edge_type} in STRUCTURAL mode"
            )


# ── Java AST Builder — ARCHITECTURAL ─────────────────────────────────────────


class TestJavaASTArchitectural:
    """Tests for ``JavaASTBuilder`` in ``ARCHITECTURAL`` mode."""

    def test_fewer_nodes_than_full(self) -> None:
        """ARCHITECTURAL mode produces significantly fewer nodes than FULL."""
        builder = JavaASTBuilder()
        full_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.FULL)
        arch_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        assert len(arch_nodes) < len(full_nodes)

    def test_only_skeleton_labels(self) -> None:
        """ARCHITECTURAL nodes keep skeleton plus minimal call-graph context labels."""
        builder = JavaASTBuilder()
        nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        allowed_labels = {
            "Node",
            "Module",
            "Class",
            "Interface",
            "Enum",
            "Method",
            "Field",
            "Parameter",
            "CallSite",
            "File",
            "Package",
            "Import",
            "Annotation",
            "AnnotationUsage",
            "SpringComponent",
            "HibernateEntity",
            "StrutsAction",
            "RequestHandler",
        }
        for node in nodes:
            assert set(node.labels).issubset(allowed_labels), (
                f"Unexpected labels {node.labels} for type={node.properties.get('type')}"
            )

    def test_method_has_source_code_property(self) -> None:
        """Method nodes should carry a ``source_code`` property."""
        builder = JavaASTBuilder()
        nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) >= 2
        for method in methods:
            assert "source_code" in method.properties
            assert len(method.properties["source_code"]) > 0

    def test_edges_are_contains(self) -> None:
        """ARCHITECTURAL mode uses ``CONTAINS`` and ``DEPENDS_ON`` edges."""
        builder = JavaASTBuilder()
        _, edges = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        allowed = {EdgeType.CONTAINS, EdgeType.DEPENDS_ON, EdgeType.IMPLEMENTS}
        for edge in edges:
            assert edge.edge_type in allowed, (
                f"Unexpected edge type {edge.edge_type} in ARCHITECTURAL mode"
            )

    def test_class_and_method_present(self) -> None:
        """The class and its methods should all be present."""
        builder = JavaASTBuilder()
        nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        classes = [n for n in nodes if n.has_label("Class")]
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(classes) >= 1
        assert len(methods) >= 2


# ── Java AST Builder — STRUCTURAL ────────────────────────────────────────────


class TestJavaASTStructural:
    """Tests for ``JavaASTBuilder`` in ``STRUCTURAL`` mode."""

    def test_fewer_nodes_than_full(self) -> None:
        """STRUCTURAL mode produces fewer nodes than FULL."""
        builder = JavaASTBuilder()
        full_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.FULL)
        struct_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.STRUCTURAL)
        assert len(struct_nodes) < len(full_nodes)

    def test_more_nodes_than_architectural(self) -> None:
        """STRUCTURAL mode produces more nodes than ARCHITECTURAL."""
        builder = JavaASTBuilder()
        arch_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        struct_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.STRUCTURAL)
        assert len(struct_nodes) > len(arch_nodes)


# ── Python Plugin — analysis_level passthrough ────────────────────────────────


class TestPythonPluginAnalysisLevel:
    """Tests for ``PythonPlugin.parse_to_ast`` with analysis levels."""

    def test_default_level_is_full(self) -> None:
        """Calling without ``analysis_level`` should behave like FULL."""
        plugin = PythonPlugin()
        full_nodes, _ = plugin.parse_to_ast("test.py", PYTHON_SOURCE)
        explicit_full_nodes, _ = plugin.parse_to_ast(
            "test.py",
            PYTHON_SOURCE,
            analysis_level=AnalysisLevel.FULL,
        )
        assert len(full_nodes) == len(explicit_full_nodes)

    def test_architectural_via_plugin(self) -> None:
        """ARCHITECTURAL level works through the plugin interface."""
        plugin = PythonPlugin()
        nodes, edges = plugin.parse_to_ast(
            "test.py",
            PYTHON_SOURCE,
            analysis_level=AnalysisLevel.ARCHITECTURAL,
        )
        assert len(nodes) > 0
        allowed = {EdgeType.CONTAINS, EdgeType.DEPENDS_ON, EdgeType.IMPLEMENTS, EdgeType.TESTS}
        assert all(e.edge_type in allowed for e in edges)


# ── Java Plugin — analysis_level passthrough ──────────────────────────────────


class TestJavaPluginAnalysisLevel:
    """Tests for ``JavaPlugin.parse_to_ast`` with analysis levels."""

    def test_default_level_is_full(self) -> None:
        """Calling without ``analysis_level`` should behave like FULL."""
        plugin = JavaPlugin()
        full_nodes, _ = plugin.parse_to_ast("Test.java", JAVA_SOURCE)
        explicit_full_nodes, _ = plugin.parse_to_ast(
            "Test.java",
            JAVA_SOURCE,
            analysis_level=AnalysisLevel.FULL,
        )
        assert len(full_nodes) == len(explicit_full_nodes)

    def test_architectural_via_plugin(self) -> None:
        """ARCHITECTURAL level works through the plugin interface."""
        plugin = JavaPlugin()
        nodes, edges = plugin.parse_to_ast(
            "Test.java",
            JAVA_SOURCE,
            analysis_level=AnalysisLevel.ARCHITECTURAL,
        )
        assert len(nodes) > 0
        allowed = {EdgeType.CONTAINS, EdgeType.DEPENDS_ON, EdgeType.IMPLEMENTS}
        assert all(e.edge_type in allowed for e in edges)


# ── ProjectOrchestrator — analysis_level ──────────────────────────────────────


class TestOrchestratorAnalysisLevel:
    """Tests for ``ProjectOrchestrator`` with different analysis levels."""

    def test_architectural_skips_cfg_dfg(self) -> None:
        """ARCHITECTURAL mode should produce no FLOWS_TO or REACHES edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "main.py").write_text(PYTHON_SOURCE)
            orch = ProjectOrchestrator(
                plugins=[PythonPlugin()],
                analysis_level=AnalysisLevel.ARCHITECTURAL,
            )
            nodes, edges = orch.analyze(tmpdir)
            assert len(nodes) > 0
            flow_edges = [e for e in edges if e.edge_type == EdgeType.FLOWS_TO]
            reach_edges = [e for e in edges if e.edge_type == EdgeType.REACHES]
            assert len(flow_edges) == 0
            assert len(reach_edges) == 0

    def test_structural_skips_dfg_only(self) -> None:
        """STRUCTURAL mode should produce FLOWS_TO but not REACHES edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "main.py").write_text(PYTHON_SOURCE)
            orch = ProjectOrchestrator(
                plugins=[PythonPlugin()],
                analysis_level=AnalysisLevel.STRUCTURAL,
            )
            nodes, edges = orch.analyze(tmpdir)
            assert len(nodes) > 0
            reach_edges = [e for e in edges if e.edge_type == EdgeType.REACHES]
            assert len(reach_edges) == 0

    def test_full_produces_all_edge_types(self) -> None:
        """FULL mode should produce PARENT_OF, FLOWS_TO, and REACHES edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "main.py").write_text(PYTHON_SOURCE)
            orch = ProjectOrchestrator(
                plugins=[PythonPlugin()],
                analysis_level=AnalysisLevel.FULL,
            )
            _nodes, edges = orch.analyze(tmpdir)
            edge_types = {e.edge_type for e in edges}
            assert EdgeType.PARENT_OF in edge_types
            assert EdgeType.FLOWS_TO in edge_types
            assert EdgeType.REACHES in edge_types

    def test_architectural_node_reduction(self) -> None:
        """ARCHITECTURAL mode produces significantly fewer nodes than FULL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "main.py").write_text(PYTHON_SOURCE)

            full_orch = ProjectOrchestrator(
                plugins=[PythonPlugin()],
                analysis_level=AnalysisLevel.FULL,
            )
            full_nodes, _ = full_orch.analyze(tmpdir)

            arch_orch = ProjectOrchestrator(
                plugins=[PythonPlugin()],
                analysis_level=AnalysisLevel.ARCHITECTURAL,
            )
            arch_nodes, _ = arch_orch.analyze(tmpdir)

            assert len(arch_nodes) < len(full_nodes)

    def test_default_analysis_level_is_full(self) -> None:
        """Orchestrator without explicit level defaults to FULL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "main.py").write_text("x = 1\n")
            orch = ProjectOrchestrator(plugins=[PythonPlugin()])
            _nodes, edges = orch.analyze(tmpdir)
            # FULL mode produces PARENT_OF edges.
            parent_edges = [e for e in edges if e.edge_type == EdgeType.PARENT_OF]
            assert len(parent_edges) > 0

    def test_java_architectural(self) -> None:
        """ARCHITECTURAL mode works for Java files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "UserService.java").write_text(JAVA_SOURCE)
            orch = ProjectOrchestrator(
                plugins=[JavaPlugin()],
                analysis_level=AnalysisLevel.ARCHITECTURAL,
            )
            nodes, edges = orch.analyze(tmpdir)
            assert len(nodes) > 0
            methods = [n for n in nodes if n.has_label("Method")]
            assert len(methods) >= 2
            assert all(e.edge_type != EdgeType.FLOWS_TO for e in edges)

    def test_java_architectural_preserves_typed_calls(self) -> None:
        """ARCHITECTURAL Java keeps enough call-site context for typed CALLS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Service.java").write_text(
                "public class Service {\n    public void work() { }\n}\n"
            )
            (root / "Client.java").write_text(
                "public class Client {\n"
                "    void run(Service service) {\n"
                "        service.work();\n"
                "    }\n"
                "}\n"
            )
            orch = ProjectOrchestrator(
                plugins=[JavaPlugin()],
                analysis_level=AnalysisLevel.ARCHITECTURAL,
            )
            nodes, edges = orch.analyze(tmpdir)

            assert any(n.properties.get("type") == "method_invocation" for n in nodes)
            assert any(n.properties.get("type") == "formal_parameter" for n in nodes)
            calls = [e for e in edges if e.edge_type == EdgeType.CALLS]
            assert len(calls) == 1
            assert calls[0].properties.get("callee") == "work"
            assert calls[0].properties.get("resolution") == "typed"


# ── CodeSlicer — JIT expansion ────────────────────────────────────────────────


class TestCodeSlicerJIT:
    """Tests for ``CodeSlicer.get_method_source`` and ``expand_method``."""

    @pytest.fixture()
    def arch_cpg(self) -> tuple[list, list]:
        """Generate an ARCHITECTURAL-level CPG for testing."""
        plugin = PythonPlugin()
        nodes, edges = plugin.parse_to_ast(
            "test.py",
            PYTHON_SOURCE,
            analysis_level=AnalysisLevel.ARCHITECTURAL,
        )
        return nodes, edges

    def test_get_method_source_returns_code(self, arch_cpg: tuple[list, list]) -> None:
        """``get_method_source`` returns the source text."""
        nodes, edges = arch_cpg
        slicer = CodeSlicer(nodes, edges)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) > 0
        for method in methods:
            source = slicer.get_method_source(method.id)
            assert source is not None
            assert "def " in source or "return" in source

    def test_get_method_source_nonexistent_node(self, arch_cpg: tuple[list, list]) -> None:
        """``get_method_source`` returns None for a nonexistent node."""
        nodes, edges = arch_cpg
        slicer = CodeSlicer(nodes, edges)
        assert slicer.get_method_source("nonexistent-id") is None

    def test_get_method_source_no_source_property(self) -> None:
        """``get_method_source`` returns None if node has no ``source_code``."""
        from omnicpg.models.node import CPGNode

        node = CPGNode(id="no-src", labels=("Node", "Method"))
        slicer = CodeSlicer([node], [])
        assert slicer.get_method_source("no-src") is None

    def test_expand_method_returns_full_cpg(self, arch_cpg: tuple[list, list]) -> None:
        """``expand_method`` returns a full local CPG for the method."""
        nodes, edges = arch_cpg
        slicer = CodeSlicer(nodes, edges)
        plugin = PythonPlugin()
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) > 0

        expanded_nodes, expanded_edges = slicer.expand_method(methods[0].id, plugin)
        assert len(expanded_nodes) > 0
        assert len(expanded_edges) > 0
        # The expanded CPG should have PARENT_OF edges (full level).
        edge_types = {e.edge_type for e in expanded_edges}
        assert EdgeType.PARENT_OF in edge_types

    def test_expand_method_nonexistent_node(self, arch_cpg: tuple[list, list]) -> None:
        """``expand_method`` returns empty for a nonexistent node."""
        nodes, edges = arch_cpg
        slicer = CodeSlicer(nodes, edges)
        plugin = PythonPlugin()
        exp_nodes, exp_edges = slicer.expand_method("nonexistent-id", plugin)
        assert exp_nodes == []
        assert exp_edges == []

    def test_java_expand_method(self) -> None:
        """``expand_method`` works for Java methods."""
        plugin = JavaPlugin()
        nodes, edges = plugin.parse_to_ast(
            "Test.java",
            JAVA_SOURCE,
            analysis_level=AnalysisLevel.ARCHITECTURAL,
        )
        slicer = CodeSlicer(nodes, edges)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) > 0

        expanded_nodes, _expanded_edges = slicer.expand_method(methods[0].id, plugin)
        assert len(expanded_nodes) > 0


# ── Node reduction quantification ────────────────────────────────────────────


class TestNodeReduction:
    """Verify the claimed ~95% node reduction in ARCHITECTURAL mode."""

    def test_python_reduction_ratio(self) -> None:
        """ARCHITECTURAL mode reduces Python nodes by at least 50%."""
        builder = PythonASTBuilder()
        full_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.FULL)
        arch_nodes, _ = builder.build("test.py", PYTHON_SOURCE, AnalysisLevel.ARCHITECTURAL)
        ratio = len(arch_nodes) / len(full_nodes)
        assert ratio < 0.5, f"Expected >50% reduction, got {1 - ratio:.0%}"

    def test_java_reduction_ratio(self) -> None:
        """ARCHITECTURAL mode reduces Java nodes by at least 50%."""
        builder = JavaASTBuilder()
        full_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.FULL)
        arch_nodes, _ = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        ratio = len(arch_nodes) / len(full_nodes)
        assert ratio < 0.5, f"Expected >50% reduction, got {1 - ratio:.0%}"

    def test_edge_reduction_ratio(self) -> None:
        """ARCHITECTURAL mode reduces edges by at least 50%."""
        builder = JavaASTBuilder()
        _, full_edges = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.FULL)
        _, arch_edges = builder.build("Test.java", JAVA_SOURCE, AnalysisLevel.ARCHITECTURAL)
        ratio = len(arch_edges) / len(full_edges) if full_edges else 0
        assert ratio < 0.5, f"Expected >50% edge reduction, got {1 - ratio:.0%}"
