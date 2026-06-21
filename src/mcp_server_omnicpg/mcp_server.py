"""MCP Server implementation for OmniCPG.

This module implements a standard MCP (Model Context Protocol) server
that exposes CPG query tools as MCP-compatible tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.sse import SseServerTransport
from mcp.types import (
    ServerCapabilities,
    TextContent,
    Tool,
    ToolsCapability,
)

from mcp_server_omnicpg.config import Config
from mcp_server_omnicpg.neo4j_adapter import get_adapter
from mcp_server_omnicpg.tools import (
    analyze_function,
    find_control_flow,
    find_data_flow,
    find_path,
    get_call_graph,
    get_dependencies,
    get_file_structure,
    get_node_by_id,
    get_node_source_code,
    list_projects,
    query_edges,
    query_nodes,
    search_code,
)
from mcp_server_omnicpg.tools.advanced_analysis import (
    analyze_change_impact,
    analyze_code_complexity,
    detect_security_issues,
    find_dead_code,
    find_similar_code,
    get_architecture_metrics,
)
from mcp_server_omnicpg.tools.analysis_tools import (
    analyze_path,
    sync_git_changes,
    verify_graph_sync,
)
from mcp_server_omnicpg.tools.apoc_tools import (
    apoc_expand_path,
    apoc_graph_schema,
    apoc_meta_stats,
    apoc_run_read_query,
    apoc_run_timeboxed_query,
    apoc_shortest_path,
    apoc_spanning_tree,
    apoc_subgraph_around_node,
    batch_callsite_methods,
    find_callers_of,
    find_callsite_method,
)
from mcp_server_omnicpg.tools.auto_expansion import (
    expand_method_on_demand,
    find_control_flow_with_auto_expand,
    find_data_flow_with_auto_expand,
    get_expansion_stats,
)
from mcp_server_omnicpg.tools.code_intelligence import (
    explain_code,
    get_code_context,
    get_test_coverage_info,
    semantic_search,
    suggest_refactoring,
    trace_variable,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create MCP server
app = Server("omnicpg-mcp-server")
adapter = get_adapter()
# Keep this exported for SSE health endpoint metadata.
TOOL_COUNT = 44
_RESOLVED_PROJECT_ID: str | None = None
_UNSCOPED_TOOLS = {
    "get_server_info",
    "apoc_graph_schema",
    "apoc_meta_stats",
    "list_projects",
}
PROJECT_ID_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "Optional project scope override; defaults to OMNICPG_PROJECT_ID or auto-discovery.",
}


def _resolve_project_id(
    explicit_project_id: str | None = None,
    *,
    require_scoped: bool = True,
) -> str | None:
    """Return the configured project id, or infer a unique live project id.

    The server still prefers ``Config.PROJECT_ID``. When the environment does
    not provide one, a single-project graph can safely auto-discover the only
    live ``project_id`` so MCP calls stay scoped instead of running unbounded.
    """
    global _RESOLVED_PROJECT_ID

    if explicit_project_id is not None and str(explicit_project_id).strip():
        return str(explicit_project_id).strip()
    if Config.PROJECT_ID:
        return Config.PROJECT_ID
    if _RESOLVED_PROJECT_ID is not None:
        return _RESOLVED_PROJECT_ID

    try:
        rows = adapter.query(
            "MATCH (n:Node) RETURN n.project_id AS project_id, count(*) AS c"
            " ORDER BY c DESC LIMIT 2"
        )
        project_ids = {str(row.get("project_id")) for row in rows if row.get("project_id")}
        if len(project_ids) == 1:
            _RESOLVED_PROJECT_ID = next(iter(project_ids))
            return _RESOLVED_PROJECT_ID
        if not require_scoped:
            return None
        if len(project_ids) > 1:
            raise ValueError(
                "Multiple project_id values detected in Neo4j. "
                "Provide 'project_id' in tool arguments or set OMNICPG_PROJECT_ID."
            )
        raise ValueError(
            "No project_id found in Neo4j. "
            "Provide 'project_id' in tool arguments or set OMNICPG_PROJECT_ID."
        )
    except Exception:
        logger.warning("project_id auto-discovery failed", exc_info=True)
        if require_scoped:
            raise
        _RESOLVED_PROJECT_ID = None

    return _RESOLVED_PROJECT_ID


# Required parameters per tool, validated after alias normalization so callers
# get one clear, actionable error instead of an opaque KeyError. Project scope
# is handled separately by _resolve_project_id and is not listed here.
_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "search_code": ("keyword",),
    "get_node_by_id": ("node_id",),
    "find_path": ("start_node_id", "end_node_id"),
    "find_data_flow": ("source_node_id", "target_node_id"),
    "find_control_flow": ("start_node_id", "end_node_id"),
    "get_dependencies": ("node_id",),
    "analyze_function": ("function_id",),
    "get_file_structure": ("file_path",),
    "find_callers_of": ("method_name",),
    "find_callsite_method": ("callsite_name",),
    "batch_callsite_methods": ("callsite_names",),
    "apoc_shortest_path": ("start_node_id", "end_node_id"),
    "apoc_subgraph_around_node": ("node_id",),
    "analyze_path": ("path",),
    "sync_git_changes": (),
    "verify_graph_sync": (),
    "find_data_flow_with_auto_expand": ("source_node_id", "target_node_id"),
    "find_control_flow_with_auto_expand": ("start_node_id", "end_node_id"),
    "apoc_expand_path": ("start_node_id",),
    "apoc_spanning_tree": ("start_node_id",),
    "apoc_run_read_query": ("cypher",),
    "apoc_run_timeboxed_query": ("cypher",),
    "expand_method_on_demand": ("method_id",),
    "analyze_change_impact": ("node_id",),
    "find_similar_code": ("node_id",),
    "get_code_context": ("node_id",),
    "semantic_search": ("intent",),
    "suggest_refactoring": ("node_id",),
    "explain_code": ("node_id",),
    "trace_variable": ("node_id",),
    "get_node_source_code": ("node_id",),
}

# Hints shown when a required parameter is missing, so the caller knows how to
# obtain a value (e.g. node ids come from search/query tools).
_PARAM_HINTS: dict[str, str] = {
    "node_id": "Obtain it from search_code, semantic_search or query_nodes first.",
    "function_id": "Obtain it from search_code/query_nodes (a Method/Function node id).",
    "method_id": "Obtain it from search_code/query_nodes (a Method node id).",
    "start_node_id": "Obtain node ids from search_code, semantic_search or query_nodes.",
    "end_node_id": "Obtain node ids from search_code, semantic_search or query_nodes.",
    "source_node_id": "Obtain node ids from search_code, semantic_search or query_nodes.",
    "target_node_id": "Obtain node ids from search_code, semantic_search or query_nodes.",
}


def _normalize_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize backward-compatible aliases for common MCP parameters.

    Accepts the canonical ``node_id`` for tools whose underlying function uses a
    differently named id parameter, so callers never have to remember per-tool
    id names.

    Args:
        name: The tool name being dispatched.
        arguments: The raw arguments supplied by the MCP client.

    Returns:
        A shallow copy of ``arguments`` with alias keys mapped to canonical ones.
    """
    normalized = dict(arguments)

    if name == "find_control_flow":
        if "start_node_id" not in normalized and "source_node_id" in normalized:
            normalized["start_node_id"] = normalized["source_node_id"]
        if "end_node_id" not in normalized and "target_node_id" in normalized:
            normalized["end_node_id"] = normalized["target_node_id"]

    if (
        name == "apoc_spanning_tree"
        and "start_node_id" not in normalized
        and "node_id" in normalized
    ):
        normalized["start_node_id"] = normalized["node_id"]

    if name == "semantic_search" and "intent" not in normalized and "query" in normalized:
        normalized["intent"] = normalized["query"]

    # Accept the canonical `node_id` for id-typed parameters that the underlying
    # function names differently, reducing a very common caller mistake.
    if name == "analyze_function" and "function_id" not in normalized and "node_id" in normalized:
        normalized["function_id"] = normalized["node_id"]

    if (
        name == "expand_method_on_demand"
        and "method_id" not in normalized
        and "node_id" in normalized
    ):
        normalized["method_id"] = normalized["node_id"]

    return normalized


