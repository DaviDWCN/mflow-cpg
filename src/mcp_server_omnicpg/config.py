"""Configuration management for MCP Neo4j server."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Configuration class for MCP Neo4j server.

    Reads configuration from environment variables with sensible defaults.
    Uses the same Neo4j configuration as the main OmniCPG project.
    """

    try:
        from mflow_cpg import get_config
        _unified_cfg = get_config()
        NEO4J_URI = _unified_cfg.neo4j.uri
        NEO4J_USER = _unified_cfg.neo4j.username
        NEO4J_PASSWORD = _unified_cfg.neo4j.password
    except Exception:
        NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
        NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

    # Project isolation: when set, every MCP query is scoped to this
    # project_id so a shared Neo4j instance never leaks cross-project data.
    PROJECT_ID: str | None = os.getenv("OMNICPG_PROJECT_ID") or None

    # MCP server settings
    MCP_PORT: int = int(os.getenv("MCP_PORT", "8080"))
    MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")

    # Query settings
    DEFAULT_QUERY_TIMEOUT: int = int(os.getenv("DEFAULT_QUERY_TIMEOUT", "30"))
    MAX_QUERY_DEPTH: int = int(os.getenv("MAX_QUERY_DEPTH", "5"))
    DEFAULT_QUERY_LIMIT: int = int(os.getenv("DEFAULT_QUERY_LIMIT", "10"))

    # Retry settings
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_DELAY_SECONDS: float = float(os.getenv("RETRY_DELAY_SECONDS", "2.0"))

    @classmethod
    def validate(cls) -> None:
        """Validate configuration settings.

        Raises:
            ValueError: If required settings are missing or invalid.
        """
        if not cls.NEO4J_URI:
            raise ValueError("NEO4J_URI must be set")
        if not cls.NEO4J_USER:
            raise ValueError("NEO4J_USER must be set")
        if not cls.NEO4J_PASSWORD:
            raise ValueError("NEO4J_PASSWORD must be set")

        if cls.DEFAULT_QUERY_TIMEOUT <= 0:
            raise ValueError("DEFAULT_QUERY_TIMEOUT must be positive")
        if cls.MAX_QUERY_DEPTH <= 0:
            raise ValueError("MAX_QUERY_DEPTH must be positive")
        if cls.DEFAULT_QUERY_LIMIT <= 0:
            raise ValueError("DEFAULT_QUERY_LIMIT must be positive")
