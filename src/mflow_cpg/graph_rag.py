"""
GraphRAG Module for M-Flow × OmniCPG.
Provides community detection, LLM summarization, and community-based GraphRAG retrieval.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import time
import random
from typing import Any, Dict, List, Optional
import networkx as nx

from m_flow.retrieval.base_retriever import BaseRetriever
from m_flow.retrieval.registered_community_retrievers import register_community_retriever
from mflow_cpg.config import get_config

logger = logging.getLogger("GraphRAG")


def _retry_with_backoff(func, max_retries=3, base_delays=(2.0, 4.0, 8.0)):
    attempt = 0
    while attempt < max_retries:
        try:
            return func()
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < max_retries - 1:
                delay = base_delays[attempt] if attempt < len(base_delays) else base_delays[-1]
                jitter = random.uniform(0.1, 0.5)
                time.sleep(delay + jitter)
                attempt += 1
            else:
                raise
        except urllib.error.URLError:
            raise
    raise urllib.error.URLError("Max retries exceeded")


class GraphRAGManager:
    def __init__(self, adapter: Any):
        self.adapter = adapter
        self.config = get_config()
        self.api_base = self.config.semantic_analysis.api_base.rstrip("/")
        self.api_key = self.config.semantic_analysis.api_key
        self.model = self.config.semantic_analysis.model
        self.embedding_model = self.config.semantic_analysis.embedding_model

    def _fetch_embedding(self, text: str) -> Optional[List[float]]:
        if not text or not text.strip():
            return None
        url = f"{self.api_base}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = {
            "model": self.embedding_model,
            "input": text,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        def fetch():
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "data" in result and len(result["data"]) > 0:
                    embedding = result["data"][0].get("embedding")
                    if isinstance(embedding, list):
                        return [float(x) for x in embedding]
            return None

        try:
            return _retry_with_backoff(fetch)
        except Exception as e:
            logger.debug(f"Failed to fetch embedding: {e}")
            return None

    def _fetch_llm_response(self, prompt: str, system_prompt: str) -> Optional[str]:
        url = f"{self.api_base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 500,
        }
        
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        def fetch():
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0].get("message", {}).get("content", "").strip()
                    return content if content else None
            return None

        try:
            return _retry_with_backoff(fetch)
        except Exception as e:
            logger.debug(f"Failed to fetch LLM response: {e}")
            return None

    def detect_communities(self, project_id: str) -> int:
        """
        Detect communities in the project graph.
        Tries Neo4j GDS Louvain first; falls back to NetworkX Louvain in Python.
        """
        logger.info(f"Running community detection for project {project_id}")
        
        # 1. Attempt GDS
        gds_success = False
        try:
            # Check GDS existence
            gds_check = self.adapter.query("CALL gds.list() YIELD name RETURN count(*) AS cnt")
            if gds_check and gds_check[0].get("cnt", 0) > 0:
                logger.info("Neo4j GDS library detected. Attempting GDS projection and Louvain run.")
                graph_name = f"graph-{project_id}"
                
                # Drop graph projection if exists
                try:
                    self.adapter.query(f"CALL gds.graph.drop('{graph_name}', false)")
                except Exception:
                    pass
                
                # Cypher projection
                self.adapter.query(
                    """
                    CALL gds.graph.project.cypher(
                      $graph_name,
                      'MATCH (n:Node) WHERE n.project_id = $project_id RETURN id(n) AS id, labels(n) AS labels',
                      'MATCH (n1:Node)-[r]->(n2:Node) WHERE n1.project_id = $project_id AND n2.project_id = $project_id RETURN id(n1) AS source, id(n2) AS target, type(r) AS type'
                    )
                    """,
                    graph_name=graph_name,
                    project_id=project_id,
                )
                
                # Run Louvain write back
                self.adapter.query(
                    """
                    CALL gds.louvain.write(
                      $graph_name,
                      {
                        writeProperty: 'community_id'
                      }
                    )
                    """,
                    graph_name=graph_name,
                )
                
                # Clean up projection
                self.adapter.query(f"CALL gds.graph.drop('{graph_name}')")
                
                # Convert the raw numerical IDs to formatted community IDs
                self.adapter.query(
                    """
                    MATCH (n:Node)
                    WHERE n.project_id = $project_id AND n.community_id IS NOT NULL
                    SET n.community_id = 'comm-' + $project_id + '-' + toString(n.community_id)
                    """,
                    project_id=project_id,
                )
                
                gds_success = True
                logger.info("Successfully completed GDS Louvain community writeback.")
        except Exception as e:
            logger.warning(f"Neo4j GDS Louvain execution failed: {e}. Falling back to Python-based Louvain clustering.")

        if gds_success:
            # Return number of detected communities
            cnt_res = self.adapter.query(
                "MATCH (n:Node) WHERE n.project_id = $project_id RETURN count(distinct n.community_id) AS cnt",
                project_id=project_id
            )
            return cnt_res[0].get("cnt", 0) if cnt_res else 0

        # 2. Python Fallback using NetworkX
        nodes = self.adapter.query(
            "MATCH (n:Node) WHERE n.project_id = $project_id RETURN n.id AS id",
            project_id=project_id
        )
        edges = self.adapter.query(
            """
            MATCH (n1:Node)-[r]->(n2:Node)
            WHERE n1.project_id = $project_id AND n2.project_id = $project_id
            RETURN n1.id AS source, n2.id AS target
            """,
            project_id=project_id
        )
        
        if not nodes:
            logger.warning("No nodes found for project. Skipping community detection.")
            return 0

        G = nx.Graph()
        for node in nodes:
            G.add_node(node["id"])
        for edge in edges:
            G.add_edge(edge["source"], edge["target"])

        try:
            from networkx.algorithms.community import louvain_communities
        except ImportError:
            from networkx.community import louvain_communities
        try:
            comms = louvain_communities(G)
        except Exception as e:
            logger.error(f"NetworkX Louvain communities failed: {e}")
            return 0

        # Write back community_id
        updates = []
        for idx, comm in enumerate(comms):
            comm_id = f"comm-{project_id}-{idx}"
            for nid in comm:
                updates.append({"id": nid, "community_id": comm_id})

        # Batch write to Neo4j
        batch_size = 1000
        for start in range(0, len(updates), batch_size):
            batch = updates[start : start + batch_size]
            self.adapter.query(
                """
                UNWIND $rows AS row
                MATCH (n:Node {id: row.id})
                SET n.community_id = row.community_id
                """,
                rows=batch,
            )

        logger.info(f"Python fallback Louvain: wrote {len(updates)} community IDs across {len(comms)} communities.")
        return len(comms)

    def generate_community_summaries(self, project_id: str) -> dict[str, Any]:
        """
        Generate LLM hierarchical summaries for communities in project,
        compute embeddings, and store them as CommunitySummary nodes in Neo4j.
        """
        # Ensure vector index for CommunitySummary
        try:
            self.adapter.query(
                "CREATE VECTOR INDEX community_summary_index IF NOT EXISTS "
                "FOR (c:CommunitySummary) ON (c.summary_embedding) "
                "OPTIONS {indexConfig: "
                "{`vector.dimensions`: 768, `vector.similarity_function`: 'cosine'}}"
            )
        except Exception:
            pass

        # Retrieve nodes grouped by community_id
        rows = self.adapter.query(
            """
            MATCH (n:Node)
            WHERE n.project_id = $project_id AND n.community_id IS NOT NULL
            RETURN n.community_id AS community_id, 
                   collect({
                       id: n.id, 
                       name: n.name, 
                       type: n.type,
                       labels: labels(n),
                       intent: coalesce(n.semantic_intent, n.semantic_high_level_intent, "")
                   }) AS nodes
            """,
            project_id=project_id,
        )

        if not rows:
            logger.warning("No communities found to summarize.")
            return {"communities_summarized": 0}

        summarized_count = 0
        for row in rows:
            community_id = row["community_id"]
            nodes_info = row["nodes"]
            
            # Simple heuristic: sort by name, limit to 50 nodes to avoid context limits
            if len(nodes_info) > 50:
                nodes_info = nodes_info[:50]
                
            entities_str_list = []
            for n in nodes_info:
                labels_str = ",".join(n.get("labels") or [])
                desc = f"- {n.get('name')} ({n.get('type') or labels_str})"
                if n.get("intent"):
                    desc += f": {n.get('intent')}"
                entities_str_list.append(desc)
            entities_str = "\n".join(entities_str_list)

            system_prompt = (
                "You are an expert software architect. Analyze the following code entities "
                "and cognitive memories belonging to a single community in a software system. "
                "Summarize the high-level role, architectural purpose, and business domain "
                "of this community in 2-3 paragraphs. Do not include introductory or boilerplate text."
            )
            
            user_prompt = f"Community ID: {community_id}\n\nEntities:\n{entities_str}"
            
            logger.info(f"Generating summary for community {community_id} containing {len(nodes_info)} elements...")
            summary = self._fetch_llm_response(user_prompt, system_prompt)
            if not summary:
                continue

            # Generate embedding
            embedding = self._fetch_embedding(summary)
            
            # Save CommunitySummary node
            name = f"Community {community_id.split('-')[-1]}"
            self.adapter.query(
                """
                MERGE (c:CommunitySummary {project_id: $project_id, community_id: $community_id})
                SET c.summary = $summary,
                    c.name = $name,
                    c.summary_embedding = $embedding
                """,
                project_id=project_id,
                community_id=community_id,
                summary=summary,
                name=name,
                embedding=embedding,
            )

            # Link Member Nodes to CommunitySummary
            self.adapter.query(
                """
                MATCH (n:Node) 
                WHERE n.project_id = $project_id AND n.community_id = $community_id
                MATCH (c:CommunitySummary {project_id: $project_id, community_id: $community_id})
                MERGE (n)-[:MEMBER_OF]->(c)
                """,
                project_id=project_id,
                community_id=community_id,
            )
            summarized_count += 1

        logger.info(f"Generated summaries and linked elements for {summarized_count} communities.")
        return {"communities_summarized": summarized_count}


class GraphRAGRetriever(BaseRetriever):
    """
    GraphRAGRetriever retrieves contextual information about communities
    by performing vector similarity searches over community summaries in Neo4j.
    """

    def __init__(self, user_prompt_path: str = "retrieval_context.txt", system_prompt_path: str = "direct_answer.txt", top_k: int = 3):
        self.user_prompt = user_prompt_path
        self.system_prompt = system_prompt_path
        self.top_k = top_k

    async def get_context(self, query: str) -> Any:
        from m_flow.adapters.graph import get_graph_provider
        db = await get_graph_provider()
        
        # Instantiate manager to access embedding generation
        manager = GraphRAGManager(db)
        query_vector = manager._fetch_embedding(query)
        if not query_vector:
            return f"Failed to generate embedding for query '{query}'."

        # Query vector search index on CommunitySummary
        search_query = """
        CALL db.index.vector.queryNodes("community_summary_index", $top_k, $query_vector)
        YIELD node, score
        RETURN node.community_id AS community_id, node.name AS name, node.summary AS summary, score
        """
        rows = await db.query(search_query, {"top_k": self.top_k, "query_vector": query_vector})
        
        if not rows:
            return f"No related communities found for query '{query}'."

        formatted_contexts = []
        for row in rows:
            community_id = row["community_id"]
            name = row["name"]
            summary = row["summary"]
            score = row["score"]

            # Query key member classes/methods in this community
            members_query = """
            MATCH (n:Node)-[:MEMBER_OF]->(c:CommunitySummary {community_id: $community_id})
            WHERE (n:Class OR n:Method OR n:Interface)
            RETURN n.name AS name, labels(n) AS labels
            LIMIT 10
            """
            member_rows = await db.query(members_query, {"community_id": community_id})
            members_str = ""
            if member_rows:
                member_names = [f"{m['name']} ({','.join(m['labels'])})" for m in member_rows]
                members_str = "\nKey Member Entities:\n" + "\n".join(f"- {m}" for m in member_names)

            formatted_contexts.append(
                f"### GraphRAG Community: {name} (Relevance Score: {score:.4f})\n"
                f"Community ID: {community_id}\n\n"
                f"{summary}\n"
                f"{members_str}"
            )

        return "\n\n---\n\n".join(formatted_contexts)

    async def get_completion(self, query: str, context: Optional[Any] = None, session_id: Optional[str] = None) -> Any:
        if context is None:
            context = await self.get_context(query)
        return context


# Register Retriever with M-Flow community registry
register_community_retriever("GRAPH_RAG", GraphRAGRetriever)