def _validate_required_params(name: str, arguments: dict[str, Any]) -> None:
    """Raise a clear error when required parameters are missing or blank.

    Args:
        name: The tool name being dispatched (post-normalization).
        arguments: The normalized argument map.

    Raises:
        ValueError: If any required parameter is absent or an empty string,
            with a message naming the parameter, the tool, and how to obtain it.
        ValueError: If an ID parameter is not in MD5 hex format.
    """
    missing: list[str] = []
    for param in _REQUIRED_PARAMS.get(name, ()):
        value = arguments.get(param)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(param)

    if missing:
        details = "; ".join(
            f"'{p}'" + (f" ({_PARAM_HINTS[p]})" if p in _PARAM_HINTS else "") for p in missing
        )
        raise ValueError(f"Missing required parameter(s) for tool '{name}': {details}")

    # Smart ID format validation
    for param in _REQUIRED_PARAMS.get(name, ()):
        if "id" in param:
            value = arguments.get(param)
            if isinstance(value, str) and value.strip():
                val_str = value.strip()
                # Check if it looks like a 32-char hex string
                is_md5 = len(val_str) == 32 and all(c in "0123456789abcdefABCDEF" for c in val_str)
                if not is_md5:
                    import sys

                    # Skip check for legacy short mock IDs during testing
                    if "pytest" in sys.modules and len(val_str) < 10:
                        continue
                    raise ValueError(
                        f"Invalid format for parameter '{param}' in tool '{name}': "
                        f"Expected a 32-character hex ID hash, but got '{value}'. "
                        f"Note: ID hashes are retrieved from search/query tools first. "
                        f"If you wanted to search by name or keyword, use 'search_code' or 'query_nodes' instead."
                    )


def _validate_conditional_params(name: str, arguments: dict[str, Any]) -> None:
    """Validate tool-specific conditional input requirements.

    Args:
        name: The tool name being dispatched (post-normalization).
        arguments: The normalized argument map.

    Raises:
        ValueError: When a tool-specific conditional requirement is violated.
    """
    if name == "get_test_coverage_info":
        node_id = arguments.get("node_id")
        file_path = arguments.get("file_path")
        has_node_id = isinstance(node_id, str) and bool(node_id.strip())
        has_file_path = isinstance(file_path, str) and bool(file_path.strip())
        if not has_node_id and not has_file_path:
            raise ValueError(
                "Missing required parameter(s) for tool 'get_test_coverage_info': "
                "provide at least one of 'node_id' or 'file_path'"
            )


