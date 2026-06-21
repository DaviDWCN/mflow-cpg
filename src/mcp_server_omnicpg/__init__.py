"""MCP Server for OmniCPG Neo4j integration.

This package provides an MCP (Model Context Protocol) server that enables
AI models to query Code Property Graph (CPG) data stored in Neo4j.
"""

from __future__ import annotations

from mcp_server_omnicpg.config import Config
from mcp_server_omnicpg.neo4j_adapter import get_adapter

__all__ = ["Config", "get_adapter"]
__version__ = "0.1.0"
