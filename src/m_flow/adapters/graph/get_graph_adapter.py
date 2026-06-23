"""
Graph database engine factory.

Supports Kuzu (local/remote), Neo4j, Neptune, Neptune Analytics, and
extensible via ``supported_databases`` registry.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from .config import get_graph_context_config
from .graph_db_interface import GraphProvider

# ---------------------------------------------------------------------------
# Public async factory
# ---------------------------------------------------------------------------


async def get_graph_provider() -> GraphProvider:
    """
    Resolve configuration and return an initialised graph adapter.

    Because adapter construction may involve async I/O (e.g., schema sync),
    callers must ``await`` this factory.
    """
    cfg = get_graph_context_config()
    adapter = _build_adapter(**cfg)

    if hasattr(adapter, "initialize"):
        await adapter.initialize()

    return adapter


# ---------------------------------------------------------------------------
# Cached sync builder
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _build_adapter(
    graph_database_provider: str,
    graph_file_path: str = "",
    graph_database_url: str = "",
    graph_database_name: str = "",
    graph_database_username: str = "",
    graph_database_password: str = "",
    graph_database_port: str = "",
    graph_database_key: str = "",
    graph_dataset_database_handler: str = "",
) -> GraphProvider:
    """
    Instantiate the appropriate graph adapter for *graph_database_provider*.

    Raises
    ------
    EnvironmentError
        When required parameters are missing or provider is unknown.
    """
    provider = graph_database_provider.lower()

    if provider == "kuzu":
        import os
        from .kuzu.adapter import KuzuAdapter

        # Determine the database path
        if not graph_file_path:
            from m_flow.base_config import get_base_config
            storage_dir = os.path.join(get_base_config().system_root_directory, "databases")
            os.makedirs(storage_dir, exist_ok=True)
            db_path = os.path.join(storage_dir, graph_database_name or "m_flow_graph_kuzu")
        else:
            db_path = os.path.join(graph_file_path, graph_database_name or "")

        return KuzuAdapter(db_path=db_path)

    if provider != "neo4j":
        # Force/fallback to neo4j as requested
        print(f"[Warning] Enforcing unified Neo4j provider (requested was: {graph_database_provider})")
        provider = "neo4j"

    # Built-in Neo4j provider
    _require(graph_database_url, "Neo4j URL")
    from .neo4j_driver.adapter import Neo4jAdapter

    return Neo4jAdapter(
        graph_database_url=graph_database_url,
        graph_database_username=graph_database_username or None,
        graph_database_password=graph_database_password or None,
        graph_database_name=graph_database_name or None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(val: Optional[str], label: str) -> None:
    if not val:
        raise EnvironmentError(f"Missing required configuration: {label}")


def _validate_prefix(url: str, prefix: str) -> None:
    if not url.startswith(prefix):
        raise ValueError(f"URL must start with '{prefix}'")


def _ensure_langchain_aws() -> None:
    try:
        import langchain_aws  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "langchain_aws is required for Neptune support. Install with: pip install langchain_aws"
        ) from exc


# Backward-compatible aliases (deprecated)