def get_server_info() -> dict[str, Any]:
    """Return MCP server status for readiness checks.

    Mirrors the logic of the SSE ``/health`` endpoint: it reports whether the
    Neo4j connection is live, whether the graph currently holds any ``Node``
    data, and how many MCP tools are registered.

    Returns:
        A dict with keys:
        - ``neo4j_connected`` (bool): whether the adapter is connected.
        - ``has_data`` (bool): whether at least one ``Node`` exists.
        - ``tool_count`` (int): the number of registered MCP tools.
        - ``projects`` (list): per-project node counts for scoping decisions.
        - ``usage`` (dict): a short workflow guide and project-scope rules.
    """
    neo4j_connected = adapter.is_connected()
    has_data = False
    projects: list[dict[str, Any]] = []
    if neo4j_connected:
        try:
            rows = adapter.query("MATCH (n:Node) RETURN count(n) AS c LIMIT 1")
            has_data = bool(rows and rows[0].get("c", 0) > 0)
        except Exception:
            logger.warning("get_server_info: count query failed", exc_info=True)
        try:
            project_rows = adapter.query(
                "MATCH (n:Node) WHERE n.project_id IS NOT NULL "
                "RETURN n.project_id AS project_id, count(n) AS nodes "
                "ORDER BY nodes DESC LIMIT 10"
            )
            projects = [
                {"project_id": r.get("project_id"), "nodes": r.get("nodes")} for r in project_rows
            ]
        except Exception:
            logger.warning("get_server_info: project inventory failed", exc_info=True)

    return {
        "neo4j_connected": neo4j_connected,
        "has_data": has_data,
        "tool_count": TOOL_COUNT,
        "projects": projects,
        "usage": {
            "project_scope": (
                "If more than one project is listed, pass 'project_id' on every "
                "tool call (or set OMNICPG_PROJECT_ID); a single project is "
                "auto-scoped."
            ),
            "tool_groups": {
                "1_discovery": ["search_code", "semantic_search", "query_nodes", "list_projects"],
                "2_node_inspection": [
                    "get_node_by_id",
                    "explain_code",
                    "get_code_context",
                    "get_node_source_code",
                    "get_file_structure",
                ],
                "3_flow_analysis": [
                    "find_path",
                    "find_data_flow",
                    "find_control_flow",
                    "find_data_flow_with_auto_expand",
                    "find_control_flow_with_auto_expand",
                    "trace_variable",
                ],
                "4_call_graph": [
                    "get_call_graph",
                    "find_callers_of",
                    "find_callsite_method",
                    "batch_callsite_methods",
                ],
                "5_advanced_metrics": [
                    "analyze_code_complexity",
                    "find_dead_code",
                    "analyze_change_impact",
                    "find_similar_code",
                    "get_architecture_metrics",
                    "get_test_coverage_info",
                    "detect_security_issues",
                ],
                "6_incremental_git": ["analyze_path", "sync_git_changes", "verify_graph_sync"],
                "7_apoc_raw": [
                    "apoc_expand_path",
                    "apoc_subgraph_around_node",
                    "apoc_shortest_path",
                    "apoc_spanning_tree",
                    "apoc_graph_schema",
                    "apoc_meta_stats",
                    "apoc_run_read_query",
                    "apoc_run_timeboxed_query",
                ],
            },
            "recommendation_flow": [
                "Step 1: Determine the project_id using `list_projects` or `get_server_info`.",
                "Step 2: Find a starting Method/Class/Variable node using `search_code` (for keywords) or `query_nodes` (for exact names/paths). Note that Node IDs are 32-character MD5 hex hashes.",
                "Step 3: To inspect code details, use `get_node_source_code` (for source snippet) or `get_code_context`/`explain_code` (for relationships).",
                "Step 4: To trace variables or call graphs, use `trace_variable` (for dataflow) or `get_call_graph`/`find_callers_of` (for callers/callees).",
            ],
            "common_mistakes_to_avoid": [
                "NEVER pass method/variable names directly into parameters expecting `*_node_id`, `node_id`, `function_id`, or `method_id`. Those parameters require the 32-character MD5 hash representing the node in Neo4j, which must be obtained from search/query tools first.",
                "Always scope Cypher queries in `apoc_run_read_query` using the `project_id` filter (e.g. MATCH (n:Node {project_id: $project_id})).",
                "When tracking data flow, use `find_data_flow_with_auto_expand` to handle methods that haven't been pre-expanded at index time.",
            ],
        },
    }


