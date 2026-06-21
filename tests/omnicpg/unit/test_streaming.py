"""Unit tests for streaming analysis and parallel processing."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from omnicpg.interfaces.language_plugin import LanguagePlugin
from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode
from omnicpg.orchestrator.project_orchestrator import (
    ProjectOrchestrator,
    _extract_callee_name_from_code,
)

if TYPE_CHECKING:
    from omnicpg.models.analysis_level import AnalysisLevel

# ── Stub plugins ──────────────────────────────────────────────────────────────


class _CountingPlugin(LanguagePlugin):
    """Plugin that produces a predictable number of nodes per file."""

    def __init__(self) -> None:
        self.files_parsed: list[str] = []

    @property
    def supported_extensions(self) -> list[str]:
        return [".py"]

    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        self.files_parsed.append(file_path)
        node = CPGNode(
            id=f"node-{len(self.files_parsed)}",
            labels=("Node",),
            properties={"file_path": file_path, "type": "module"},
        )
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


class _CallGraphPlugin(LanguagePlugin):
    """Plugin that produces method definitions and call sites for call-graph testing."""

    def __init__(self) -> None:
        self._call_counter = 0

    @property
    def supported_extensions(self) -> list[str]:
        return [".py"]

    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        self._call_counter += 1
        nodes: list[CPGNode] = []
        # Every file produces a method definition.
        nodes.append(
            CPGNode(
                id=f"method-{self._call_counter}",
                labels=("Node", "Method"),
                properties={
                    "file_path": file_path,
                    "name": f"func_{self._call_counter}",
                    "type": "function_definition",
                },
            )
        )
        # The second file also calls the first file's function.
        if self._call_counter > 1:
            nodes.append(
                CPGNode(
                    id=f"call-{self._call_counter}",
                    labels=("Node",),
                    properties={
                        "file_path": file_path,
                        "type": "call",
                        "code": "func_1()",
                    },
                )
            )
        return nodes, []

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


class TestAnalyzeStreaming:
    """Tests for ``analyze_streaming``."""

    def test_streaming_yields_all_files(self) -> None:
        """Every file is processed across all chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(5):
                (Path(tmpdir) / f"file_{i}.py").write_text(f"x_{i} = {i}\n")

            plugin = _CountingPlugin()
            orch = ProjectOrchestrator(plugins=[plugin])

            chunks = list(orch.analyze_streaming(tmpdir, chunk_size=2))

            total_nodes = sum(len(nodes) for nodes, _ in chunks)
            assert total_nodes == 5
            assert len(plugin.files_parsed) == 5

    def test_streaming_chunk_size_one(self) -> None:
        """Processing one file at a time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                (Path(tmpdir) / f"file_{i}.py").write_text(f"x_{i} = {i}\n")

            plugin = _CountingPlugin()
            orch = ProjectOrchestrator(plugins=[plugin])

            chunks = list(orch.analyze_streaming(tmpdir, chunk_size=1))

            # _CountingPlugin produces no Method/call nodes → no call-graph chunk.
            node_counts = [len(nodes) for nodes, _ in chunks]
            assert node_counts == [1, 1, 1]

    def test_streaming_large_chunk(self) -> None:
        """A chunk larger than the total file count works fine."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                (Path(tmpdir) / f"file_{i}.py").write_text(f"x_{i} = {i}\n")

            plugin = _CountingPlugin()
            orch = ProjectOrchestrator(plugins=[plugin])

            chunks = list(orch.analyze_streaming(tmpdir, chunk_size=1000))

            total_nodes = sum(len(n) for n, _ in chunks)
            assert total_nodes == 3
            # _CountingPlugin produces no call-graph data → single chunk.
            assert len(chunks) == 1

    def test_streaming_empty_directory(self) -> None:
        """An empty directory yields zero chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = ProjectOrchestrator(plugins=[_CountingPlugin()])
            chunks = list(orch.analyze_streaming(tmpdir, chunk_size=10))
            assert chunks == []

    def test_streaming_invalid_chunk_size(self) -> None:
        """chunk_size < 1 raises ValueError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = ProjectOrchestrator(plugins=[_CountingPlugin()])
            with pytest.raises(ValueError, match="chunk_size"):
                list(orch.analyze_streaming(tmpdir, chunk_size=0))

    def test_streaming_cross_file_call_graph(self) -> None:
        """Call-graph edges appear in the final chunk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                (Path(tmpdir) / f"file_{i}.py").write_text(f"x_{i} = {i}\n")

            plugin = _CallGraphPlugin()
            orch = ProjectOrchestrator(plugins=[plugin])

            chunks = list(orch.analyze_streaming(tmpdir, chunk_size=1))

            # The last chunk should contain call-graph edges with empty nodes.
            last_nodes, last_edges = chunks[-1]
            assert last_nodes == []
            assert len(last_edges) > 0
            assert all(e.edge_type == EdgeType.CALLS for e in last_edges)

    def test_java_streaming_emits_v2_call_and_interprocedural_edges(self) -> None:
        """Java streaming keeps Method-to-Method CALLS metadata and arg/return flows."""
        from omnicpg.plugins.java_plugin import JavaPlugin

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Service.java").write_text(
                "public class Service {\n    public String work(String name) { return name; }\n}\n"
            )
            (root / "OtherService.java").write_text(
                "public class OtherService {\n"
                "    public String work(String name) { return name; }\n"
                "}\n"
            )
            (root / "Client.java").write_text(
                "public class Client {\n"
                "    public void run() {\n"
                "        Service s = new Service();\n"
                '        s.work("x");\n'
                "    }\n"
                "}\n"
            )

            orch = ProjectOrchestrator(plugins=[JavaPlugin()])
            nodes: list[CPGNode] = []
            edges: list[CPGEdge] = []
            for chunk_nodes, chunk_edges in orch.analyze_streaming(tmpdir, chunk_size=1):
                nodes.extend(chunk_nodes)
                edges.extend(chunk_edges)

            node_index = {node.id: node for node in nodes}
            calls = [
                edge
                for edge in edges
                if edge.edge_type == EdgeType.CALLS and edge.properties.get("callee") == "work"
            ]
            assert len(calls) == 1
            assert all(node_index[edge.source_id].has_label("Method") for edge in calls)
            assert all(node_index[edge.target_id].has_label("Method") for edge in calls)
            assert all(edge.properties.get("callsite_id") for edge in calls)
            assert calls[0].properties.get("resolution") == "typed"
            assert "Service.java" in str(
                node_index[calls[0].target_id].properties.get("file_path", "")
            )

            interprocedural_kinds = {
                edge.properties.get("interprocedural")
                for edge in edges
                if edge.edge_type == EdgeType.REACHES
            }
            assert {"argument", "return"} <= interprocedural_kinds

    def test_java_streaming_emits_method_reference_calls(self) -> None:
        """Java streaming preserves method-reference call sites."""
        from omnicpg.plugins.java_plugin import JavaPlugin

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Service.java").write_text(
                "public class Service {\n    public void work() { }\n}\n"
            )
            (root / "Client.java").write_text(
                "public class Client {\n"
                "    public void run(Service service) {\n"
                "        Runnable task = service::work;\n"
                "    }\n"
                "}\n"
            )

            orch = ProjectOrchestrator(plugins=[JavaPlugin()])
            nodes: list[CPGNode] = []
            edges: list[CPGEdge] = []
            for chunk_nodes, chunk_edges in orch.analyze_streaming(tmpdir, chunk_size=1):
                nodes.extend(chunk_nodes)
                edges.extend(chunk_edges)

            node_index = {node.id: node for node in nodes}
            calls = [
                edge
                for edge in edges
                if edge.edge_type == EdgeType.CALLS and edge.properties.get("callee") == "work"
            ]
            assert len(calls) == 1
            assert node_index[calls[0].source_id].has_label("Method")
            assert node_index[calls[0].target_id].has_label("Method")
            assert calls[0].properties.get("resolution") == "typed"
            assert calls[0].properties.get("call_kind") == "method_reference"

    def test_streaming_matches_analyze_node_count(self) -> None:
        """Streaming produces the same number of nodes as in-memory analyze."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(4):
                (Path(tmpdir) / f"file_{i}.py").write_text(f"x_{i} = {i}\n")

            # In-memory mode.
            plugin1 = _CountingPlugin()
            orch1 = ProjectOrchestrator(plugins=[plugin1])
            nodes_mem, _edges_mem = orch1.analyze(tmpdir)

            # Streaming mode.
            plugin2 = _CountingPlugin()
            orch2 = ProjectOrchestrator(plugins=[plugin2])
            total_streaming = sum(len(n) for n, _ in orch2.analyze_streaming(tmpdir, chunk_size=2))

            assert total_streaming == len(nodes_mem)


