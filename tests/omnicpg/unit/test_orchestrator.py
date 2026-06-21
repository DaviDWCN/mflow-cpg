"""Unit tests for the ProjectOrchestrator."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from omnicpg.interfaces.language_plugin import LanguagePlugin
from omnicpg.models.node import CPGNode
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator

if TYPE_CHECKING:
    from omnicpg.models.analysis_level import AnalysisLevel
    from omnicpg.models.edge import CPGEdge

# ── Stub plugin ───────────────────────────────────────────────────────────────


class _StubPythonPlugin(LanguagePlugin):
    """Minimal plugin that records which files it was asked to process."""

    def __init__(self) -> None:
        self.parsed_files: list[str] = []

    @property
    def supported_extensions(self) -> list[str]:
        return [".py"]

    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        self.parsed_files.append(file_path)
        node = CPGNode(id="stub-node", labels=("Node",))
        return [node], []

    def build_cfg(
        self, ast_nodes: list[CPGNode], ast_edges: list[CPGEdge] | None = None
    ) -> list[CPGEdge]:
        return []

    def build_dfg(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        return []


class _StubJSPlugin(LanguagePlugin):
    """Stub for a hypothetical JavaScript plugin."""

    @property
    def supported_extensions(self) -> list[str]:
        return [".js", ".jsx"]

    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        node = CPGNode(id="js-node", labels=("Node",))
        return [node], []

    def build_cfg(
        self, ast_nodes: list[CPGNode], ast_edges: list[CPGEdge] | None = None
    ) -> list[CPGEdge]:
        return []

    def build_dfg(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        return []


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestProjectOrchestrator:
    """Tests for :class:`ProjectOrchestrator`."""

    def test_no_plugins_raises(self) -> None:
        """Constructing without any plugin is an error."""
        with pytest.raises(ValueError, match="At least one"):
            ProjectOrchestrator(plugins=[])

    def test_scan_nonexistent_dir_raises(self) -> None:
        """Scanning a missing directory raises FileNotFoundError."""
        orch = ProjectOrchestrator(plugins=[_StubPythonPlugin()])
        with pytest.raises(FileNotFoundError):
            orch.scan_directory("/nonexistent/path")

    def test_scan_file_instead_of_dir_raises(self) -> None:
        """Scanning a file (not a directory) raises ValueError."""
        orch = ProjectOrchestrator(plugins=[_StubPythonPlugin()])
        with tempfile.NamedTemporaryFile(suffix=".py") as tmp, pytest.raises(ValueError):
            orch.scan_directory(tmp.name)

    def test_scan_returns_matching_files(self) -> None:
        """Only files with registered extensions are returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "a.py").write_text("pass")
            (Path(tmpdir) / "b.txt").write_text("hello")
            (Path(tmpdir) / "c.py").write_text("x = 1")

            orch = ProjectOrchestrator(plugins=[_StubPythonPlugin()])
            files = orch.scan_directory(tmpdir)
            assert len(files) == 2
            assert all(f.suffix == ".py" for f in files)

    def test_route_selects_correct_plugin(self) -> None:
        """The orchestrator routes .py files to the Python plugin."""
        py_plugin = _StubPythonPlugin()
        js_plugin = _StubJSPlugin()
        orch = ProjectOrchestrator(plugins=[py_plugin, js_plugin])

        assert orch._route_file(Path("foo.py")) is py_plugin
        assert orch._route_file(Path("bar.js")) is js_plugin
        assert orch._route_file(Path("baz.txt")) is None

    def test_analyze_calls_all_pipeline_steps(self) -> None:
        """``analyze`` invokes parse_to_ast, build_cfg, build_dfg."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "main.py").write_text("x = 1\n")

            plugin = _StubPythonPlugin()
            orch = ProjectOrchestrator(plugins=[plugin])
            nodes, _edges = orch.analyze(tmpdir)

            assert len(plugin.parsed_files) == 1
            assert len(nodes) == 1
            assert nodes[0].id == "stub-node"

    def test_analyze_empty_directory(self) -> None:
        """Analyzing an empty directory yields no nodes or edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = ProjectOrchestrator(plugins=[_StubPythonPlugin()])
            nodes, edges = orch.analyze(tmpdir)
            assert nodes == []
            assert edges == []
