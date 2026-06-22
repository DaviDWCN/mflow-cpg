"""MCP tools for querying OmniCPG Neo4j database.

This package contains all MCP tools that provide various query
and analysis capabilities for CPG data.

Available Tools:
    - Basic Queries:
        * query_nodes: Query CPG nodes
        * query_edges: Query CPG edges
        * get_node_by_id: Get node details by ID

    - Path Queries:
        * find_path: Find paths between nodes
        * find_data_flow: Find data flow paths
        * find_control_flow: Find control flow paths

    - Graph Analysis:
        * get_call_graph: Get function call graph
        * get_dependencies: Get dependency relationships
        * analyze_function: Analyze function details
        * get_file_structure: Get file structure
"""

from __future__ import annotations

from mcp_server_omnicpg.tools.agentic_tools import (
    agentic_workflow,
)
from mcp_server_omnicpg.tools.auto_expansion import (
    expand_method_on_demand,
    find_control_flow_with_auto_expand,
    find_data_flow_with_auto_expand,
    get_expansion_stats,
)

# Import all tools for easy access
from mcp_server_omnicpg.tools.basic_queries import (
    get_node_by_id,
    query_edges,
    query_nodes,
    search_code,
)
from mcp_server_omnicpg.tools.graph_analysis import (
    analyze_function,
    get_call_graph,
    get_dependencies,
    get_file_structure,
)
from mcp_server_omnicpg.tools.helper_tools import (
    get_node_source_code,
    list_projects,
)
from mcp_server_omnicpg.tools.path_queries import (
    find_control_flow,
    find_data_flow,
    find_path,
)

__all__ = [
    "agentic_workflow",
    "analyze_function",
    "expand_method_on_demand",
    "find_control_flow",
    "find_control_flow_with_auto_expand",
    "find_data_flow",
    # Auto Expansion Tools
    "find_data_flow_with_auto_expand",
    # Path Queries
    "find_path",
    # Graph Analysis
    "get_call_graph",
    "get_dependencies",
    "get_expansion_stats",
    "get_file_structure",
    "get_node_by_id",
    "get_node_source_code",
    # Helper Tools
    "list_projects",
    "query_edges",
    # Basic Queries
    "query_nodes",
    "search_code",
]