class TestParallelAnalysis:
    """Tests for multi-threaded file analysis."""

    def test_max_workers_validation(self) -> None:
        """max_workers < 1 is rejected."""
        with pytest.raises(ValueError, match="max_workers"):
            ProjectOrchestrator(plugins=[_CountingPlugin()], max_workers=0)

    def test_parallel_produces_same_results(self) -> None:
        """Parallel analysis produces the same node count as sequential."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(6):
                (Path(tmpdir) / f"f{i}.py").write_text(f"x = {i}\n")

            plugin_seq = _CountingPlugin()
            orch_seq = ProjectOrchestrator(plugins=[plugin_seq], max_workers=1)
            nodes_seq, _ = orch_seq.analyze(tmpdir)

            plugin_par = _CountingPlugin()
            orch_par = ProjectOrchestrator(plugins=[plugin_par], max_workers=4)
            nodes_par, _ = orch_par.analyze(tmpdir)

            assert len(nodes_par) == len(nodes_seq)

    def test_parallel_streaming(self) -> None:
        """Streaming + parallel works together."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(6):
                (Path(tmpdir) / f"f{i}.py").write_text(f"x = {i}\n")

            plugin = _CountingPlugin()
            orch = ProjectOrchestrator(plugins=[plugin], max_workers=2)
            total = sum(len(n) for n, _ in orch.analyze_streaming(tmpdir, chunk_size=3))
            assert total == 6


class TestExtractCalleeNameFromCode:
    """Tests for ``_extract_callee_name_from_code``."""

    def test_simple_call(self) -> None:
        """``func()`` → ``func``."""
        assert _extract_callee_name_from_code("func()") == "func"

    def test_qualified_call(self) -> None:
        """``obj.method()`` → ``method``."""
        assert _extract_callee_name_from_code("obj.method()") == "method"

    def test_no_parens(self) -> None:
        """No parentheses → ``None``."""
        assert _extract_callee_name_from_code("not_a_call") is None

    def test_empty_prefix(self) -> None:
        """Empty prefix → ``None``."""
        assert _extract_callee_name_from_code("()") is None

    def test_non_identifier(self) -> None:
        """Non-identifier prefix → ``None``."""
        assert _extract_callee_name_from_code("123()") is None

    def test_chained_attribute(self) -> None:
        """``a.b.c()`` → ``c``."""
        assert _extract_callee_name_from_code("a.b.c()") == "c"