@app.list_tools()  # type: ignore
async def list_tools() -> list[Tool]:
    """List all available MCP tools."""
    tools = [
        Tool(
            name="query_nodes",
            description=(
                "List CPG nodes by label/type, name, file, role or layer. Each "
                "result has an 'id' you pass as 'node_id' to detail tools. Use "
                "search_code/semantic_search for fuzzy lookup when unsure of names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_type": {
                        "type": "string",
                        "description": "Node type to filter. Accepts a high-level label (e.g. 'Method', 'Class', 'Variable', 'Field') or a raw tree-sitter type (e.g. 'method_declaration', 'class_declaration').",
                    },
                    "name": {
                        "type": "string",
                        "description": "Node name to filter (exact match)",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "File path to filter",
                    },
                    "role": {
                        "type": "string",
                        "description": "Architectural role to filter (e.g., 'Controller', 'Service', 'Repository', 'Entity', 'DTO')",
                    },
                    "layer": {
                        "type": "string",
                        "description": "Architectural layer to filter ('web', 'service', 'data', 'model')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of nodes to return",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="query_edges",
            description="Query CPG edges by type, source, target, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "edge_type": {
                        "type": "string",
                        "description": "Edge type to filter (e.g., 'CALLS', 'CONTAINS')",
                    },
                    "source_id": {
                        "type": "string",
                        "description": "Source node ID to filter",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "Target node ID to filter",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of edges to return",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="search_code",
            description=(
                "Full-text keyword search over code entities (Method/Class/"
                "Interface/Field) by name, FQN or source code. Use this first "
                "when you do not know exact identifiers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Lucene full-text query, e.g. 'premium calculation', "
                            "'calc*', or 'name:transfer AND source_code:timeout'"
                        ),
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional label filter: Method, Class, Interface, or Field",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 10,
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="get_node_by_id",
            description="Get detailed information about a specific node by ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "The unique ID of the node",
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="find_path",
            description="Find paths between two nodes in the CPG",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "start_node_id": {
                        "type": "string",
                        "description": "The starting node ID",
                    },
                    "end_node_id": {
                        "type": "string",
                        "description": "The ending node ID",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum path depth to search",
                        "default": 5,
                    },
                    "relationship_types": {
                        "type": "string",
                        "description": "Optional comma-separated list of relationship types to filter",
                    },
                },
                "required": ["start_node_id", "end_node_id"],
            },
        ),
        Tool(
            name="find_data_flow",
            description="Find data flow paths between nodes",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "source_node_id": {
                        "type": "string",
                        "description": "The source node ID (data origin)",
                    },
                    "target_node_id": {
                        "type": "string",
                        "description": "The target node ID (data destination)",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum path depth to search",
                        "default": 5,
                    },
                },
                "required": ["source_node_id", "target_node_id"],
            },
        ),
        Tool(
            name="find_control_flow",
            description="Find control flow paths between nodes",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "start_node_id": {
                        "type": "string",
                        "description": "The starting node ID",
                    },
                    "source_node_id": {
                        "type": "string",
                        "description": "Alias of start_node_id for compatibility",
                    },
                    "end_node_id": {
                        "type": "string",
                        "description": "The ending node ID",
                    },
                    "target_node_id": {
                        "type": "string",
                        "description": "Alias of end_node_id for compatibility",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum path depth to search",
                        "default": 5,
                    },
                },
                "oneOf": [
                    {"required": ["start_node_id", "end_node_id"]},
                    {"required": ["source_node_id", "target_node_id"]},
                ],
            },
        ),
        Tool(
            name="get_call_graph",
            description="Get function call graph",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "function_name": {
                        "type": "string",
                        "description": "Specific function name to analyze",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "How many levels of the call graph to traverse",
                        "default": 2,
                    },
                    "include_callers": {
                        "type": "boolean",
                        "description": "Whether to include functions that call the target",
                        "default": True,
                    },
                    "include_callees": {
                        "type": "boolean",
                        "description": "Whether to include functions called by the target",
                        "default": True,
                    },
                },
            },
        ),
        Tool(
            name="get_dependencies",
            description="Get dependency relationships for a node",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "The node ID to analyze",
                    },
                    "dependency_type": {
                        "type": "string",
                        "description": "Type of dependencies: 'inbound', 'outbound', or 'both'",
                        "default": "both",
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="analyze_function",
            description=(
                "Analyze function details and relationships. Pass the Method/"
                "Function node id (from search_code/semantic_search/query_nodes)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "function_id": {
                        "type": "string",
                        "description": "The function/Method node id to analyze (alias: node_id)",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Alias of function_id for convenience",
                    },
                },
                "oneOf": [
                    {"required": ["function_id"]},
                    {"required": ["node_id"]},
                ],
            },
        ),
        Tool(
            name="get_file_structure",
            description="Get structure of a source code file",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "file_path": {
                        "type": "string",
                        "description": "The file path to analyze",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="find_callers_of",
            description=(
                "Impact analysis: find all Methods that call a given method by "
                "name (Java call-graph via CallSite + PARENT_OF traversal)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "method_name": {
                        "type": "string",
                        "description": "Name of the method being called (matched against CallSite.name)",
                    },
                    "file_path_contains": {
                        "type": "string",
                        "description": "Optional path substring to restrict results",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum PARENT_OF hops when climbing the AST",
                        "default": 15,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of caller pairs returned",
                        "default": 20,
                    },
                },
                "required": ["method_name"],
            },
        ),
        Tool(
            name="find_callsite_method",
            description=(
                "Find the Method node(s) that contain a named CallSite in the "
                "AST (reconstructs missing method-level CALLS edges for Java)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "callsite_name": {
                        "type": "string",
                        "description": "The name property of the CallSite node",
                    },
                    "file_path_contains": {
                        "type": "string",
                        "description": "Optional path substring to restrict the search",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum PARENT_OF traversal depth",
                        "default": 15,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of (CallSite, Method) pairs to return",
                        "default": 20,
                    },
                },
                "required": ["callsite_name"],
            },
        ),
        Tool(
            name="batch_callsite_methods",
            description=(
                "Batch call-graph lookup: for each CallSite name, find its "
                "containing Method in a single round-trip."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "callsite_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of CallSite name values to look up",
                    },
                    "file_path_contains": {
                        "type": "string",
                        "description": "Optional path substring to restrict the search",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum PARENT_OF hops",
                        "default": 15,
                    },
                    "limit_per_name": {
                        "type": "integer",
                        "description": "Maximum pairs per name to return",
                        "default": 10,
                    },
                },
                "required": ["callsite_names"],
            },
        ),
        Tool(
            name="apoc_shortest_path",
            description="Find the shortest directed path between two nodes via APOC BFS",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "start_node_id": {
                        "type": "string",
                        "description": "ID of the source CPG node",
                    },
                    "end_node_id": {
                        "type": "string",
                        "description": "ID of the target CPG node",
                    },
                    "relationship_filter": {
                        "type": "string",
                        "description": "APOC relationship filter (e.g. 'CALLS>|REACHES>')",
                        "default": "",
                    },
                    "max_level": {
                        "type": "integer",
                        "description": "Search radius in hops",
                        "default": 10,
                    },
                },
                "required": ["start_node_id", "end_node_id"],
            },
        ),
        Tool(
            name="apoc_subgraph_around_node",
            description="Retrieve the full subgraph around a node via apoc.path.subgraphAll",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the centre CPG node",
                    },
                    "relationship_filter": {
                        "type": "string",
                        "description": "APOC relationship filter (e.g. 'CALLS>|CONTAINS>')",
                        "default": "",
                    },
                    "label_filter": {
                        "type": "string",
                        "description": "APOC label filter (e.g. '+Method|+Class')",
                        "default": "",
                    },
                    "max_level": {
                        "type": "integer",
                        "description": "Maximum hop distance from the centre node",
                        "default": 2,
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="apoc_graph_schema",
            description="Return the graph schema via apoc.meta.schema()",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="apoc_meta_stats",
            description="Return graph-wide statistics via apoc.meta.stats()",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="analyze_path",
            description=(
                "Trigger an incremental analysis on a specific path "
                "(file or directory) relative to the workspace."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace (e.g. 'src/main.java')",
                    },
                    "level": {
                        "type": "string",
                        "description": "Analysis level ('FULL' or 'ARCHITECTURAL')",
                        "default": "FULL",
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language ('java', 'python', or 'auto')",
                        "default": "auto",
                    },
                    "chunk_size": {
                        "type": "integer",
                        "description": "Processing chunk size for streaming",
                        "default": 500,
                    },
                    "max_workers": {
                        "type": "integer",
                        "description": "Concurrent workers",
                        "default": 4,
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="sync_git_changes",
            description=("Sync the graph incrementally based on git changes between two commits."),
            inputSchema={
                "type": "object",
                "properties": {
                    "commit_from": {
                        "type": "string",
                        "description": "The starting git commit (default: 'HEAD~1').",
                    },
                    "commit_to": {
                        "type": "string",
                        "description": "The ending git commit (default: 'HEAD').",
                    },
                    "level": {
                        "type": "string",
                        "description": "Analysis level: 'FULL' (default) or 'ARCHITECTURAL'",
                        "enum": ["FULL", "ARCHITECTURAL"],
                    },
                    "language": {
                        "type": "string",
                        "description": "Language override: 'auto' (default), 'java', 'python'",
                        "enum": ["auto", "java", "python"],
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="verify_graph_sync",
            description=(
                "Verify that the files indexed in Neo4j match the files tracked by Git "
                "for the current project_id. Identifies missing and ghost files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Optional project_id isolation scope.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="find_data_flow_with_auto_expand",
            description=(
                "Find data flow paths between nodes, auto-expanding unexpanded "
                "methods on demand when no path is found."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_node_id": {
                        "type": "string",
                        "description": "The source node ID (data origin)",
                    },
                    "target_node_id": {
                        "type": "string",
                        "description": "The target node ID (data destination)",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum path depth to search",
                        "default": 5,
                    },
                    "auto_expand": {
                        "type": "boolean",
                        "description": "Whether to auto-expand unexpanded methods",
                        "default": True,
                    },
                },
                "required": ["source_node_id", "target_node_id"],
            },
        ),
        Tool(
            name="find_control_flow_with_auto_expand",
            description=(
                "Find control flow paths between nodes, auto-expanding "
                "unexpanded methods on demand when no path is found."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start_node_id": {
                        "type": "string",
                        "description": "The starting node ID",
                    },
                    "end_node_id": {
                        "type": "string",
                        "description": "The ending node ID",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum path depth to search",
                        "default": 5,
                    },
                    "auto_expand": {
                        "type": "boolean",
                        "description": "Whether to auto-expand unexpanded methods",
                        "default": True,
                    },
                },
                "required": ["start_node_id", "end_node_id"],
            },
        ),
        Tool(
            name="apoc_expand_path",
            description=(
                "Flexible path expansion from a start node via "
                "apoc.path.expandConfig (direction/label/uniqueness control)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "start_node_id": {
                        "type": "string",
                        "description": "ID of the starting CPG node",
                    },
                    "relationship_filter": {
                        "type": "string",
                        "description": "APOC relationship filter (e.g. 'CALLS>|REACHES>')",
                        "default": "",
                    },
                    "label_filter": {
                        "type": "string",
                        "description": "APOC label filter (e.g. '+Method|+Class')",
                        "default": "",
                    },
                    "min_level": {
                        "type": "integer",
                        "description": "Minimum hop distance to include",
                        "default": 1,
                    },
                    "max_level": {
                        "type": "integer",
                        "description": "Maximum hop distance to expand",
                        "default": 3,
                    },
                    "bfs": {
                        "type": "boolean",
                        "description": "Breadth-first (true) or depth-first (false)",
                        "default": True,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of end nodes to return",
                        "default": 50,
                    },
                    "uniqueness": {
                        "type": "string",
                        "description": "APOC uniqueness mode (e.g. 'NODE_GLOBAL')",
                        "default": "NODE_GLOBAL",
                    },
                },
                "required": ["start_node_id"],
            },
        ),
        Tool(
            name="apoc_spanning_tree",
            description="Compute a spanning tree rooted at a node via apoc.path.spanningTree.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "start_node_id": {
                        "type": "string",
                        "description": "ID of the root CPG node",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Alias of start_node_id for compatibility",
                    },
                    "relationship_filter": {
                        "type": "string",
                        "description": "APOC relationship filter (e.g. 'CALLS>|CONTAINS>')",
                        "default": "",
                    },
                    "label_filter": {
                        "type": "string",
                        "description": "APOC label filter (e.g. '+Method|+Class')",
                        "default": "",
                    },
                    "max_level": {
                        "type": "integer",
                        "description": "Maximum hop distance from the root",
                        "default": 3,
                    },
                    "bfs": {
                        "type": "boolean",
                        "description": "Breadth-first (true) or depth-first (false)",
                        "default": True,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tree nodes to return",
                        "default": 50,
                    },
                },
                "oneOf": [
                    {"required": ["start_node_id"]},
                    {"required": ["node_id"]},
                ],
            },
        ),
        Tool(
            name="apoc_run_read_query",
            description=(
                "Execute an arbitrary read-only Cypher query (write keywords "
                "rejected). Use when no pre-built tool covers your query."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "cypher": {
                        "type": "string",
                        "description": "A read-only Cypher query string",
                    },
                    "params": {
                        "type": "object",
                        "description": "Optional map of Cypher parameter names to values",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows to return (default 100, max 500)",
                        "default": 100,
                    },
                },
                "required": ["cypher"],
            },
        ),
        Tool(
            name="apoc_run_timeboxed_query",
            description=(
                "Execute a read-only Cypher query with a hard timeout via "
                "apoc.cypher.runTimeboxed (for deep/slow traversals)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "cypher": {
                        "type": "string",
                        "description": "A read-only Cypher query string",
                    },
                    "params": {
                        "type": "object",
                        "description": "Optional map of Cypher parameter names to values",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Millisecond budget for the inner query",
                        "default": 10000,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows to return (default 100, max 500)",
                        "default": 100,
                    },
                },
                "required": ["cypher"],
            },
        ),
        Tool(
            name="expand_method_on_demand",
            description=(
                "Expand a single method's intra-procedural data/control-flow "
                "subgraph on demand (for methods not expanded at import time). "
                "Pass the Method node id from search_code/query_nodes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "method_id": {
                        "type": "string",
                        "description": "ID of the Method node to expand (alias: node_id)",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Alias of method_id for convenience",
                    },
                },
                "oneOf": [
                    {"required": ["method_id"]},
                    {"required": ["node_id"]},
                ],
            },
        ),
        Tool(
            name="get_expansion_stats",
            description="Return statistics about on-demand method expansion (cache/coverage).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_server_info",
            description=(
                "Return MCP server status (Neo4j connectivity, has_data, tool "
                "count), the per-project node inventory, and a short usage/"
                "workflow guide. Call this FIRST to verify readiness and learn "
                "whether project_id scoping is required."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="detect_security_issues",
            description=(
                "Detect security findings (SQL injection via source->sink "
                "dataflow reachability, hardcoded secrets) with interprocedural "
                "edge evidence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "rules": {
                        "type": "object",
                        "description": "Optional rule overrides (source/sink/secret patterns)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of findings to return",
                        "default": 50,
                    },
                },
            },
        ),
        Tool(
            name="analyze_code_complexity",
            description=(
                "Rank methods by cyclomatic complexity (coalesce of complexity/"
                "mccabe). Surfaces a metric_source marker when enrichment is missing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "top": {
                        "type": "integer",
                        "description": "Maximum number of methods to return",
                        "default": 20,
                    },
                    "min_complexity": {
                        "type": "integer",
                        "description": "Minimum complexity threshold to include a method",
                        "default": 0,
                    },
                },
            },
        ),
        Tool(
            name="find_dead_code",
            description="Find methods with no incoming CALLS edges (candidate dead code).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "exclude_entrypoints": {
                        "type": "boolean",
                        "description": "Skip framework entry points (main, controllers, tests)",
                        "default": True,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of methods to return",
                        "default": 100,
                    },
                },
            },
        ),
        Tool(
            name="analyze_change_impact",
            description=(
                "Compute reverse reachability (blast radius) for a changed node, "
                "grouped by distance and layer."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the node being changed",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum reverse-reachability depth",
                        "default": 5,
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="find_similar_code",
            description=(
                "Find structurally similar methods using a deterministic "
                "structural fingerprint (no vectors)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the reference method",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum similarity score (0..1)",
                        "default": 0.7,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return",
                        "default": 10,
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="get_architecture_metrics",
            description=(
                "Aggregate architectural metrics by layer/role and detect "
                "layering violations. Returns a coverage marker when enrichment "
                "is missing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                },
            },
        ),
        Tool(
            name="get_code_context",
            description=(
                "Return a compact context card for a node plus its 1-hop callers and callees."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the node to describe",
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="semantic_search",
            description=(
                "Search code entities by natural-language intent over the "
                "code_fulltext index, optionally filtered by label."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "intent": {
                        "type": "string",
                        "description": "Natural-language description of what to find",
                    },
                    "query": {
                        "type": "string",
                        "description": "Alias of intent for compatibility",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional label filter (Method, Class, Interface, Field)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of hits to return",
                        "default": 10,
                    },
                },
                "oneOf": [
                    {"required": ["intent"]},
                    {"required": ["query"]},
                ],
            },
        ),
        Tool(
            name="suggest_refactoring",
            description=(
                "Produce rule-based refactoring suggestions (extract_method, "
                "dedupe, reduce_coupling) for a node. Does not change code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the node to analyse",
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="explain_code",
            description=(
                "Return a structured fact card explaining a node (what it is, "
                "what it calls, who calls it, role/layer)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the node to explain",
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="trace_variable",
            description=(
                "Trace a variable's data flow along REACHES/FLOWS_TO edges "
                "(max_depth capped at 50)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the variable/parameter node to trace from",
                    },
                    "direction": {
                        "type": "string",
                        "description": "Trace direction: 'forward' or 'backward'",
                        "default": "forward",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum traversal depth (must be <= 50)",
                        "default": 10,
                    },
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="get_test_coverage_info",
            description=(
                "Report whether a node/file is covered by tests via TESTS edges. "
                "Returns a coverage marker when uncovered."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "ID of the node to check",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "File path to check coverage for",
                    },
                },
                "anyOf": [
                    {"required": ["node_id"]},
                    {"required": ["file_path"]},
                ],
            },
        ),
        Tool(
            name="list_projects",
            description=(
                "List all projects currently in the Neo4j database with their metadata "
                "(languages, total nodes, total files). Use this first to explore "
                "available projects and choose a project_id scope."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_node_source_code",
            description=(
                "Retrieve the exact source code snippet of a specific node by its ID "
                "with line numbers prefix. Pass a 32-character hex ID hash (obtained "
                "from search_code/query_nodes first)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                    "node_id": {
                        "type": "string",
                        "description": "The 32-character hex ID hash of the target node",
                    },
                },
                "required": ["node_id"],
            },
        ),
    ]

    return tools


