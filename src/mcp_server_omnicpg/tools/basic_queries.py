"""Basic query tools for CPG nodes and edges.

Provides fundamental tools for querying Neo4j CPG data:
- query_nodes: Query nodes by type, name, file path, etc.
- query_edges: Query edges by type, source, target, etc.
- get_node_by_id: Get detailed information about a specific node.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server_omnicpg.neo4j_adapter import get_adapter

logger = logging.getLogger(__name__)


def query_nodes(
    node_type: str | None = None,
    name: str | None = None,
    file_path: str | None = None,
    project_id: str | None = None,
    language: str | None = None,
    role: str | None = None,
    layer: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Query CPG nodes by various criteria.

    Args:
        node_type: Node type to filter (e.g., "Method", "Class", "Variable").
            If None, returns all node types.
        name: Node name to filter (exact match). If None, any name is allowed.
        file_path: File path to filter. If None, any file path is allowed.
        project_id: Project ID to filter. If None, returns all projects.
        language: Programming language to filter (e.g., "python", "java").
            If None, returns all languages.
        role: Architectural role to filter (e.g., "Controller", "Service",
            "Repository", "Entity", "DTO"). If None, any role is allowed.
        layer: Architectural layer to filter ("web", "service", "data",
            "model"). If None, any layer is allowed.
        limit: Maximum number of nodes to return. Default is 10.

    Returns:
        A list of node dictionaries, each containing:
        - id: Node ID
        - type: Node type (if available)
        - name: Node name (if available)
        - file_path: File path (if available)
        - project_id: Project ID (if available)
        - language: Programming language (if available)
        - properties: All node properties

    Raises:
        ValueError: If limit is less than 1.
        ConnectionError: If Neo4j connection fails.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    # Build Cypher query with optional filters
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit}

    if node_type:
        # High-level node kinds are stored as Neo4j labels (e.g. "Method",
        # "Class", "Variable"), while the `type` property holds the raw
        # tree-sitter grammar name (e.g. "method_declaration"). Match either so
        # both label names and AST type names work.
        conditions.append("($node_type IN labels(n) OR n.type = $node_type)")
        params["node_type"] = node_type

    if name:
        # Check if name exists in properties (some nodes may not have it)
        conditions.append("(n.name IS NOT NULL AND n.name = $name)")
        params["name"] = name

    if file_path:
        conditions.append("n.file_path = $file_path")
        params["file_path"] = file_path

    if project_id:
        # Filter by project ID
        conditions.append("n.project_id = $project_id")
        params["project_id"] = project_id

    if language:
        # Filter by programming language
        conditions.append("n.language = $language")
        params["language"] = language

    if role:
        # Filter by architectural role (set by graph enrichment)
        conditions.append("n.role = $role")
        params["role"] = role

    if layer:
        # Filter by architectural layer (set by graph enrichment)
        conditions.append("n.layer = $layer")
        params["layer"] = layer

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        MATCH (n:Node)
        {where_clause}
        RETURN n.id AS id,
               n.type AS type,
               n.name AS name,
               n.code AS code,
               n.file_path AS file_path,
               n.project_id AS project_id,
               n.language AS language,
               properties(n) AS properties
        LIMIT $limit
    """

    results = adapter.query(query, **params)

    # Format results
    formatted_results: list[dict[str, Any]] = []
    for result in results:
        formatted_results.append(
            {
                "id": result.get("id"),
                "type": result.get("type"),
                "name": result.get("name"),
                "code": result.get("code"),
                "file_path": result.get("file_path"),
                "project_id": result.get("project_id"),
                "language": result.get("language"),
                "properties": result.get("properties", {}),
            }
        )

    logger.info(
        "Queried %d nodes (node_type=%s, name=%s, file_path=%s, project_id=%s, language=%s)",
        len(formatted_results),
        node_type,
        name,
        file_path,
        project_id,
        language,
    )

    return formatted_results


