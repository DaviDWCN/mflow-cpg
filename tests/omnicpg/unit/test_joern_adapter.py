"""Unit tests for the JoernAdapter (CSV export and interface compliance)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import MappingProxyType

import pytest

from omnicpg.adapters.joern_adapter import JoernAdapter
from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode
from omnicpg.utils.id_gen import generate_id


@pytest.fixture()
def sample_nodes() -> list[CPGNode]:
    """Return a small set of CPG nodes for testing."""
    return [
        CPGNode(
            id=generate_id(),
            labels=("Node", "Method"),
            properties=MappingProxyType(
                {
                    "type": "function_definition",
                    "name": "greet",
                    "code": "def greet(name): ...",
                    "file_path": "test.py",
                    "line_start": 1,
                    "line_end": 2,
                }
            ),
        ),
        CPGNode(
            id=generate_id(),
            labels=("Node", "Variable"),
            properties=MappingProxyType(
                {
                    "type": "identifier",
                    "code": "name",
                    "file_path": "test.py",
                    "line_start": 1,
                    "line_end": 1,
                }
            ),
        ),
    ]


@pytest.fixture()
def sample_edges(sample_nodes: list[CPGNode]) -> list[CPGEdge]:
    """Return a small set of CPG edges for testing."""
    return [
        CPGEdge(
            source_id=sample_nodes[0].id,
            target_id=sample_nodes[1].id,
            edge_type=EdgeType.PARENT_OF,
        ),
    ]


class TestJoernAdapter:
    """Tests for :class:`JoernAdapter`."""

    def test_connect_local(self) -> None:
        """Connecting in local mode should succeed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JoernAdapter(output_dir=tmpdir)
            adapter.connect("local", ("", ""))
            adapter.disconnect()

    def test_not_connected_raises(self) -> None:
        """Operations before connect() should raise RuntimeError."""
        adapter = JoernAdapter()
        with pytest.raises(RuntimeError, match="Not connected"):
            adapter.insert_nodes([])

    def test_insert_nodes_creates_csv(self, sample_nodes: list[CPGNode]) -> None:
        """insert_nodes should write a nodes.csv file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JoernAdapter(output_dir=tmpdir)
            adapter.connect("local", ("", ""))
            adapter.insert_nodes(sample_nodes)

            csv_path = Path(tmpdir) / "nodes.csv"
            assert csv_path.exists()

            rows = adapter.read_exported_nodes()
            assert len(rows) == 2
            assert rows[0]["id"] == sample_nodes[0].id

    def test_insert_edges_creates_csv(
        self,
        sample_nodes: list[CPGNode],
        sample_edges: list[CPGEdge],
    ) -> None:
        """insert_edges should write an edges.csv file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JoernAdapter(output_dir=tmpdir)
            adapter.connect("local", ("", ""))
            adapter.insert_edges(sample_edges)

            csv_path = Path(tmpdir) / "edges.csv"
            assert csv_path.exists()

            rows = adapter.read_exported_edges()
            assert len(rows) == 1
            assert rows[0]["edge_type"] == "PARENT_OF"

    def test_clear_removes_csv(self, sample_nodes: list[CPGNode]) -> None:
        """clear() should delete exported CSV files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JoernAdapter(output_dir=tmpdir)
            adapter.connect("local", ("", ""))
            adapter.insert_nodes(sample_nodes)
            adapter.clear()

            assert not (Path(tmpdir) / "nodes.csv").exists()

    def test_query_without_server_raises(self) -> None:
        """query() in local mode should raise RuntimeError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JoernAdapter(output_dir=tmpdir)
            adapter.connect("local", ("", ""))
            with pytest.raises(RuntimeError, match="server connection"):
                adapter.query("cpg.method.name")

    def test_query_with_server_uri(self) -> None:
        """query() with a server URI should not raise (returns empty list)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JoernAdapter(output_dir=tmpdir)
            adapter.connect("http://localhost:8080", ("", ""))
            result = adapter.query("cpg.method.name")
            assert result == []

    def test_roundtrip_nodes(self, sample_nodes: list[CPGNode]) -> None:
        """Exported nodes should be readable back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JoernAdapter(output_dir=tmpdir)
            adapter.connect("local", ("", ""))
            adapter.insert_nodes(sample_nodes)

            rows = adapter.read_exported_nodes()
            assert len(rows) == len(sample_nodes)
            # Verify labels are colon-separated.
            assert "Node:Method" in rows[0]["labels"]
