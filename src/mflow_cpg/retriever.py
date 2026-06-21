"""
CPGRetriever - retrieves hybrid code structure and business context from unified Neo4j database.
Registers as the "CODE_GRAPH" recall mode in M-Flow.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Type
from fastapi.encoders import jsonable_encoder

from m_flow.adapters.graph import get_graph_provider
from m_flow.retrieval.base_retriever import BaseRetriever
from m_flow.retrieval.registered_community_retrievers import register_community_retriever

logger = logging.getLogger("CPGRetriever")

class CPGRetriever(BaseRetriever):
    """
    CPGRetriever merges business concepts and code structures
    by traversing M-Flow entities and OmniCPG nodes in Neo4j.
    """

    def __init__(self, user_prompt_path: str = "retrieval_context.txt", system_prompt_path: str = "direct_answer.txt"):
        self.user_prompt = user_prompt_path
        self.system_prompt = system_prompt_path

    async def get_context(self, query: str) -> Any:
        """
        Retrieves code context based on query (entity name, method/class symbol, or keyword).
        """
        try:
            db = await get_graph_provider()
            
            # 1. Look up M-Flow Entity -> Code Node mapping
            mapping_query = """
            MATCH (e:Entity)
            WHERE e.name = $query OR e.canonical_name = $query
            MATCH (e)-[:IMPLEMENTED_BY|same_entity_as]->(c:Node)
            RETURN c.id AS cpg_node_id, labels(c) AS labels, c.name AS name, c.fqn AS fqn
            """
            rows = await db.query(mapping_query, {"query": query})
            
            cpg_nodes = []
            if rows:
                for row in rows:
                    cpg_nodes.append((row["cpg_node_id"], row["name"], row["labels"]))
            else:
                # 2. Fallback to direct symbol lookup on CPG nodes
                symbol_query = """
                MATCH (c:Node)
                WHERE (c:Method OR c:Class OR c:Module) 
                  AND (c.name = $query OR c.fqn = $query)
                RETURN c.id AS cpg_node_id, labels(c) AS labels, c.name AS name
                LIMIT 5
                """
                rows = await db.query(symbol_query, {"query": query})
                for row in rows:
                    cpg_nodes.append((row["cpg_node_id"], row["name"], row["labels"]))

            if not cpg_nodes:
                # 3. Fallback to full-text search
                fulltext_query = """
                CALL db.index.fulltext.queryNodes("code_fulltext", $query) YIELD node, score
                RETURN node.id AS cpg_node_id, labels(node) AS labels, node.name AS name
                LIMIT 3
                """
                try:
                    rows = await db.query(fulltext_query, {"query": query})
                    for row in rows:
                        cpg_nodes.append((row["cpg_node_id"], row["name"], row["labels"]))
                except Exception:
                    # Fulltext index might not be populated or created
                    pass

            if not cpg_nodes:
                return f"No code context found for query '{query}'."

            # 4. Fetch rich context for each node
            formatted_contexts = []
            for node_id, name, labels in cpg_nodes:
                label_set = set(labels)
                if "Method" in label_set:
                    # Method details: code, signature, complexity, caller/callee details
                    method_query = """
                    MATCH (m:Node {id: $node_id})
                    OPTIONAL MATCH (caller:Node)-[:CALLS]->(m)
                    OPTIONAL MATCH (m)-[:CALLS]->(callee:Node)
                    RETURN m.name AS name, m.fqn AS fqn, m.code AS code, m.source_code AS source_code,
                           m.signature AS signature, m.complexity AS complexity,
                           m.semantic_intent AS intent,
                           collect(distinct caller.fqn) AS callers,
                           collect(distinct callee.fqn) AS callees
                    """
                    m_details = await db.query(method_query, {"node_id": node_id})
                    if m_details:
                        det = m_details[0]
                        code = det.get("code") or det.get("source_code") or "No source code available"
                        intent = det.get("intent") or "No semantic intent summary available"
                        callers = ", ".join(det.get("callers") or []) or "None"
                        callees = ", ".join(det.get("callees") or []) or "None"
                        formatted_contexts.append(
                            f"Method: {det.get('fqn') or det.get('name')}\n"
                            f"Signature: {det.get('signature') or 'N/A'}\n"
                            f"Complexity: {det.get('complexity') or 'N/A'}\n"
                            f"Semantic Summary: {intent}\n"
                            f"Callers: {callers}\n"
                            f"Callees: {callees}\n"
                            f"Code:\n```\n{code}\n```"
                        )
                elif "Class" in label_set:
                    # Class details: fields, methods, subclass relations
                    class_query = """
                    MATCH (c:Node {id: $node_id})
                    OPTIONAL MATCH (c)-[:PARENT_OF|CONTAINS]->(m:Method)
                    OPTIONAL MATCH (c)-[:PARENT_OF|CONTAINS]->(f:Field)
                    RETURN c.name AS name, c.fqn AS fqn, c.superclass AS superclass,
                           c.semantic_intent AS intent,
                           c.semantic_side_effects AS side_effects,
                           collect(distinct m.name) AS methods,
                           collect(distinct f.name) AS fields
                    """
                    c_details = await db.query(class_query, {"node_id": node_id})
                    if c_details:
                        det = c_details[0]
                        intent = det.get("intent") or "No semantic intent summary available"
                        side_effects = det.get("side_effects") or "None"
                        superclass = det.get("superclass") or "None"
                        methods = ", ".join(det.get("methods") or []) or "None"
                        fields = ", ".join(det.get("fields") or []) or "None"
                        formatted_contexts.append(
                            f"Class: {det.get('fqn') or det.get('name')}\n"
                            f"Superclass: {superclass}\n"
                            f"Semantic Summary: {intent}\n"
                            f"Side Effects: {side_effects}\n"
                            f"Fields: {fields}\n"
                            f"Methods: {methods}"
                        )
                else:
                    # Generic node details
                    generic_query = """
                    MATCH (n:Node {id: $node_id})
                    RETURN n.name AS name, n.type AS type, n.file_path AS file_path, n.code AS code
                    """
                    n_details = await db.query(generic_query, {"node_id": node_id})
                    if n_details:
                        det = n_details[0]
                        formatted_contexts.append(
                            f"Code Entity: {det.get('name')} (Type: {det.get('type')})\n"
                            f"File Path: {det.get('file_path') or 'N/A'}\n"
                            f"Code Snippet:\n```\n{det.get('code') or 'N/A'}\n```"
                        )

            return "\n\n---\n\n".join(formatted_contexts)

        except Exception as e:
            logger.error(f"CPGRetriever error: {e}", exc_info=True)
            return f"Error retrieving code context: {e}"

    async def get_completion(self, query: str, context: Optional[Any] = None, session_id: Optional[str] = None) -> Any:
        if context is None:
            context = await self.get_context(query)
        return context

# Auto-register to M-Flow community registry
register_community_retriever("CODE_GRAPH", CPGRetriever)
