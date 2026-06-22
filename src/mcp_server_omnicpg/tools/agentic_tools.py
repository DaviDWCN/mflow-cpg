"""Agentic tools for OmniCPG MCP Server.

This module provides compound tools that combine basic searches and graph
traversals to facilitate Agentic workflows (like ReAct or Reflection).
"""

from __future__ import annotations

from typing import Any

from mcp_server_omnicpg.tools.code_intelligence import (
    get_code_context,
    semantic_search,
)
from mcp_server_omnicpg.tools.graph_analysis import get_call_graph


def agentic_workflow(
    query: str,
    project_id: str | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    """Execute a compound workflow to provide a rich diagnostic payload.

    This tool takes a natural-language intent/query, uses `semantic_search`
    to find the top nodes matching the query, and then for each node
    retrieves its context and call graph.

    Args:
        query: Natural-language query describing the issue or functionality.
        project_id: Project ID to scope the query to.
        limit: Max number of top matches to analyze (default: 3).

    Returns:
        A rich diagnostic payload containing matched nodes along with their
        detailed code context and call graph information.
    """
    search_res = semantic_search(intent=query, limit=limit, project_id=project_id)

    if "error" in search_res:
        return search_res

    matches = search_res.get("results", [])
    if not matches:
        return search_res

    enriched_matches = []
    for match in matches:
        node_id = match.get("id")
        if not node_id:
            continue

        ctx = get_code_context(node_id=node_id, project_id=project_id)

        name = match.get("name")
        if name:
            cg = get_call_graph(
                function_name=name,
                depth=1,
                include_callers=True,
                include_callees=True,
                project_id=project_id
            )
        else:
            cg = None

        enriched_matches.append(
            {
                "match": match,
                "context": ctx,
                "call_graph": cg,
            }
        )

    return {
        "query": query,
        "results": enriched_matches,
        "count": len(enriched_matches),
    }
