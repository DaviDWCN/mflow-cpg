"""Unit tests for the PythonPlugin (end-to-end through the plugin interface)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from omnicpg.models.edge import EdgeType
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator
from omnicpg.plugins.python_plugin import PythonPlugin


class TestPythonPlugin:
    """Tests for :class:`PythonPlugin` via its public interface."""

    def test_supported_extensions(self) -> None:
        """The plugin declares ``.py`` as its supported extension."""
        plugin = PythonPlugin()
        assert plugin.supported_extensions == [".py"]

    def test_parse_to_ast(self) -> None:
        """``parse_to_ast`` returns nodes and structural edges."""
        plugin = PythonPlugin()
        nodes, edges = plugin.parse_to_ast("test.py", "x = 1\n")
        assert len(nodes) > 0
        allowed = {EdgeType.PARENT_OF, EdgeType.CONTAINS, EdgeType.DEPENDS_ON}
        assert all(e.edge_type in allowed for e in edges)

    def test_build_cfg(self) -> None:
        """``build_cfg`` returns FLOWS_TO edges for functions."""
        plugin = PythonPlugin()
        nodes, ast_edges = plugin.parse_to_ast("test.py", "def f():\n    x = 1\n    return x\n")
        cfg_edges = plugin.build_cfg(nodes, ast_edges)
        assert len(cfg_edges) > 0
        assert all(e.edge_type == EdgeType.FLOWS_TO for e in cfg_edges)

    def test_build_dfg(self) -> None:
        """``build_dfg`` returns REACHES edges."""
        plugin = PythonPlugin()
        nodes, ast_edges = plugin.parse_to_ast("test.py", "def f():\n    x = 1\n    return x\n")
        cfg_edges = plugin.build_cfg(nodes, ast_edges)
        dfg_edges = plugin.build_dfg(nodes, cfg_edges, ast_edges)
        assert len(dfg_edges) > 0
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)

    def test_end_to_end_via_orchestrator(self) -> None:
        """Full pipeline through ProjectOrchestrator with the real PythonPlugin."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "sample.py").write_text(
                "def greet(name):\n    msg = 'Hi ' + name\n    return msg\n"
            )
            orchestrator = ProjectOrchestrator(plugins=[PythonPlugin()])
            nodes, edges = orchestrator.analyze(tmpdir)

            assert len(nodes) > 0
            assert len(edges) > 0

            edge_types = {e.edge_type for e in edges}
            assert EdgeType.PARENT_OF in edge_types
            assert EdgeType.FLOWS_TO in edge_types
            assert EdgeType.REACHES in edge_types
