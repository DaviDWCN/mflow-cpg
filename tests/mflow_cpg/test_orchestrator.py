from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from m_flow.retrieval.memory_orchestrator import MemoryOrchestrator
from m_flow.knowledge.graph_ops.m_flow_graph.MemoryGraphElements import Edge, Node
from mflow_cpg.config import UnifiedConfig, RerankerSettings

def make_mock_edge(src_id: str, tgt_id: str, rel_type: str) -> Edge:
    node1 = Node(node_id=src_id, attributes={"type": "Fact", "text": f"Node {src_id}"})
    node2 = Node(node_id=tgt_id, attributes={"type": "Fact", "text": f"Node {tgt_id}"})
    edge = Edge(
        node1=node1,
        node2=node2,
        attributes={"relationship_name": rel_type, "edge_text": rel_type}
    )
    return edge

class MockResponseContextManager:
    def __init__(self, response):
        self.response = response
        
    async def __aenter__(self):
        return self.response
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

class MockClientSessionContextManager:
    def __init__(self, session):
        self.session = session
        
    async def __aenter__(self):
        return self.session
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

@pytest.mark.anyio
async def test_memory_orchestrator_reranker_integration():
    # Setup mock edges
    edge_a = make_mock_edge("a", "b", "REVEALS")
    edge_c = make_mock_edge("c", "d", "REVEALS")
    
    # Mock configs
    reranker_config = RerankerSettings(
        enabled=True,
        provider="ollama",
        model="bge-reranker-v2-m3",
        endpoint="http://localhost:11434/v1",
        api_key="ollama",
        top_n=2
    )
    unified_config = UnifiedConfig()
    unified_config.reranker = reranker_config
    
    # Instantiate orchestrator
    orchestrator = MemoryOrchestrator()
    orchestrator.config.enable_atomic = True
    orchestrator.config.enable_episodic = False
    orchestrator.config.enable_procedural = False
    
    # Mock _retrieve_atomic to return our mock edges
    orchestrator._retrieve_atomic = AsyncMock(return_value=[edge_a, edge_c])
    
    # Mock aiohttp ClientSession post response
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json.return_value = {
        "results": [
            {"index": 1, "relevance_score": 0.95}, # edge_c
            {"index": 0, "relevance_score": 0.35}  # edge_a
        ]
    }
    
    mock_session = MagicMock()
    mock_session.post.return_value = MockResponseContextManager(mock_resp)
    
    with patch("aiohttp.ClientSession", return_value=MockClientSessionContextManager(mock_session)):
        with patch("m_flow.shared.config_registry.get_global_config", return_value=unified_config):
            with patch("mflow_cpg.config.get_config", return_value=unified_config):
                # Call retrieve
                result = await orchestrator.retrieve("test query")
                
                # Assertions
                assert len(result.merged_edges) == 2
            # The order should be updated by the reranker (index 1 / edge_c should be first)
            assert result.merged_edges[0] == edge_c
            assert result.merged_edges[1] == edge_a
            
            # Verify POST request parameters
            mock_session.post.assert_called_once()
            called_args, called_kwargs = mock_session.post.call_args
            payload = called_kwargs["json"]
            assert payload["model"] == "bge-reranker-v2-m3"
            assert payload["query"] == "test query"
            assert len(payload["documents"]) == 2