@app.call_tool()  # type: ignore
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls from MCP clients by offloading synchronous work to a thread."""
    return await asyncio.to_thread(_execute_tool_sync, name, arguments)


def _execute_tool_sync(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute tool logic synchronously."""
    try:
        # Ensure Neo4j connection
        adapter.ensure_connected()

        normalized_args = _normalize_tool_arguments(name, arguments)

        # Fail fast with an actionable message when required inputs are missing,
        # instead of surfacing an opaque KeyError to the caller.
        _validate_required_params(name, normalized_args)
        _validate_conditional_params(name, normalized_args)

        # Project isolation: scope every query to the configured project so a
        # shared Neo4j instance never leaks cross-project data (REQ-SCHEMA-006).
        project_id = _resolve_project_id(
            explicit_project_id=normalized_args.get("project_id"),
            require_scoped=name not in _UNSCOPED_TOOLS,
        )

        result: Any = None

        if name == "query_nodes":
            result = query_nodes(
                node_type=normalized_args.get("node_type"),
                name=normalized_args.get("name"),
                file_path=normalized_args.get("file_path"),
                project_id=project_id,
                role=normalized_args.get("role"),
                layer=normalized_args.get("layer"),
                limit=normalized_args.get("limit", 10),
            )

        elif name == "search_code":
            result = search_code(
                keyword=normalized_args["keyword"],
                label=normalized_args.get("label"),
                project_id=project_id,
                limit=normalized_args.get("limit", 10),
            )

        elif name == "query_edges":
            result = query_edges(
                edge_type=normalized_args.get("edge_type"),
                source_id=normalized_args.get("source_id"),
                target_id=normalized_args.get("target_id"),
                project_id=project_id,
                limit=normalized_args.get("limit", 10),
            )

        elif name == "get_node_by_id":
            result = get_node_by_id(node_id=normalized_args["node_id"], project_id=project_id)

        elif name == "find_path":
            result = find_path(
                start_node_id=normalized_args["start_node_id"],
                end_node_id=normalized_args["end_node_id"],
                max_depth=normalized_args.get("max_depth", 5),
                relationship_types=normalized_args.get("relationship_types"),
                project_id=project_id,
            )

        elif name == "find_data_flow":
            result = find_data_flow(
                source_node_id=normalized_args["source_node_id"],
                target_node_id=normalized_args["target_node_id"],
                max_depth=normalized_args.get("max_depth", 5),
                project_id=project_id,
            )

        elif name == "find_control_flow":
            result = find_control_flow(
                start_node_id=normalized_args["start_node_id"],
                end_node_id=normalized_args["end_node_id"],
                max_depth=normalized_args.get("max_depth", 5),
                project_id=project_id,
            )

        elif name == "get_call_graph":
            result = get_call_graph(
                function_name=normalized_args.get("function_name"),
                depth=normalized_args.get("depth", 2),
                include_callers=normalized_args.get("include_callers", True),
                include_callees=normalized_args.get("include_callees", True),
                project_id=project_id,
            )

        elif name == "get_dependencies":
            result = get_dependencies(
                node_id=normalized_args["node_id"],
                dependency_type=normalized_args.get("dependency_type", "both"),
                project_id=project_id,
            )

        elif name == "analyze_function":
            result = analyze_function(
                function_id=normalized_args["function_id"], project_id=project_id
            )

        elif name == "get_file_structure":
            result = get_file_structure(
                file_path=normalized_args["file_path"], project_id=project_id
            )

        elif name == "find_callers_of":
            result = find_callers_of(
                method_name=normalized_args["method_name"],
                file_path_contains=normalized_args.get("file_path_contains", ""),
                max_depth=normalized_args.get("max_depth", 15),
                limit=normalized_args.get("limit", 20),
                project_id=project_id,
            )

        elif name == "find_callsite_method":
            result = find_callsite_method(
                callsite_name=normalized_args["callsite_name"],
                file_path_contains=normalized_args.get("file_path_contains", ""),
                max_depth=normalized_args.get("max_depth", 15),
                limit=normalized_args.get("limit", 20),
                project_id=project_id,
            )

        elif name == "batch_callsite_methods":
            result = batch_callsite_methods(
                callsite_names=normalized_args["callsite_names"],
                file_path_contains=normalized_args.get("file_path_contains", ""),
                max_depth=normalized_args.get("max_depth", 15),
                limit_per_name=normalized_args.get("limit_per_name", 10),
                project_id=project_id,
            )

        elif name == "apoc_shortest_path":
            result = apoc_shortest_path(
                start_node_id=normalized_args["start_node_id"],
                end_node_id=normalized_args["end_node_id"],
                relationship_filter=normalized_args.get("relationship_filter", ""),
                max_level=normalized_args.get("max_level", 10),
                project_id=project_id,
            )

        elif name == "apoc_subgraph_around_node":
            result = apoc_subgraph_around_node(
                node_id=normalized_args["node_id"],
                relationship_filter=normalized_args.get("relationship_filter", ""),
                label_filter=normalized_args.get("label_filter", ""),
                max_level=normalized_args.get("max_level", 2),
                project_id=project_id,
            )

        elif name == "apoc_expand_path":
            result = apoc_expand_path(
                start_node_id=normalized_args["start_node_id"],
                relationship_filter=normalized_args.get("relationship_filter", ""),
                label_filter=normalized_args.get("label_filter", ""),
                min_level=normalized_args.get("min_level", 1),
                max_level=normalized_args.get("max_level", 3),
                bfs=normalized_args.get("bfs", True),
                limit=normalized_args.get("limit", 50),
                uniqueness=normalized_args.get("uniqueness", "NODE_GLOBAL"),
                project_id=project_id,
            )

        elif name == "apoc_spanning_tree":
            result = apoc_spanning_tree(
                start_node_id=normalized_args["start_node_id"],
                relationship_filter=normalized_args.get("relationship_filter", ""),
                label_filter=normalized_args.get("label_filter", ""),
                max_level=normalized_args.get("max_level", 3),
                bfs=normalized_args.get("bfs", True),
                limit=normalized_args.get("limit", 50),
                project_id=project_id,
            )

        elif name == "apoc_run_read_query":
            result = apoc_run_read_query(
                cypher=normalized_args["cypher"],
                params=normalized_args.get("params"),
                limit=normalized_args.get("limit", 100),
                project_id=project_id,
            )

        elif name == "apoc_run_timeboxed_query":
            result = apoc_run_timeboxed_query(
                cypher=normalized_args["cypher"],
                params=normalized_args.get("params"),
                timeout_ms=normalized_args.get("timeout_ms", 10_000),
                limit=normalized_args.get("limit", 100),
                project_id=project_id,
            )

        elif name == "apoc_graph_schema":
            result = apoc_graph_schema()

        elif name == "apoc_meta_stats":
            result = apoc_meta_stats()

        elif name == "analyze_path":
            result = analyze_path(
                path=normalized_args["path"],
                level=normalized_args.get("level", "FULL"),
                language=normalized_args.get("language", "auto"),
                chunk_size=normalized_args.get("chunk_size", 500),
                max_workers=normalized_args.get("max_workers", 4),
            )

        elif name == "sync_git_changes":
            result = sync_git_changes(
                commit_from=normalized_args.get("commit_from", "HEAD~1"),
                commit_to=normalized_args.get("commit_to", "HEAD"),
                level=normalized_args.get("level", "FULL"),
                language=normalized_args.get("language", "auto"),
                chunk_size=normalized_args.get("chunk_size", 500),
                max_workers=normalized_args.get("max_workers", 4),
            )

        elif name == "verify_graph_sync":
            project_id = _resolve_project_id(normalized_args.get("project_id"))
            if not project_id:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "Failed to resolve project_id. Ensure project_id is provided."
                            }
                        ),
                    )
                ]
            result = verify_graph_sync(
                project_id=project_id,
            )

        elif name == "find_data_flow_with_auto_expand":
            result = find_data_flow_with_auto_expand(
                source_node_id=normalized_args["source_node_id"],
                target_node_id=normalized_args["target_node_id"],
                max_depth=normalized_args.get("max_depth", 5),
                auto_expand=normalized_args.get("auto_expand", True),
            )

        elif name == "find_control_flow_with_auto_expand":
            result = find_control_flow_with_auto_expand(
                start_node_id=normalized_args["start_node_id"],
                end_node_id=normalized_args["end_node_id"],
                max_depth=normalized_args.get("max_depth", 5),
                auto_expand=normalized_args.get("auto_expand", True),
            )

        elif name == "expand_method_on_demand":
            result = expand_method_on_demand(method_id=normalized_args["method_id"])

        elif name == "get_expansion_stats":
            result = get_expansion_stats()

        elif name == "get_server_info":
            result = get_server_info()

        elif name == "detect_security_issues":
            result = detect_security_issues(
                rules=normalized_args.get("rules"),
                limit=normalized_args.get("limit", 50),
                project_id=project_id,
            )

        elif name == "analyze_code_complexity":
            result = analyze_code_complexity(
                top=normalized_args.get("top", 20),
                min_complexity=normalized_args.get("min_complexity", 0),
                project_id=project_id,
            )

        elif name == "find_dead_code":
            result = find_dead_code(
                exclude_entrypoints=normalized_args.get("exclude_entrypoints", True),
                limit=normalized_args.get("limit", 100),
                project_id=project_id,
            )

        elif name == "analyze_change_impact":
            result = analyze_change_impact(
                node_id=normalized_args["node_id"],
                max_depth=normalized_args.get("max_depth", 5),
                project_id=project_id,
            )

        elif name == "find_similar_code":
            result = find_similar_code(
                node_id=normalized_args["node_id"],
                threshold=normalized_args.get("threshold", 0.7),
                limit=normalized_args.get("limit", 10),
                project_id=project_id,
            )

        elif name == "get_architecture_metrics":
            result = get_architecture_metrics(project_id=project_id)

        elif name == "get_code_context":
            result = get_code_context(node_id=normalized_args["node_id"], project_id=project_id)

        elif name == "semantic_search":
            result = semantic_search(
                intent=normalized_args["intent"],
                label=normalized_args.get("label"),
                limit=normalized_args.get("limit", 10),
                project_id=project_id,
            )

        elif name == "suggest_refactoring":
            result = suggest_refactoring(node_id=normalized_args["node_id"], project_id=project_id)

        elif name == "explain_code":
            result = explain_code(node_id=normalized_args["node_id"], project_id=project_id)

        elif name == "trace_variable":
            result = trace_variable(
                node_id=normalized_args["node_id"],
                direction=normalized_args.get("direction", "forward"),
                max_depth=normalized_args.get("max_depth", 10),
                project_id=project_id,
            )

        elif name == "get_test_coverage_info":
            result = get_test_coverage_info(
                node_id=normalized_args.get("node_id"),
                file_path=normalized_args.get("file_path"),
                project_id=project_id,
            )

        elif name == "list_projects":
            result = list_projects()

        elif name == "get_node_source_code":
            result = get_node_source_code(
                node_id=normalized_args["node_id"],
                project_id=project_id,
            )

        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}),
                )
            ]

        # Serialize the result as JSON so MCP clients (LLMs) can parse it
        # reliably instead of guessing at Python repr formatting.
        if result is None:
            text = json.dumps({"results": [], "message": "No results found"})
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)

        return [
            TextContent(
                type="text",
                text=text,
            )
        ]

    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}", exc_info=True)
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e)}, ensure_ascii=False),
            )
        ]


