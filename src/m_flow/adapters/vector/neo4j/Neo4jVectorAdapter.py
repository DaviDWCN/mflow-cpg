"""
Neo4j adapter for vector storage.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

from neo4j import AsyncGraphDatabase

from m_flow.adapters.vector.vector_db_interface import VectorProvider
from m_flow.core import MemoryNode
from m_flow.shared.logging_utils import get_logger
from mflow_cpg.config import get_config

logger = get_logger(__name__)


class Neo4jVectorAdapter(VectorProvider):
    """
    Neo4j-backed vector database implementation.

    This maps the VectorProvider operations directly onto the
    Neo4j graph database, using vector indexes and APOC/GDS as necessary,
    ensuring a unified graph + vector persistence layer.
    """

    def __init__(self, embedding_engine: Any, uri: str = "", user: str = "", password: str = "", database: str = ""):
        self.embedding_engine = embedding_engine

        # Use provided config or fallback to unified configuration
        self.uri = uri or get_config().neo4j.uri
        self.user = user or get_config().neo4j.username
        self.password = password or get_config().neo4j.password
        self.database = database or get_config().neo4j.database

        self.driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password)
        )
        logger.info(f"Initialized Neo4jVectorAdapter connected to {self.uri}")

    # -------------------------------------------------------------------------
    # Collection management
    # -------------------------------------------------------------------------

    async def has_collection(self, collection_name: str) -> bool:
        # In Neo4j, a collection is a Label with a vector index.
        # We can query if the index exists.
        index_name = f"idx_vector_{collection_name}"
        query = "SHOW VECTOR INDEXES YIELD name, labelsOrTypes WHERE name = $index_name AND $collection_name IN labelsOrTypes RETURN count(*) AS count"
        async with self.driver.session(database=self.database) as session:
            result = await session.run(query, index_name=index_name, collection_name=collection_name)
            record = await result.single()
            if record and record["count"] > 0:
                return True
        return False

    async def create_collection(
        self,
        collection_name: str,
        payload_schema: Optional[Any] = None,
    ) -> None:
        index_name = f"idx_vector_{collection_name}"
        # We assume dimensions based on the embedding engine or use a default.
        dimensions = self.embedding_engine.get_dimensions() if hasattr(self.embedding_engine, "get_dimensions") else 768

        query = f"""
        CREATE VECTOR INDEX {index_name} IF NOT EXISTS
        FOR (n:`{collection_name}`) ON (n.embedding)
        OPTIONS {{
            indexConfig: {{
                `vector.dimensions`: {dimensions},
                `vector.similarity_function`: 'cosine'
            }}
        }}
        """
        async with self.driver.session(database=self.database) as session:
            await session.run(query)
            logger.info(f"Created vector index for {collection_name}")

    # -------------------------------------------------------------------------
    # Memory node CRUD
    # -------------------------------------------------------------------------

    async def create_memory_nodes(
        self,
        collection_name: str,
        memory_nodes: List[MemoryNode],
    ) -> None:
        if not memory_nodes:
            return

        # Embed all nodes first if they have text but no embedding
        # Though usually embeddings are passed inside the node or caller handles it.
        # In M-Flow, the caller expects to pass text, but MemoryNode has extract_index_text.
        texts_to_embed = []
        node_dicts = []
        for node in memory_nodes:
            text = MemoryNode.extract_index_text(node)
            texts_to_embed.append(text or "")

            node_dict = node.model_dump(mode="json")
            # Convert dict/list metadata into strings for Neo4j properties
            for k, v in node_dict.items():
                if isinstance(v, (dict, list)):
                    node_dict[k] = json.dumps(v)
            node_dicts.append(node_dict)

        # Generate embeddings in batch
        embeddings = await self.embed_data(texts_to_embed)

        for i, emb in enumerate(embeddings):
            if emb:
                node_dicts[i]["embedding"] = emb

        query = f"""
        UNWIND $nodes AS node
        MERGE (n:`{collection_name}` {{id: node.id}})
        SET n += node
        """
        async with self.driver.session(database=self.database) as session:
            await session.run(query, nodes=node_dicts)

    async def retrieve(
        self,
        collection_name: str,
        memory_node_ids: List[str],
    ) -> List[Dict[str, Any]]:
        if not memory_node_ids:
            return []

        query = f"""
        MATCH (n:`{collection_name}`)
        WHERE n.id IN $ids
        RETURN properties(n) AS props
        """
        results = []
        async with self.driver.session(database=self.database) as session:
            result = await session.run(query, ids=memory_node_ids)
            async for record in result:
                props = record["props"]
                # Clean up known properties (e.g. parse JSON strings back safely)
                for k in ["metadata"]:
                    v = props.get(k)
                    if isinstance(v, str):
                        try:
                            props[k] = json.loads(v)
                        except Exception:
                            pass
                results.append(props)
        return results

    async def delete_memory_nodes(
        self,
        collection_name: str,
        memory_node_ids: List[str],
    ) -> None:
        if not memory_node_ids:
            return

        query = f"""
        MATCH (n:`{collection_name}`)
        WHERE n.id IN $ids
        DELETE n
        """
        async with self.driver.session(database=self.database) as session:
            await session.run(query, ids=memory_node_ids)

    # -------------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------------

    async def search(
        self,
        collection_name: str,
        query_text: Optional[str] = None,
        query_vector: Optional[List[float]] = None,
        limit: Optional[int] = None,
        with_vector: bool = False,
        where_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        limit = limit or 10
        index_name = f"idx_vector_{collection_name}"

        if not query_vector and query_text:
            query_vector = (await self.embed_data([query_text]))[0]

        if not query_vector:
            # Fallback to normal text search if no vector or text provided
            return []

        # Vector search query with pre-filtering capability if needed
        # Since where_filter might be unsafe or require pre-filtering, we
        # use apoc or basic cypher to handle it cleanly without Cypher injection.
        # We will not directly inject `where_filter` into the query string.
        # For M-Flow context, where_filter is rarely used directly from users in a raw string way,
        # but to be safe we will ignore unstructured text where_filters or just not support arbitrary where_filters
        # and issue a warning. True SQL-like parsing to safe Cypher is beyond the scope here.
        if where_filter:
            logger.warning("where_filter is currently not supported in Neo4jVectorAdapter to prevent Cypher injection.")

        query = """
        CALL db.index.vector.queryNodes($index_name, $limit, $query_vector)
        YIELD node, score
        RETURN properties(node) AS props, score
        """

        results = []
        async with self.driver.session(database=self.database) as session:
            result = await session.run(query, index_name=index_name, limit=limit, query_vector=query_vector)
            async for record in result:
                props = record["props"]
                if not with_vector and "embedding" in props:
                    del props["embedding"]
                # Parse known JSON properties safely
                for k in ["metadata"]:
                    v = props.get(k)
                    if isinstance(v, str):
                        try:
                            props[k] = json.loads(v)
                        except Exception:
                            pass
                props["_score"] = record["score"]
                results.append(props)

        return results

    async def batch_search(
        self,
        collection_name: str,
        query_texts: List[str],
        limit: Optional[int] = None,
        with_vectors: bool = False,
    ) -> List[List[Dict[str, Any]]]:
        vectors = await self.embed_data(query_texts)
        results = []
        for vec in vectors:
            res = await self.search(
                collection_name=collection_name,
                query_vector=vec,
                limit=limit,
                with_vector=with_vectors
            )
            results.append(res)
        return results

    # -------------------------------------------------------------------------
    # Embedding
    # -------------------------------------------------------------------------

    async def embed_data(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        # The embed method differs slightly depending on the engine.
        # Typically M-Flow embedding engines provide an async `embed` or `embed_documents`.
        if hasattr(self.embedding_engine, "embed_documents"):
            return await self.embedding_engine.embed_documents(texts)
        elif hasattr(self.embedding_engine, "embed"):
            return [await self.embedding_engine.embed(t) for t in texts]
        else:
            # Synchronous fallback or generic
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, self.embedding_engine, texts)
            except Exception:
                # Direct call fallback
                return self.embedding_engine(texts)

    # -------------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------------

    async def prune(self) -> None:
        # In a unified graph, pruning needs to be careful not to delete CPG data.
        # This implementation can be a no-op or clear orphaned memory nodes.
        pass

    # -------------------------------------------------------------------------
    # Optional extension points
    # -------------------------------------------------------------------------

    async def get_connection(self) -> Any:
        return self.driver

    async def get_collection(self, collection_name: str) -> Any:
        return collection_name

    async def create_vector_index(
        self,
        index_name: str,
        index_property_name: str,
    ) -> None:
        # Generic create index
        pass

    async def index_memory_nodes(
        self,
        index_name: str,
        index_property_name: str,
        memory_nodes: List[MemoryNode],
    ) -> None:
        pass

    def get_memory_node_schema(self, model_type: Any) -> Any:
        return model_type

    # -------------------------------------------------------------------------
    # Multi-tenancy hooks
    # -------------------------------------------------------------------------

    @classmethod
    async def create_dataset(
        cls,
        dataset_id: Optional[UUID],
        user: Optional[Any],
    ) -> Dict[str, Any]:
        return {}

    async def delete_dataset(
        self,
        dataset_id: UUID,
        user: Any,
    ) -> None:
        pass
