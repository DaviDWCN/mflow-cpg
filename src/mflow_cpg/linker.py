"""
ConceptToCodeLinker - establishes bidrectional connections in Neo4j between
M-Flow Entity nodes (business concepts) and OmniCPG Node nodes (source code structures).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from m_flow.adapters.graph import get_graph_provider

logger = logging.getLogger("ConceptToCodeLinker")

class ConceptToCodeLinker:
    def __init__(self, adapter: Any):
        """
        Initialize with a Neo4jAdapter.
        """
        self.adapter = adapter

    async def link_concepts_and_code(self, project_id: str) -> Dict[str, Any]:
        """
        Scans all Entities in the graph and matches them to code structures (Class/Method/Field)
        belonging to the specified project_id. Creates bidrectional links:
        - (Entity)-[:IMPLEMENTED_BY]->(Node)
        - (Node)-[:IMPLEMENTS_CONCEPT]->(Entity)
        """
        # 1. Fetch all Entities from M-Flow
        entities_query = """
        MATCH (e:Entity)
        RETURN e.name AS name, e.canonical_name AS canonical_name
        """
        entities = await self.adapter.query(entities_query)
        if not entities:
            logger.info("No M-Flow entities found to link.")
            return {"status": "no_entities", "links_created": 0}

        links_created = 0
        logger.info(f"ConceptToCodeLinker: Scanning {len(entities)} entities against project '{project_id}'...")

        for entity in entities:
            name = entity.get("name")
            canonical_name = entity.get("canonical_name")
            
            names_to_match = {n for n in (name, canonical_name) if n}
            
            for match_name in names_to_match:
                # 2. Find matching code nodes (Class, Method, Field, Module) in Neo4j
                # Match by exact name or exact FQN suffix
                code_match_query = """
                MATCH (c:Node)
                WHERE c.project_id = $project_id
                  AND (c:Class OR c:Method OR c:Field OR c:Module)
                  AND (c.name = $match_name OR c.fqn ENDS WITH $match_name)
                RETURN c.id AS id, c.name AS name, labels(c) AS labels
                """
                code_nodes = await self.adapter.query(code_match_query, {"project_id": project_id, "match_name": match_name})
                
                if not code_nodes:
                    # Try a substring search if the entity name looks like a code class/method name (e.g. CamelCase or snake_case)
                    # We only do this if the name is > 4 characters to avoid false positives.
                    if len(match_name) > 4 and (match_name[0].isupper() or "_" in match_name):
                        fuzzy_match_query = """
                        MATCH (c:Node)
                        WHERE c.project_id = $project_id
                          AND (c:Class OR c:Method)
                          AND c.name CONTAINS $match_name
                        RETURN c.id AS id, c.name AS name, labels(c) AS labels
                        """
                        code_nodes = await self.adapter.query(fuzzy_match_query, {"project_id": project_id, "match_name": match_name})

                # 3. Create edges
                if code_nodes:
                    for c_node in code_nodes:
                        cpg_node_id = c_node["id"]
                        
                        link_query = """
                        MATCH (e:Entity) WHERE e.name = $entity_name
                        MATCH (c:Node {id: $cpg_node_id})
                        MERGE (e)-[r1:IMPLEMENTED_BY]->(c)
                        MERGE (c)-[r2:IMPLEMENTS_CONCEPT]->(e)
                        RETURN count(*) AS count
                        """
                        await self.adapter.query(
                            link_query, 
                            {"entity_name": name, "cpg_node_id": cpg_node_id}
                        )
                        links_created += 1
                        logger.info(f"Linked Entity '{name}' <-> Code Node '{c_node['name']}' ({cpg_node_id})")

        return {
            "status": "success",
            "entities_scanned": len(entities),
            "links_created": links_created
        }
