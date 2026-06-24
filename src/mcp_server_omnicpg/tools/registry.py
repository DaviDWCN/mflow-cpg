"""MCP tool registry for OmniCPG.

Centralizes the schemas and handler function mappings for all 45 MCP tools.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable
from mcp.types import Tool

# Import all tool implementations from their respective modules
from mcp_server_omnicpg.tools import (
    analyze_function,
    find_control_flow,
    find_data_flow,
    find_path,
    get_call_graph,
    get_dependencies,
    agentic_workflow,
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

logger = logging.getLogger(__name__)

# Common project isolation property schema (REQ-SCHEMA-006)
PROJECT_ID_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "Optional project scope override; defaults to OMNICPG_PROJECT_ID or auto-discovery.",
}


class MCPToolRegistry:
    """Registry to map MCP tool definitions to their Python handlers."""

    def __init__(self):
        self.tools: dict[str, Tool] = {}
        self.handlers: dict[str, Callable] = {}

    def register(self, tool_def: Tool, handler: Callable | None = None) -> None:
        """Register a tool and its execution handler."""
        self.tools[tool_def.name] = tool_def
        if handler is not None:
            self.handlers[tool_def.name] = handler

    def register_handler(self, name: str, handler: Callable) -> None:
        """Register/override a handler for an already-defined tool."""
        if name not in self.tools:
            raise KeyError(f"Cannot register handler for undefined tool '{name}'")
        self.handlers[name] = handler

    def get_tools(self) -> list[Tool]:
        """Return the list of all registered tools."""
        return list(self.tools.values())

    def execute(self, name: str, normalized_args: dict[str, Any], project_id: str | None) -> Any:
        """Dynamically execute a registered tool handler."""
        handler = self.handlers.get(name)
        if not handler:
            raise ValueError(f"No execution handler registered for tool '{name}'")

        # Map normalized arguments to handler parameters using signature inspection
        sig = inspect.signature(handler)
        has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

        kwargs = {}
        if has_var_keyword:
            kwargs.update(normalized_args)
            kwargs["project_id"] = project_id

        for param_name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if param_name == "project_id":
                kwargs["project_id"] = project_id
            elif param_name in normalized_args:
                kwargs[param_name] = normalized_args[param_name]
            elif param.default is not inspect.Parameter.empty:
                # Fall back to function defaults if parameter is not in normalized_args
                pass

        return handler(**kwargs)


# Instantiate the global registry
registry = MCPToolRegistry()

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions registration
# ─────────────────────────────────────────────────────────────────────────────

registry.register(
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
    query_nodes,
)

registry.register(
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
    query_edges,
)

registry.register(
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
    search_code,
)

registry.register(
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
    get_node_by_id,
)

registry.register(
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
    find_path,
)

registry.register(
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
    find_data_flow,
)

registry.register(
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
    find_control_flow,
)

registry.register(
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
    get_call_graph,
)

registry.register(
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
    get_dependencies,
)

registry.register(
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
    analyze_function,
)

registry.register(
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
    get_file_structure,
)

registry.register(
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
    find_callers_of,
)

registry.register(
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
    find_callsite_method,
)

registry.register(
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
    batch_callsite_methods,
)

registry.register(
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
    apoc_shortest_path,
)

registry.register(
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
    apoc_subgraph_around_node,
)

registry.register(
    Tool(
        name="apoc_graph_schema",
        description="Return the graph schema via apoc.meta.schema()",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    apoc_graph_schema,
)

registry.register(
    Tool(
        name="apoc_meta_stats",
        description="Return graph-wide statistics via apoc.meta.stats()",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    apoc_meta_stats,
)

registry.register(
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
    analyze_path,
)

registry.register(
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
    sync_git_changes,
)

registry.register(
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
    verify_graph_sync,
)

registry.register(
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
    find_data_flow_with_auto_expand,
)

registry.register(
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
    find_control_flow_with_auto_expand,
)

registry.register(
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
    apoc_expand_path,
)

registry.register(
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
    apoc_spanning_tree,
)

registry.register(
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
    apoc_run_read_query,
)

registry.register(
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
    apoc_run_timeboxed_query,
)

registry.register(
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
    expand_method_on_demand,
)

registry.register(
    Tool(
        name="get_expansion_stats",
        description="Return statistics about on-demand method expansion (cache/coverage).",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    get_expansion_stats,
)

registry.register(
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
    None,  # Will be registered dynamically in mcp_server.py to avoid circular import
)

registry.register(
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
    detect_security_issues,
)

registry.register(
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
    analyze_code_complexity,
)

registry.register(
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
    find_dead_code,
)

registry.register(
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
    analyze_change_impact,
)

registry.register(
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
    find_similar_code,
)

registry.register(
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
    get_architecture_metrics,
)

registry.register(
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
    get_code_context,
)

registry.register(
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
    semantic_search,
)

registry.register(
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
    suggest_refactoring,
)

registry.register(
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
    explain_code,
)

registry.register(
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
    trace_variable,
)

registry.register(
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
    get_test_coverage_info,
)

registry.register(
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
    list_projects,
)

registry.register(
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
    get_node_source_code,
)

registry.register(
    Tool(
        name="agentic_workflow",
        description=(
            "Execute a compound workflow to provide a rich diagnostic payload. "
            "Combines natural language semantic search with context retrieval and call graph fetching."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query describing the issue or functionality.",
                },
                "project_id": PROJECT_ID_SCHEMA_PROPERTY,
                "limit": {
                    "type": "integer",
                    "description": "Max number of top matches to analyze (default: 3).",
                },
            },
            "required": ["query"],
        },
    ),
    agentic_workflow,
)
