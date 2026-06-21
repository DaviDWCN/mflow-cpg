"""Integration tests for the Neo4j adapter.

These tests require a running Neo4j instance and are marked with
``@pytest.mark.integration`` so they can be skipped in CI.

Set the following environment variables to configure the connection:

    NEO4J_URI      (default: bolt://localhost:7687)
    NEO4J_USER     (default: neo4j)
    NEO4J_PASSWORD (default: password)
"""

from __future__ import annotations

import os

import pytest

from omnicpg.adapters.neo4j_adapter import Neo4jAdapter
from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode

_NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
_NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
_NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password")


@pytest.mark.integration
class TestNeo4jAdapter:
    """Integration tests for :class:`Neo4jAdapter`."""

    @pytest.fixture(autouse=True)
    def _adapter(self) -> Neo4jAdapter:  # type: ignore[return]
        """Connect, clear, yield, then disconnect."""
        adapter = Neo4jAdapter(batch_size=100)
        try:
            adapter.connect(_NEO4J_URI, (_NEO4J_USER, _NEO4J_PASSWORD))
        except (ConnectionError, Exception):
            pytest.skip("Neo4j is not available")
        adapter.clear()
        self.adapter = adapter
        yield adapter  # type: ignore[misc]
        adapter.clear()
        adapter.disconnect()

    def test_insert_and_count_nodes(self) -> None:
        """Inserted nodes can be counted in the database."""
        nodes = [
            CPGNode(id="n1", labels=("Node", "Method")),
            CPGNode(id="n2", labels=("Node",)),
        ]
        self.adapter.insert_nodes(nodes)
        # Verify via raw query
        driver = self.adapter._get_driver()
        with driver.session() as session:
            result = session.run("MATCH (n:Node) RETURN count(n) AS cnt")
            assert result.single()["cnt"] == 2  # type: ignore[index]

    def test_insert_and_count_edges(self) -> None:
        """Inserted edges link the correct nodes."""
        nodes = [
            CPGNode(id="n1", labels=("Node",)),
            CPGNode(id="n2", labels=("Node",)),
        ]
        edges = [
            CPGEdge(source_id="n1", target_id="n2", edge_type=EdgeType.PARENT_OF),
        ]
        self.adapter.insert_nodes(nodes)
        self.adapter.insert_edges(edges)

        driver = self.adapter._get_driver()
        with driver.session() as session:
            result = session.run("MATCH ()-[r:PARENT_OF]->() RETURN count(r) AS cnt")
            assert result.single()["cnt"] == 1  # type: ignore[index]

    def test_clear_removes_everything(self) -> None:
        """``clear()`` leaves the database empty."""
        self.adapter.insert_nodes([CPGNode(id="n1", labels=("Node",))])
        self.adapter.clear()
        driver = self.adapter._get_driver()
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) AS cnt")
            assert result.single()["cnt"] == 0  # type: ignore[index]
