"""Path query tools for CPG graph navigation.

Provides tools for finding paths and flows in the CPG.
All traversals are implemented via ``apoc.path.expandConfig`` which gives
BFS ordering, cycle-safe uniqueness constraints, and flexible relationship /
label filtering - a significant improvement over variable-length Cypher
pattern matching:

- find_path:          Find paths between two nodes (any relationship type)
- find_data_flow:     Find data flow paths (REACHES edges)
- find_control_flow:  Find control flow paths (FLOWS_TO edges)
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server_omnicpg.neo4j_adapter import get_adapter

logger = logging.getLogger(__name__)


def find_path(
    start_node_id: str,
    end_node_id: str,
    max_depth: int = 5,
    relationship_types: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find paths between two nodes in the CPG via ``apoc.path.expandConfig``.

    BFS traversal terminates at *end_node_id*, guaranteeing that the first
    results are always the shortest paths.  The optional *relationship_types*
    filter is forwarded directly as an APOC relationship-filter string.

    Args:
        start_node_id: The starting node ID.
        end_node_id: The ending node ID.
        max_depth: Maximum path depth to search. Default is 5.
        relationship_types: Optional comma-separated relationship types, e.g.
            ``"CALLS,CONTAINS"``.  APOC direction suffixes (``>``, ``<``) are
            also accepted per-type.  If ``None``, all relationship types are
            allowed.
        project_id: Project ID to scope the endpoints to. If None, endpoints
            are matched across all projects.

    Returns:
        A list of path dicts each containing:
        - ``nodes``: list of node IDs along the path.
        - ``edges``: list of ``{type, source, target}`` dicts.
        - ``length``: number of edges in the path.

    Raises:
        ValueError: If max_depth is less than 1.
        ConnectionError: If Neo4j connection fails.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    # Convert comma-separated types to APOC pipe-separated filter
    rel_filter = ""
    if relationship_types:
        rel_filter = "|".join(rt.strip() for rt in relationship_types.split(","))

    proj_where = (
        "\n        WHERE start.project_id = $project_id AND end.project_id = $project_id"
        if project_id
        else ""
    )
    query = (
        """
        MATCH (start:Node {id: $start_node_id}), (end:Node {id: $end_node_id})"""
        + proj_where
        + """
        CALL apoc.path.expandConfig(start, {
            endNodes:           [end],
            relationshipFilter: $rel_filter,
            maxLevel:           $max_depth,
            bfs:                true,
            uniqueness:         'NODE_GLOBAL',
            limit:              10
        }) YIELD path
        RETURN [n IN nodes(path) | n.id]            AS node_ids,
               [r IN relationships(path) | type(r)] AS edge_types,
               length(path)                         AS path_length
        ORDER BY path_length
        LIMIT 10
    """
    )

    path_params: dict[str, Any] = {
        "start_node_id": start_node_id,
        "end_node_id": end_node_id,
        "rel_filter": rel_filter,
        "max_depth": max_depth,
    }
    if project_id:
        path_params["project_id"] = project_id
    results = adapter.query(query, **path_params)

    formatted_results: list[dict[str, Any]] = []
    for result in results:
        node_ids: list[Any] = result.get("node_ids") or []
        edge_types: list[Any] = result.get("edge_types") or []
        edges = [
            {"type": edge_types[i], "source": node_ids[i], "target": node_ids[i + 1]}
            for i in range(len(edge_types))
        ]
        formatted_results.append(
            {
                "nodes": node_ids,
                "edges": edges,
                "length": result.get("path_length", 0),
            }
        )

    logger.info(
        "find_path (APOC): %d paths from %s to %s (max_depth=%d)",
        len(formatted_results),
        start_node_id,
        end_node_id,
        max_depth,
    )
    return formatted_results


def find_data_flow(
    source_node_id: str,
    target_node_id: str,
    max_depth: int = 5,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find data flow paths between two nodes via ``apoc.path.expandConfig``.

    Data flow follows directed ``REACHES`` edges.  Using APOC gives BFS
    ordering and ``NODE_GLOBAL`` uniqueness so cycles are handled safely
    without blowing up on large DFGs.

    Args:
        source_node_id: The source node ID (data origin).
        target_node_id: The target node ID (data destination).
        max_depth: Maximum path depth to search. Default is 5.
        project_id: Project ID to scope the endpoints to. If None, endpoints
            are matched across all projects.

    Returns:
        A list of data flow paths each containing:
        - ``nodes``: list of node IDs along the flow.
        - ``operations``: list of relationship type strings.
        - ``length``: number of operations in the flow.
        - ``description``: human-readable summary of the flow.

    Raises:
        ValueError: If max_depth is less than 1.
        ConnectionError: If Neo4j connection fails.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    proj_where = (
        "\n        WHERE source.project_id = $project_id AND target.project_id = $project_id"
        if project_id
        else ""
    )
    flow_params: dict[str, Any] = {
        "source_node_id": source_node_id,
        "target_node_id": target_node_id,
        "max_depth": max_depth,
    }
    if project_id:
        flow_params["project_id"] = project_id

    query = (
        """
        MATCH (source:Node {id: $source_node_id}),
              (target:Node {id: $target_node_id})"""
        + proj_where
        + """
        CALL apoc.path.expandConfig(source, {
            endNodes:           [target],
            relationshipFilter: 'REACHES>',
            maxLevel:           $max_depth,
            bfs:                true,
            uniqueness:         'NODE_GLOBAL',
            limit:              10
        }) YIELD path
        RETURN [n IN nodes(path) | n.id]                                  AS node_ids,
               [r IN relationships(path) | type(r)]                       AS operations,
               [r IN relationships(path) | coalesce(r.interprocedural, 'none')]
                                                                           AS flow_types,
               [n IN nodes(path) | coalesce(n.code, n.id)]               AS node_identifiers,
               length(path)                                               AS flow_length
        ORDER BY flow_length
        LIMIT 10
    """
    )

    results = adapter.query(query, **flow_params)
    if not results:
        fallback_query = (
            """
            MATCH (source:Node {id: $source_node_id}),
                  (target:Node {id: $target_node_id})"""
            + proj_where
            + """
            CALL apoc.path.expandConfig(source, {
                endNodes:           [target],
                relationshipFilter: 'REACHES',
                maxLevel:           $max_depth,
                bfs:                true,
                uniqueness:         'NODE_GLOBAL',
                limit:              10
            }) YIELD path
            RETURN [n IN nodes(path) | n.id]                                  AS node_ids,
                   [r IN relationships(path) | type(r)]                       AS operations,
                   [r IN relationships(path) | coalesce(r.interprocedural, 'none')]
                                                                               AS flow_types,
                   [n IN nodes(path) | coalesce(n.code, n.id)]                AS node_identifiers,
                   length(path)                                                AS flow_length
            ORDER BY flow_length
            LIMIT 10
        """
        )
        results = adapter.query(fallback_query, **flow_params)

    formatted_results: list[dict[str, Any]] = []
    for result in results:
        node_ids: list[Any] = result.get("node_ids") or []
        operations: list[Any] = result.get("operations") or []
        node_identifiers: list[Any] = result.get("node_identifiers") or []
        flow_types: list[Any] = result.get("flow_types") or []

        if node_identifiers:
            parts: list[str] = []
            for i, code in enumerate(node_identifiers):
                if i > 0 and i - 1 < len(operations):
                    ftype = flow_types[i - 1]
                    label = f"[{ftype}]" if ftype != "none" else "-->"
                    parts.append(label)
                snippet = str(code)
                parts.append(f"`{snippet[:50]}...`" if len(snippet) > 50 else f"`{snippet}`")
            description = " ".join(parts)
        else:
            description = f"Data flow from {source_node_id} to {target_node_id}"

        formatted_results.append(
            {
                "nodes": node_ids,
                "operations": operations,
                "length": result.get("flow_length", 0),
                "description": description,
            }
        )

    logger.info(
        "find_data_flow (APOC): %d paths from %s to %s (max_depth=%d)",
        len(formatted_results),
        source_node_id,
        target_node_id,
        max_depth,
    )
    return formatted_results


def find_control_flow(
    start_node_id: str,
    end_node_id: str,
    max_depth: int = 5,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find control flow paths between two nodes via ``apoc.path.expandConfig``.

    Control flow follows directed ``FLOWS_TO`` edges.  APOC BFS with
    ``NODE_GLOBAL`` uniqueness safely handles loops in the CFG.

    Args:
        start_node_id: The starting node ID (execution origin).
        end_node_id: The ending node ID (execution destination).
        max_depth: Maximum path depth to search. Default is 5.
        project_id: Project ID to scope the endpoints to. If None, endpoints
            are matched across all projects.

    Returns:
        A list of control flow paths each containing:
        - ``nodes``: list of node IDs along the flow.
        - ``control_edges``: list of edge type strings.
        - ``length``: number of control edges in the flow.
        - ``description``: human-readable summary.

    Raises:
        ValueError: If max_depth is less than 1.
        ConnectionError: If Neo4j connection fails.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    proj_where = (
        "\n        WHERE start.project_id = $project_id AND end.project_id = $project_id"
        if project_id
        else ""
    )
    query = (
        """
        MATCH (start:Node {id: $start_node_id}),
              (end:Node   {id: $end_node_id})"""
        + proj_where
        + """
        CALL apoc.path.expandConfig(start, {
            endNodes:           [end],
            relationshipFilter: 'FLOWS_TO>',
            maxLevel:           $max_depth,
            bfs:                true,
            uniqueness:         'NODE_GLOBAL',
            limit:              10
        }) YIELD path
        RETURN [n IN nodes(path) | n.id]                           AS node_ids,
               [r IN relationships(path) | type(r)]               AS control_edges,
               [n IN nodes(path) | coalesce(n.code, n.id)]        AS node_identifiers,
               length(path)                                        AS flow_length
        ORDER BY flow_length
        LIMIT 10
    """
    )

    cf_params: dict[str, Any] = {
        "start_node_id": start_node_id,
        "end_node_id": end_node_id,
        "max_depth": max_depth,
    }
    if project_id:
        cf_params["project_id"] = project_id
    results = adapter.query(query, **cf_params)

    formatted_results: list[dict[str, Any]] = []
    for result in results:
        node_ids: list[Any] = result.get("node_ids") or []
        control_edges: list[Any] = result.get("control_edges") or []
        node_identifiers: list[Any] = result.get("node_identifiers") or []

        description = (
            " → ".join(str(n) for n in node_identifiers)
            if node_identifiers
            else (f"Control flow from {start_node_id} to {end_node_id}")
        )

        formatted_results.append(
            {
                "nodes": node_ids,
                "control_edges": control_edges,
                "length": result.get("flow_length", 0),
                "description": description,
            }
        )

    logger.info(
        "find_control_flow (APOC): %d paths from %s to %s (max_depth=%d)",
        len(formatted_results),
        start_node_id,
        end_node_id,
        max_depth,
    )
    return formatted_results