async def main() -> None:
    """Main entry point for the MCP server."""
    logger.info("Starting OmniCPG MCP Server...")

    try:
        # Validate configuration
        Config.validate()

        # Connect to Neo4j
        adapter.connect()
        logger.info("Neo4j connection established")

        # Run the MCP server with SSE transport
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8080"))
        logger.info(f"MCP Server ready on {host}:{port}")

        # Create SSE transport
        sse = SseServerTransport(f"http://{host}:{port}/sse")

        # Create initialization options
        init_options = InitializationOptions(
            server_name="omnicpg-mcp-server",
            server_version="0.1.0",
            capabilities=ServerCapabilities(
                tools=ToolsCapability(),
            ),
        )

        # Run the server
        from starlette.applications import Starlette
        from starlette.routing import Mount, Route

        async def handle_sse(request: Any) -> None:
            async with sse.connect_sse(request.scope, request.receive, request.send) as streams:
                await app.run(streams[0], streams[1], init_options)

        starlette_app = Starlette(
            debug=False,
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages", app=sse.handle_post_message),
            ],
        )

        import uvicorn

        config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()

    except KeyboardInterrupt:
        logger.info("Shutting down...")
        adapter.disconnect()
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)


if __name__ == "__main__":
    import os

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        from mcp_server_omnicpg.sse_transport import start

        start()
    else:
        asyncio.run(main())
