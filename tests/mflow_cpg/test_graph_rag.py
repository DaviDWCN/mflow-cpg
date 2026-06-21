"""Unit and integration tests for GraphRAG community clustering, LLM summarization, and retrieval."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from mflow_cpg.graph_rag import GraphRAGManager, GraphRAGRetriever


class MockAdapter:
    """Mock adapter for GraphRAG tests."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.gds_available = False
        self.mock_nodes = []
        self.mock_edges = []
        self.mock_communities_nodes = []

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append(cypher)
        self.params.append(params)

        # GDS list check
        if "CALL gds.list()" in cypher:
            if self.gds_available:
                return [{"cnt": 1}]
            else:
                return [{"cnt": 0}]

        # Nodes count query
        if "MATCH (n:Node) WHERE n.project_id = $project_id RETURN count(distinct n.community_id)" in cypher:
            return [{"cnt": 3}]

        # Node fetching for NetworkX fallback
        if "MATCH (n:Node) WHERE n.project_id = $project_id RETURN n.id AS id" in cypher:
            return self.mock_nodes

        # Edge fetching for NetworkX fallback
        if "MATCH (n1:Node)-[r]->(n2:Node)" in cypher:
            return self.mock_edges

        # Grouping nodes by community query
        if "MATCH (n:Node)" in cypher and "RETURN n.community_id AS community_id" in cypher:
            return self.mock_communities_nodes

        return []


@pytest.fixture()
def mock_adapter() -> MockAdapter:
    return MockAdapter()


def test_detect_communities_gds_path(mock_adapter: MockAdapter) -> None:
    mock_adapter.gds_available = True
    manager = GraphRAGManager(mock_adapter)
    count = manager.detect_communities("proj-test")
    
    assert count == 3
    assert any("gds.louvain.write" in q for q in mock_adapter.queries)
    assert any("gds.graph.project.cypher" in q for q in mock_adapter.queries)


def test_detect_communities_fallback_networkx(mock_adapter: MockAdapter) -> None:
    mock_adapter.gds_available = False
    mock_adapter.mock_nodes = [{"id": "n1"}, {"id": "n2"}, {"id": "n3"}]
    mock_adapter.mock_edges = [{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3"}]
    
    manager = GraphRAGManager(mock_adapter)
    count = manager.detect_communities("proj-test")
    
    assert count > 0
    assert any("SET n.community_id = row.community_id" in q for q in mock_adapter.queries)


@patch("mflow_cpg.graph_rag.urllib.request.urlopen")
def test_generate_community_summaries(mock_urlopen: MagicMock, mock_adapter: MockAdapter) -> None:
    # Mock LLM and Embedding responses
    summary_resp = MagicMock()
    summary_resp.read.return_value = b'{"choices": [{"message": {"content": "This is a mock community summary description."}}]}'
    summary_resp.__enter__.return_value = summary_resp

    embedding_resp = MagicMock()
    embedding_resp.read.return_value = b'{"data": [{"embedding": [0.1, 0.2, 0.3]}]}'
    embedding_resp.__enter__.return_value = embedding_resp

    def urlopen_side_effect(req, **kwargs):
        if "embeddings" in req.full_url:
            return embedding_resp
        return summary_resp

    mock_urlopen.side_effect = urlopen_side_effect

    mock_adapter.mock_communities_nodes = [
        {
            "community_id": "comm-test-0",
            "nodes": [
                {"id": "n1", "name": "ClassA", "type": "Class", "labels": ["Node", "Class"], "intent": "Logic A"},
                {"id": "n2", "name": "methodB", "type": "Method", "labels": ["Node", "Method"], "intent": "Logic B"},
            ]
        }
    ]

    manager = GraphRAGManager(mock_adapter)
    res = manager.generate_community_summaries("proj-test")
    
    assert res == {"communities_summarized": 1}
    assert any("MERGE (c:CommunitySummary" in q for q in mock_adapter.queries)
    assert any("MERGE (n)-[:MEMBER_OF]->(c)" in q for q in mock_adapter.queries)


class AsyncMockAdapter:
    """Async mock adapter for retriever testing."""
    def __init__(self):
        self.queries = []
        self.params = []
        self.results = {}

    async def query(self, cypher: str, params: dict[str, Any] = None) -> list[dict[str, Any]]:
        self.queries.append(cypher)
        self.params.append(params or {})
        
        if "db.index.vector.queryNodes" in cypher:
            return self.results.get("vector_search", [])
        if "MATCH (n:Node)-[:MEMBER_OF]->(c:CommunitySummary" in cypher:
            return self.results.get("members", [])
            
        return []


@pytest.mark.anyio
@patch("mflow_cpg.graph_rag.GraphRAGManager._fetch_embedding")
async def test_graph_rag_retriever(mock_fetch_embedding: MagicMock) -> None:
    mock_fetch_embedding.return_value = [0.1, 0.2, 0.3]
    
    async_adapter = AsyncMockAdapter()
    async_adapter.results["vector_search"] = [
        {
            "community_id": "comm-test-0",
            "name": "Community 0",
            "summary": "This community manages payment processing.",
            "score": 0.88,
        }
    ]
    async_adapter.results["members"] = [
        {"name": "PaymentService", "labels": ["Node", "Class"]},
        {"name": "processPayment", "labels": ["Node", "Method"]},
    ]

    with patch("m_flow.adapters.graph.get_graph_provider", return_value=async_adapter):
        retriever = GraphRAGRetriever(top_k=1)
        context = await retriever.get_context("payment functionality")
        
        assert "Community 0" in context
        assert "payment processing" in context
        assert "PaymentService (Node,Class)" in context
        assert "processPayment (Node,Method)" in context
        assert "Relevance Score: 0.88" in context