def search_code(
    keyword: str,
    label: str | None = None,
    project_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Full-text search over code entities by keyword.

    Uses the ``code_fulltext`` Neo4j full-text index, which covers the
    ``name``, ``fqn`` and ``source_code`` of ``Method`` / ``Class`` /
    ``Interface`` / ``Field`` nodes. This is the keyword-recall entry
    point for AI agents that do not know exact identifiers.

    Args:
        keyword: Lucene full-text query (e.g. ``"premium calculation"``,
            ``"calc*"``, ``"name:transfer AND source_code:timeout"``).
        label: Optional label to restrict results to
            (``Method``/``Class``/``Interface``/``Field``).
        project_id: Project ID to filter. If None, returns all projects.
        limit: Maximum number of results. Default is 10.

    Returns:
        A list of dicts with ``id``, ``label``, ``name``, ``fqn``,
        ``file_path``, ``line_start``, ``score`` and a truncated
        ``code_preview``.

    Raises:
        ValueError: If keyword is empty or limit is less than 1.
        ConnectionError: If Neo4j connection fails.
    """
    if not keyword or not keyword.strip():
        raise ValueError("keyword must not be empty")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    conditions: list[str] = []
    params: dict[str, Any] = {"keyword": keyword, "limit": limit}
    if project_id:
        conditions.append("node.project_id = $project_id")
        params["project_id"] = project_id
    if label:
        conditions.append("$label IN labels(node)")
        params["label"] = label
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        CALL db.index.fulltext.queryNodes('code_fulltext', $keyword)
        YIELD node, score
        {where_clause}
        RETURN node.id AS id,
               [l IN labels(node) WHERE l <> 'Node'][0] AS label,
               node.name AS name,
               node.fqn AS fqn,
               node.file_path AS file_path,
               node.line_start AS line_start,
               node.source_code AS source_code,
               score AS score
        ORDER BY score DESC
        LIMIT $limit
    """

    results = adapter.query(query, **params)

    formatted_results: list[dict[str, Any]] = []
    for result in results:
        source_code = result.get("source_code")
        code_preview = source_code[:500] if isinstance(source_code, str) else None
        formatted_results.append(
            {
                "id": result.get("id"),
                "label": result.get("label"),
                "name": result.get("name"),
                "fqn": result.get("fqn"),
                "file_path": result.get("file_path"),
                "line_start": result.get("line_start"),
                "score": result.get("score"),
                "code_preview": code_preview,
            }
        )

    logger.info(
        "search_code matched %d nodes (keyword=%s, label=%s, project_id=%s)",
        len(formatted_results),
        keyword,
        label,
        project_id,
    )

    return formatted_results


def query_edges(
    edge_type: str | None = None,
    source_id: str | None = None,
    target_id: str | None = None,
    project_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Query CPG edges by various criteria.

    Args:
        edge_type: Edge type to filter (e.g., "CALLS", "DEFINES", "USES").
            If None, returns all edge types.
        source_id: Source node ID to filter. If None, any source is allowed.
        target_id: Target node ID to filter. If None, any target is allowed.
        project_id: Project ID to filter. If None, returns all projects.
        limit: Maximum number of edges to return. Default is 10.

    Returns:
        A list of edge dictionaries, each containing:
        - id: Edge ID (optional, depends on Neo4j configuration)
        - type: Edge type
        - source: Source node ID
        - target: Target node ID
        - project_id: Project ID (if available)
        - properties: All edge properties

    Raises:
        ValueError: If limit is less than 1.
        ConnectionError: If Neo4j connection fails.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    # Build Cypher query with optional filters
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit}

    # Build MATCH clause with optional edge type filter
    if edge_type:
        match_clause = f"MATCH (source:Node) -[r:{edge_type}]-> (target:Node)"
    else:
        match_clause = "MATCH (source:Node) -[r]-> (target:Node)"

    # Build WHERE clause with source and target filters
    if source_id:
        conditions.append("source.id = $source_id")
        params["source_id"] = source_id

    if target_id:
        conditions.append("target.id = $target_id")
        params["target_id"] = target_id

    if project_id:
        # Filter edges by project ID
        conditions.append("r.project_id = $project_id")
        params["project_id"] = project_id

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Extract edge type from the relationship
    query = f"""
        {match_clause}
        {where_clause}
        RETURN type(r) AS type,
               source.id AS source,
               target.id AS target,
               r.project_id AS project_id,
               properties(r) AS properties
        LIMIT $limit
    """

    results = adapter.query(query, **params)

    # Format results
    formatted_results: list[dict[str, Any]] = []
    for result in results:
        formatted_results.append(
            {
                "type": result.get("type"),
                "source": result.get("source"),
                "target": result.get("target"),
                "project_id": result.get("project_id"),
                "properties": result.get("properties", {}),
            }
        )

    logger.info(
        "Queried %d edges (edge_type=%s, source_id=%s, target_id=%s, project_id=%s)",
        len(formatted_results),
        edge_type,
        source_id,
        target_id,
        project_id,
    )

    return formatted_results


def get_node_by_id(node_id: str, project_id: str | None = None) -> dict[str, Any] | None:
    """Get detailed information about a specific node by its ID.

    Args:
        node_id: The unique ID of the node.
        project_id: Project ID to scope the lookup to. If None, the node is
            matched across all projects (REQ-SCHEMA-006 scoping is applied only
            when a project id is supplied).

    Returns:
        A dictionary containing node details, or None if node not found.
        The dictionary includes:
        - id: Node ID
        - type: Node type (extracted from labels)
        - name: Node name
        - file_path: File path
        - properties: All node properties
        - outgoing_edges: List of outgoing edges (type, target_id, properties)
        - incoming_edges: List of incoming edges (type, source_id, properties)

    Raises:
        ValueError: If node_id is empty.
        ConnectionError: If Neo4j connection fails.
    """
    if not node_id:
        raise ValueError("node_id must not be empty")

    adapter = get_adapter()
    adapter.ensure_connected()

    # Scope every query to the configured project when provided.
    proj_where = " AND n.project_id = $project_id" if project_id else ""
    base_params: dict[str, Any] = {"node_id": node_id}
    if project_id:
        base_params["project_id"] = project_id

    # Query node details
    query = f"""
        MATCH (n:Node {{id: $node_id}})
        WHERE true{proj_where}
        RETURN n.id AS id,
               labels(n) AS labels,
               n.name AS name,
               n.file_path AS file_path,
               properties(n) AS properties
    """

    results = adapter.query(query, **base_params)

    if not results:
        logger.warning("Node with ID %s not found", node_id)
        return None

    result = results[0]
    labels = result.get("labels", [])
    node_type_from_labels = next((lbl for lbl in labels if lbl != "Node"), None)

    # Query outgoing edges
    out_where = " AND target.project_id = $project_id" if project_id else ""
    outgoing_query = f"""
        MATCH (n:Node {{id: $node_id}}) -[r]-> (target:Node)
        WHERE true{out_where}
        RETURN type(r) AS type,
               target.id AS target_id,
               properties(r) AS properties
        LIMIT 50
    """
    outgoing_results = adapter.query(outgoing_query, **base_params)
    outgoing_edges = [
        {
            "type": r.get("type"),
            "target_id": r.get("target_id"),
            "properties": r.get("properties", {}),
        }
        for r in outgoing_results
    ]

    # Query incoming edges
    in_where = " AND source.project_id = $project_id" if project_id else ""
    incoming_query = f"""
        MATCH (source:Node) -[r]-> (n:Node {{id: $node_id}})
        WHERE true{in_where}
        RETURN type(r) AS type,
               source.id AS source_id,
               properties(r) AS properties
        LIMIT 50
    """
    incoming_results = adapter.query(incoming_query, **base_params)
    incoming_edges = [
        {
            "type": r.get("type"),
            "source_id": r.get("source_id"),
            "properties": r.get("properties", {}),
        }
        for r in incoming_results
    ]

    node_details: dict[str, Any] = {
        "id": result.get("id"),
        "type": node_type_from_labels,
        "name": result.get("name"),
        "file_path": result.get("file_path"),
        "properties": result.get("properties", {}),
        "outgoing_edges": outgoing_edges,
        "incoming_edges": incoming_edges,
    }

    logger.info("Retrieved node details for ID %s", node_id)

    return node_details
