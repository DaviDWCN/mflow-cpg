"""
Vector database engine factory.

Creates vector store adapters based on provider configuration.
"""

from __future__ import annotations

from functools import lru_cache

from .embeddings import get_embedding_engine
from .supported_databases import supported_databases


@lru_cache
def create_vector_engine(
    vector_db_provider: str,
    vector_db_url: str,
    vector_db_name: str,
    vector_db_port: str = "",
    vector_db_key: str = "",
    vector_dataset_database_handler: str = "",
):
    """
    Instantiate vector database adapter.

    Supports: Neo4j and any registered custom providers.

    Args:
        vector_db_provider: Provider name.
        vector_db_url: Connection URL.
        vector_db_name: Database name.
        vector_db_port: Connection port.
        vector_db_key: API key.
        vector_dataset_database_handler: Handler class name.

    Returns:
        Configured vector database adapter.

    Raises:
        EnvironmentError: Missing credentials.
        ImportError: Missing dependencies.
    """
    embedder = get_embedding_engine()

    # Check registered providers first
    if vector_db_provider in supported_databases:
        adapter_cls = supported_databases[vector_db_provider]
        return adapter_cls(
            url=vector_db_url,
            api_key=vector_db_key,
            embedding_engine=embedder,
            database_name=vector_db_name,
        )

    provider_lower = vector_db_provider.lower()

    # Neo4j - Graph Database with Vector Support
    if provider_lower == "neo4j":
        from .neo4j.Neo4jVectorAdapter import Neo4jVectorAdapter

        # Will fallback to unified configuration inside if not provided here
        return Neo4jVectorAdapter(
            embedding_engine=embedder,
            uri=vector_db_url,
            user=vector_db_key, # Usually API Key is used for user/pass logic, but adapter uses config
        )

    # Unknown provider
    known = list(supported_databases.keys()) + [
        "Neo4j",
    ]
    raise EnvironmentError(f"Unknown vector provider: {vector_db_provider}. Supported: {', '.join(known)}")
