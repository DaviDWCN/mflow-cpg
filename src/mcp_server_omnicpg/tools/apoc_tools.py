"""APOC-powered graph tools for CPG analysis.

Exposes Neo4j APOC procedures as MCP tools, providing agents with:
- Flexible path expansion  (apoc.path.expandConfig)
- Full subgraph retrieval  (apoc.path.subgraphAll)
- Shortest-path search     (apoc.path.expandConfig with endNodes)
- Spanning-tree traversal  (apoc.path.spanningTree)
- Graph schema inspection  (apoc.meta.schema)
- Graph statistics         (apoc.meta.stats)
- Safe read-only Cypher    (apoc.cypher.run guard)
- CallSite -> Method lookup (fills missing method-level CALLS edges)
- Impact analysis           (who calls method X)
- Batch call-graph lookup   (UNWIND + expandConfig)
- Timboxed Cypher execution (apoc.cypher.runTimeboxed)

All APOC calls are read-only; write procedures are never invoked.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from neo4j.exceptions import ClientError

from mcp_server_omnicpg.neo4j_adapter import get_adapter

logger = logging.getLogger(__name__)

# ── Safety guard for apoc_run_read_query ──────────────────────────────────────
# Patterns that indicate a write operation - reject the query if matched.
_WRITE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD)\b", re.IGNORECASE),
    re.compile(
        r"\bcall\s+apoc\.[\w.]*?(write|update|delete|merge|create|import|export\.[\w]+\.data)\b",
        re.IGNORECASE,
    ),
]


def _is_apoc_meta_restricted(exc: Exception) -> bool:
    """Return True when an APOC meta procedure is blocked by Neo4j sandboxing."""
    if not isinstance(exc, ClientError):
        return False
    message = str(exc)
    return "ProcedureRegistrationFailed" in message and "sandbox" in message


def _is_safe_read_query(cypher: str) -> bool:
    """Return True only when the query contains no obvious write operations."""
    return all(not pattern.search(cypher) for pattern in _WRITE_PATTERNS)


def _requires_project_scope(cypher: str, project_id: str | None) -> None:
    """Reject raw Cypher that does not explicitly reference project_id.

    Raw read-only Cypher is powerful enough to bypass the normal tool-level
    scoping guarantees, so callers must provide a project_id and the query text
    must reference it explicitly. This keeps raw queries aligned with the
    repository-wide project isolation rule.
    """
    if project_id is None or not str(project_id).strip():
        raise ValueError("project_id must be provided for raw Cypher tools")
    if not re.search(r"\bproject_id\b", cypher, re.IGNORECASE):
        raise ValueError("cypher must explicitly filter by project_id")


def _node_summary(node: Any) -> dict[str, Any]:
    """Extract a compact summary dict from a raw Neo4j node object."""
    props: dict[str, Any] = dict(node.items()) if hasattr(node, "items") else {}
    return {
        "id": props.get("id"),
        "type": props.get("type"),
        "name": props.get("name"),
        "file_path": props.get("file_path"),
    }


def _rel_summary(rel: Any) -> dict[str, Any]:
    """Extract a compact summary dict from a raw Neo4j relationship object."""
    return {
        "type": rel.type if hasattr(rel, "type") else str(rel),
        "source": rel.start_node["id"] if hasattr(rel, "start_node") else None,
        "target": rel.end_node["id"] if hasattr(rel, "end_node") else None,
    }


# ── 1. apoc_expand_path ───────────────────────────────────────────────────────


def apoc_expand_path(
    start_node_id: str,
    relationship_filter: str = "",
    label_filter: str = "",
    min_level: int = 1,
    max_level: int = 3,
    bfs: bool = True,
    limit: int = 50,
    uniqueness: str = "NODE_GLOBAL",
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Flexible path expansion from a starting node using ``apoc.path.expandConfig``.

    The APOC procedure gives agents full control over traversal direction,
    relationship-type filtering, label filtering, and uniqueness semantics
    - far beyond what raw variable-length Cypher allows.

    Args:
        start_node_id: ID of the CPG node to start from.
        relationship_filter: APOC relationship filter string, e.g.
            ``"CALLS>|CONTAINS>"`` (``>`` = outgoing, ``<`` = incoming,
            empty direction = either).  Leave empty to traverse all types.
        label_filter: APOC label filter string, e.g.
            ``"+Method|+Class|-Variable"`` (``+`` include, ``-`` exclude,
            ``/`` end-node only, ``>`` terminus).
        min_level: Minimum traversal depth (default 1).
        max_level: Maximum traversal depth (default 3).
        bfs: Use BFS (breadth-first) when ``True``, DFS when ``False``.
        limit: Maximum number of paths to return (default 50).
        uniqueness: APOC uniqueness constraint -
            ``"NODE_GLOBAL"`` (default), ``"RELATIONSHIP_GLOBAL"``,
            ``"NONE"``, ``"NODE_PATH"``, ``"RELATIONSHIP_PATH"``.
        project_id: Optional project scope. When provided, only paths whose
            nodes belong to this project are returned.

    Returns:
        List of path dicts each containing ``node_ids``, ``edge_types``,
        and ``path_length``.

    Raises:
        ValueError: If ``max_level`` < ``min_level`` or ``limit`` < 1.
        ConnectionError: If Neo4j connection fails.
    """
    if max_level < min_level:
        raise ValueError("max_level must be >= min_level")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    start_scope = " AND start.project_id = $project_id" if project_id else ""
    path_scope = (
        "WHERE all(n IN nodes(path) WHERE n.project_id = $project_id)" if project_id else ""
    )

    query = (
        """
        MATCH (start:Node {id: $start_node_id})
        """
        + f"\n        WHERE true{start_scope}\n"
        + """
        CALL apoc.path.expandConfig(start, {
            relationshipFilter: $relationship_filter,
            labelFilter:        $label_filter,
            minLevel:           $min_level,
            maxLevel:           $max_level,
            bfs:                $bfs,
            limit:              $limit,
            uniqueness:         $uniqueness
        }) YIELD path
        """
        + (f"\n        {path_scope}\n" if path_scope else "\n")
        + """
        RETURN [n IN nodes(path) | n.id]    AS node_ids,
               [r IN relationships(path) | type(r)] AS edge_types,
               length(path)                AS path_length
        ORDER BY path_length
    """
    )

    results = adapter.query(
        query,
        start_node_id=start_node_id,
        relationship_filter=relationship_filter,
        label_filter=label_filter,
        min_level=min_level,
        max_level=max_level,
        bfs=bfs,
        limit=limit,
        uniqueness=uniqueness,
        project_id=project_id,
    )

    formatted: list[dict[str, Any]] = []
    for row in results:
        node_ids: list[Any] = row.get("node_ids") or []
        edge_types: list[Any] = row.get("edge_types") or []
        edges = [
            {"type": edge_types[i], "source": node_ids[i], "target": node_ids[i + 1]}
            for i in range(len(edge_types))
        ]
        formatted.append(
            {
                "nodes": node_ids,
                "edges": edges,
                "length": row.get("path_length", 0),
            }
        )

    logger.info(
        "apoc_expand_path: start=%s rel_filter=%r max_level=%d → %d paths",
        start_node_id,
        relationship_filter,
        max_level,
        len(formatted),
    )
    return formatted


# ── 2. apoc_subgraph_around_node ─────────────────────────────────────────────


def apoc_subgraph_around_node(
    node_id: str,
    relationship_filter: str = "",
    label_filter: str = "",
    max_level: int = 2,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Retrieve the full subgraph around a node using ``apoc.path.subgraphAll``.

    Returns all reachable nodes and relationships within *max_level* hops,
    making it ideal for "show me everything related to X" queries.

    Args:
        node_id: ID of the centre CPG node.
        relationship_filter: APOC relationship filter (e.g. ``"CALLS>|CONTAINS>"``).
        label_filter: APOC label filter (e.g. ``"+Method|+Class"``).
        max_level: Maximum hop distance from the centre node (default 2).
        project_id: Optional project scope. When provided, center/node/edge
            results are constrained to this project.

    Returns:
        Dict with keys ``"center_id"``, ``"nodes"`` (list of node summaries),
        and ``"relationships"`` (list of edge summaries).

    Raises:
        ValueError: If ``max_level`` < 1.
        ConnectionError: If Neo4j connection fails.
    """
    if max_level < 1:
        raise ValueError("max_level must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    center_scope = " AND center.project_id = $project_id" if project_id else ""
    node_scope = " WHERE n.project_id = $project_id" if project_id else ""
    rel_scope = (
        " WHERE startNode(r).project_id = $project_id AND endNode(r).project_id = $project_id"
        if project_id
        else ""
    )

    query = (
        """
        MATCH (center:Node {id: $node_id})
        """
        + f"\n        WHERE true{center_scope}\n"
        + """
        CALL apoc.path.subgraphAll(center, {
            relationshipFilter: $relationship_filter,
            labelFilter:        $label_filter,
            maxLevel:           $max_level
        }) YIELD nodes, relationships
        RETURN [n IN nodes
        """
        + node_scope
        + """
               | {
                   id:        n.id,
                   type:      n.type,
                   name:      n.name,
                   file_path: n.file_path
               }]                                        AS nodes,
               [r IN relationships
        """
        + rel_scope
        + """
               | {
                   type:   type(r),
                   source: startNode(r).id,
                   target: endNode(r).id
               }]                                        AS relationships
    """
    )

    results = adapter.query(
        query,
        node_id=node_id,
        relationship_filter=relationship_filter,
        label_filter=label_filter,
        max_level=max_level,
        project_id=project_id,
    )

    if not results:
        return {"center_id": node_id, "nodes": [], "relationships": []}

    row = results[0]
    nodes: list[Any] = row.get("nodes") or []
    rels: list[Any] = row.get("relationships") or []

    logger.info(
        "apoc_subgraph_around_node: node=%s max_level=%d → %d nodes, %d rels",
        node_id,
        max_level,
        len(nodes),
        len(rels),
    )
    return {
        "center_id": node_id,
        "nodes": nodes,
        "relationships": rels,
    }


# ── 3. apoc_shortest_path ─────────────────────────────────────────────────────


def apoc_shortest_path(
    start_node_id: str,
    end_node_id: str,
    relationship_filter: str = "",
    max_level: int = 10,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Find the shortest directed path between two nodes via BFS (APOC).

    Uses ``apoc.path.expandConfig`` with ``endNodes`` and ``bfs=true`` so
    the first returned path is guaranteed to be the shortest.

    Args:
        start_node_id: ID of the source CPG node.
        end_node_id: ID of the target CPG node.
        relationship_filter: APOC relationship filter (e.g. ``"CALLS>|REACHES>"``).
            Leave empty to allow any relationship type.
        max_level: Search radius - bail out if no path found within this many
            hops (default 10).
        project_id: Optional project scope. When provided, start/end and all
            path nodes must belong to this project.

    Returns:
        A single path dict with ``nodes``, ``edges``, and ``length``; or
        ``None`` if no path exists within *max_level*.

    Raises:
        ValueError: If ``max_level`` < 1.
        ConnectionError: If Neo4j connection fails.
    """
    if max_level < 1:
        raise ValueError("max_level must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    endpoint_scope = (
        " AND start.project_id = $project_id AND end.project_id = $project_id"
        if project_id
        else ""
    )
    path_scope = (
        "WHERE all(n IN nodes(path) WHERE n.project_id = $project_id)" if project_id else ""
    )

    query = (
        """
        MATCH (start:Node {id: $start_node_id}), (end:Node {id: $end_node_id})
        """
        + f"\n        WHERE true{endpoint_scope}\n"
        + """
        CALL apoc.path.expandConfig(start, {
            endNodes:           [end],
            relationshipFilter: $relationship_filter,
            maxLevel:           $max_level,
            bfs:                true,
            uniqueness:         'NODE_GLOBAL',
            limit:              1
        }) YIELD path
        """
        + (f"\n        {path_scope}\n" if path_scope else "\n")
        + """
        RETURN [n IN nodes(path) | n.id]            AS node_ids,
               [r IN relationships(path) | type(r)] AS edge_types,
               length(path)                         AS path_length
        LIMIT 1
    """
    )

    results = adapter.query(
        query,
        start_node_id=start_node_id,
        end_node_id=end_node_id,
        relationship_filter=relationship_filter,
        max_level=max_level,
        project_id=project_id,
    )

    if not results:
        logger.info(
            "apoc_shortest_path: no path from %s to %s within %d hops",
            start_node_id,
            end_node_id,
            max_level,
        )
        return None

    row = results[0]
    node_ids: list[Any] = row.get("node_ids") or []
    edge_types: list[Any] = row.get("edge_types") or []
    edges = [
        {"type": edge_types[i], "source": node_ids[i], "target": node_ids[i + 1]}
        for i in range(len(edge_types))
    ]

    logger.info(
        "apoc_shortest_path: %s → %s length=%d",
        start_node_id,
        end_node_id,
        row.get("path_length", 0),
    )
    return {
        "nodes": node_ids,
        "edges": edges,
        "length": row.get("path_length", 0),
    }


# ── 4. apoc_spanning_tree ─────────────────────────────────────────────────────


def apoc_spanning_tree(
    start_node_id: str,
    relationship_filter: str = "",
    label_filter: str = "",
    max_level: int = 3,
    bfs: bool = True,
    limit: int = 50,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compute a spanning tree rooted at *start_node_id* via ``apoc.path.spanningTree``.

    Each node is visited at most once (spanning = no cycles), making this
    ideal for hierarchical code-structure exploration (e.g. class → methods →
    statements).

    Args:
        start_node_id: Root CPG node ID.
        relationship_filter: APOC relationship filter string.
        label_filter: APOC label filter string.
        max_level: Maximum tree depth (default 3).
        bfs: BFS when ``True`` (level-by-level), DFS when ``False``.
        limit: Maximum number of paths returned (default 50).
        project_id: Optional project scope. When provided, root and all path
            nodes must belong to this project.

    Returns:
        List of path dicts, each containing ``nodes``, ``edges``, and ``depth``.

    Raises:
        ValueError: If ``max_level`` < 1 or ``limit`` < 1.
        ConnectionError: If Neo4j connection fails.
    """
    if max_level < 1:
        raise ValueError("max_level must be at least 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    start_scope = " AND start.project_id = $project_id" if project_id else ""
    path_scope = (
        "WHERE all(n IN nodes(path) WHERE n.project_id = $project_id)" if project_id else ""
    )

    query = (
        """
        MATCH (start:Node {id: $start_node_id})
        """
        + f"\n        WHERE true{start_scope}\n"
        + """
        CALL apoc.path.spanningTree(start, {
            relationshipFilter: $relationship_filter,
            labelFilter:        $label_filter,
            maxLevel:           $max_level,
            bfs:                $bfs,
            limit:              $limit
        }) YIELD path
        """
        + (f"\n        {path_scope}\n" if path_scope else "\n")
        + """
        RETURN [n IN nodes(path) | {
                   id:   n.id,
                   type: n.type,
                   name: n.name
               }]                                        AS nodes,
               [r IN relationships(path) | type(r)]     AS edge_types,
               length(path)                             AS depth
        ORDER BY depth
    """
    )

    results = adapter.query(
        query,
        start_node_id=start_node_id,
        relationship_filter=relationship_filter,
        label_filter=label_filter,
        max_level=max_level,
        bfs=bfs,
        limit=limit,
        project_id=project_id,
    )

    formatted: list[dict[str, Any]] = []
    for row in results:
        nodes: list[Any] = row.get("nodes") or []
        edge_types: list[Any] = row.get("edge_types") or []
        # Build directed edge list from consecutive node pairs
        edges = [
            {
                "type": edge_types[i],
                "source": nodes[i].get("id"),
                "target": nodes[i + 1].get("id"),
            }
            for i in range(len(edge_types))
        ]
        formatted.append(
            {
                "nodes": nodes,
                "edges": edges,
                "depth": row.get("depth", 0),
            }
        )

    logger.info(
        "apoc_spanning_tree: root=%s max_level=%d → %d paths",
        start_node_id,
        max_level,
        len(formatted),
    )
    return formatted


# ── 5. apoc_graph_schema ──────────────────────────────────────────────────────


def apoc_graph_schema() -> dict[str, Any]:
    """Return the graph schema via ``apoc.meta.schema()``.

    Provides agents with a high-level map of node labels, their properties,
    and the relationship types that connect them - essential for composing
    correct Cypher queries without guessing.

    Returns:
        Dict with key ``"schema"`` containing the raw ``apoc.meta.schema``
        output (a nested label → property/relationship structure).

    Raises:
        ConnectionError: If Neo4j connection fails.
    """
    adapter = get_adapter()
    adapter.ensure_connected()

    query = "CALL apoc.meta.schema() YIELD value RETURN value"
    try:
        results = adapter.query(query)
        schema = results[0]["value"] if results else {}
        logger.info("apoc_graph_schema: returned schema with %d label(s)", len(schema))
        return {"schema": schema}
    except Exception as exc:
        if not _is_apoc_meta_restricted(exc):
            raise

    # Fallback for restricted APOC environments:
    # derive schema from labels/properties/relationship patterns using plain Cypher.
    logger.warning("apoc.meta.schema unavailable; using Cypher fallback schema")
    label_rows = adapter.query(
        """
        MATCH (n)
        UNWIND labels(n) AS label
        WITH label, keys(n) AS props
        UNWIND props AS prop
        RETURN label, collect(DISTINCT prop) AS properties
        """
    )
    rel_rows = adapter.query(
        """
        MATCH (a)-[r]->(b)
        UNWIND labels(a) AS from_label
        UNWIND labels(b) AS to_label
        RETURN from_label, type(r) AS rel_type, collect(DISTINCT to_label) AS targets
        """
    )

    schema_map: dict[str, Any] = {}
    for row in label_rows:
        label = str(row.get("label", ""))
        if not label:
            continue
        props = sorted(str(p) for p in (row.get("properties") or []))
        schema_map[label] = {
            "type": "node",
            "properties": {p: {} for p in props},
            "relationships": {},
        }

    for row in rel_rows:
        from_label = str(row.get("from_label", ""))
        rel_type = str(row.get("rel_type", ""))
        targets = sorted(str(t) for t in (row.get("targets") or []))
        if not from_label or not rel_type:
            continue
        schema_map.setdefault(
            from_label,
            {"type": "node", "properties": {}, "relationships": {}},
        )
        schema_map[from_label]["relationships"][rel_type] = {
            "direction": "out",
            "labels": targets,
        }

    logger.info("apoc_graph_schema fallback: derived schema with %d label(s)", len(schema_map))
    return {"schema": schema_map}


# ── 6. apoc_meta_stats ────────────────────────────────────────────────────────


def apoc_meta_stats() -> dict[str, Any]:
    """Return graph-level statistics via ``apoc.meta.stats()``.

    Provides agents with a quick overview of the graph size: total node and
    relationship counts broken down by label / relationship type.

    Returns:
        Dict with keys ``nodeCount``, ``relCount``, ``labelCount``,
        ``relTypeCount``, ``propertyKeyCount``, ``labels``, ``relTypes``.

    Raises:
        ConnectionError: If Neo4j connection fails.
    """
    adapter = get_adapter()
    adapter.ensure_connected()

    query = """
        CALL apoc.meta.stats()
        YIELD nodeCount, relCount, labelCount, relTypeCount,
              propertyKeyCount, labels, relTypes
        RETURN nodeCount, relCount, labelCount, relTypeCount,
               propertyKeyCount, labels, relTypes
    """
    try:
        results = adapter.query(query)
        if not results:
            return {}

        row = results[0]
        stats: dict[str, Any] = {
            "nodeCount": row.get("nodeCount"),
            "relCount": row.get("relCount"),
            "labelCount": row.get("labelCount"),
            "relTypeCount": row.get("relTypeCount"),
            "propertyKeyCount": row.get("propertyKeyCount"),
            "labels": row.get("labels"),
            "relTypes": row.get("relTypes"),
        }

        logger.info(
            "apoc_meta_stats: %d nodes, %d rels",
            stats.get("nodeCount", 0),
            stats.get("relCount", 0),
        )
        return stats
    except Exception as exc:
        if not _is_apoc_meta_restricted(exc):
            raise

    logger.warning("apoc.meta.stats unavailable; using Cypher fallback stats")

    node_count_row = adapter.query("MATCH (n) RETURN count(n) AS c")
    rel_count_row = adapter.query("MATCH ()-[r]->() RETURN count(r) AS c")
    labels_rows = adapter.query("MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS c")
    rel_types_rows = adapter.query("MATCH ()-[r]->() RETURN type(r) AS rel_type, count(*) AS c")
    property_key_rows = adapter.query(
        """
        MATCH (n)
        UNWIND keys(n) AS key
        RETURN collect(DISTINCT key) AS node_keys
        """
    )
    rel_property_key_rows = adapter.query(
        """
        MATCH ()-[r]->()
        UNWIND keys(r) AS key
        RETURN collect(DISTINCT key) AS rel_keys
        """
    )

    labels = {str(r["label"]): int(r["c"]) for r in labels_rows}
    rel_types = {str(r["rel_type"]): int(r["c"]) for r in rel_types_rows}

    node_keys = set(property_key_rows[0].get("node_keys", [])) if property_key_rows else set()
    rel_keys = (
        set(rel_property_key_rows[0].get("rel_keys", [])) if rel_property_key_rows else set()
    )

    stats = {
        "nodeCount": int(node_count_row[0]["c"]) if node_count_row else 0,
        "relCount": int(rel_count_row[0]["c"]) if rel_count_row else 0,
        "labelCount": len(labels),
        "relTypeCount": len(rel_types),
        "propertyKeyCount": len(node_keys | rel_keys),
        "labels": labels,
        "relTypes": rel_types,
    }

    logger.info(
        "apoc_meta_stats fallback: %d nodes, %d rels",
        stats["nodeCount"],
        stats["relCount"],
    )
    return stats


# ── 7. apoc_run_read_query ────────────────────────────────────────────────────


def apoc_run_read_query(
    cypher: str,
    params: dict[str, Any] | None = None,
    limit: int = 100,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a *read-only* Cypher query against the CPG graph.

    This is the most flexible tool for agents: when no pre-built tool covers
    your query, write the Cypher yourself.  The server enforces a read-only
    guard by rejecting queries that contain write keywords (``CREATE``,
    ``MERGE``, ``DELETE``, ``SET``, ``REMOVE``, ``DROP``, ``LOAD``).

    An implicit ``LIMIT`` clause is appended when the query does not already
    contain one, capped at *limit* rows.

    Args:
        cypher: A read-only Cypher query string.  Named parameters in the
            query must be passed via *params*.
        params: Optional dict of Cypher parameter names → values.
        limit: Maximum number of rows to return (default 100, max 500).
        project_id: Required project scope. The Cypher text must explicitly
            reference ``project_id`` to preserve multi-project isolation.

    Returns:
        List of result row dicts.

    Raises:
        ValueError: If the query appears to contain write operations,
            or if *limit* is out of the allowed range.
        ConnectionError: If Neo4j connection fails.
    """
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")

    if not cypher or not cypher.strip():
        raise ValueError("cypher must not be empty")

    if not _is_safe_read_query(cypher):
        raise ValueError(
            "Query rejected: write operations (CREATE, MERGE, DELETE, SET, REMOVE, "
            "DROP, LOAD) are not permitted via apoc_run_read_query."
        )
    _requires_project_scope(cypher, project_id)

    # Append LIMIT if not already present
    stripped = cypher.rstrip().rstrip(";")
    if not re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        stripped = f"{stripped}\nLIMIT {limit}"

    adapter = get_adapter()
    adapter.ensure_connected()

    effective_params: dict[str, Any] = params or {}
    effective_params["project_id"] = project_id
    results = adapter.query(stripped, **effective_params)

    logger.info(
        "apoc_run_read_query: returned %d rows for query %.80r",
        len(results),
        cypher,
    )
    return [dict(row) for row in results]


# ── 8. find_callsite_method ───────────────────────────────────────────────────


def find_callsite_method(
    callsite_name: str,
    file_path_contains: str = "",
    max_depth: int = 15,
    limit: int = 20,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find the Method node(s) that *contain* a named CallSite in the AST.

    Java analysis in OmniCPG does **not** emit method-level ``CALLS`` edges.
    Call relationships must be reconstructed by locating ``CallSite`` nodes
    (which are leaves in the AST) and traversing *up* the ``PARENT_OF``
    edges (i.e. following ``<PARENT_OF`` backwards) until a ``Method`` node
    is reached.

    This is the single most important query pattern for Java call-graph
    analysis and directly replaces the missing ``CALLS`` edges.

    Args:
        callsite_name: The ``name`` property of the CallSite node, e.g.
            ``"execute"``, ``"save"``, ``"getMap"``.
        file_path_contains: Optional substring to restrict the search to a
            specific project, directory, or file (e.g. ``"hcscore"``).
            Leave empty to search across all projects.
        max_depth: Maximum ``PARENT_OF`` traversal depth when climbing the
            AST to find the enclosing Method.  Default 15 is sufficient for
            typical Java ASTs; increase if you have deeply nested lambdas.
        limit: Maximum number of (CallSite, Method) pairs to return
            (default 20).
        project_id: Optional project scope used to filter CallSite and
            enclosing Method nodes.

    Returns:
        List of dicts each containing:
        - ``callsite_code``: source snippet of the call expression.
        - ``callsite_line``: line number of the CallSite.
        - ``callsite_file``: file path of the CallSite.
        - ``callsite_id``: CPG node ID of the CallSite.
        - ``method_name``: name of the enclosing Method.
        - ``method_file``: file path of the enclosing Method.
        - ``method_id``: CPG node ID of the enclosing Method.
        - ``depth``: number of ``PARENT_OF`` hops from CallSite to Method.

    Raises:
        ValueError: If *limit* < 1 or *max_depth* < 1.
        ConnectionError: If Neo4j connection fails.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    # Filter callsites by name, optional file path, and optional project scope.
    conditions: list[str] = []
    if file_path_contains:
        conditions.append("cs.file_path CONTAINS $file_path_contains")
    if project_id:
        conditions.append("cs.project_id = $project_id")
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    match_clause = "MATCH (cs:CallSite {name: $callsite_name})"

    query = f"""
        {match_clause}
        {where_clause}
        WITH cs LIMIT $limit
        CALL apoc.path.expandConfig(cs, {{
            relationshipFilter: '<PARENT_OF',
            maxLevel:           $max_depth,
            terminatorLabels:   ['Method'],
            filterStartNode:    false,
            bfs:                true,
            uniqueness:         'NODE_GLOBAL'
        }}) YIELD path
                WITH cs, nodes(path)[-1] AS method, length(path) AS depth
                WHERE method:Method
                    AND ($project_id IS NULL OR method.project_id = $project_id)
        RETURN cs.id          AS callsite_id,
               cs.code        AS callsite_code,
               cs.line_start  AS callsite_line,
               cs.file_path   AS callsite_file,
               method.name    AS method_name,
               method.file_path AS method_file,
               method.id      AS method_id,
               depth
        ORDER BY callsite_file, callsite_line
        LIMIT $limit
    """

    results = adapter.query(
        query,
        callsite_name=callsite_name,
        file_path_contains=file_path_contains,
        max_depth=max_depth,
        limit=limit,
        project_id=project_id,
    )

    formatted: list[dict[str, Any]] = [
        {
            "callsite_id": row.get("callsite_id"),
            "callsite_code": row.get("callsite_code"),
            "callsite_line": row.get("callsite_line"),
            "callsite_file": row.get("callsite_file"),
            "method_name": row.get("method_name"),
            "method_file": row.get("method_file"),
            "method_id": row.get("method_id"),
            "depth": row.get("depth"),
        }
        for row in results
    ]

    logger.info(
        "find_callsite_method: name=%r file_filter=%r -> %d results",
        callsite_name,
        file_path_contains,
        len(formatted),
    )
    return formatted


# ── 9. find_callers_of ────────────────────────────────────────────────────────


def find_callers_of(
    method_name: str,
    file_path_contains: str = "",
    max_depth: int = 15,
    limit: int = 20,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Impact analysis: find all Methods that call a given method by name.

    Builds an inbound call graph for *method_name* by:
    1. Matching all ``CallSite`` nodes whose ``name`` matches *method_name*.
    2. Traversing *up* the AST via ``<PARENT_OF`` until a ``Method`` is reached.
    3. Returning the containing Method for each CallSite.

    This directly implements the "who calls X?" impact-analysis pattern from
    the hcscore guide and compensates for the absent method-level CALLS edges.

    Args:
        method_name: The name of the method being called, e.g. ``"getMap"``,
            ``"delete"``.  Matched against ``CallSite.name``.
        file_path_contains: Optional path substring to restrict results
            (e.g. ``"hcscore"``).  Empty = search all projects.
        max_depth: Maximum ``PARENT_OF`` hops when climbing the AST
            (default 15).
        limit: Maximum number of caller (Method, CallSite) pairs returned
            (default 20).
        project_id: Optional project scope used to filter CallSite and caller
            Method nodes.

    Returns:
        List of dicts each containing:
        - ``caller_method``: name of the Method that contains the call.
        - ``caller_file``: file path of the caller Method.
        - ``caller_id``: CPG node ID of the caller Method.
        - ``call_code``: source snippet of the call expression.
        - ``call_line``: line number of the call.

    Raises:
        ValueError: If *limit* < 1 or *max_depth* < 1.
        ConnectionError: If Neo4j connection fails.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    conditions: list[str] = []
    if file_path_contains:
        conditions.append("cs.file_path CONTAINS $file_path_contains")
    if project_id:
        conditions.append("cs.project_id = $project_id")
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    match_clause = "MATCH (cs:CallSite {name: $method_name})"

    query = f"""
        {match_clause}
        {where_clause}
        WITH cs LIMIT $limit
        CALL apoc.path.expandConfig(cs, {{
            relationshipFilter: '<PARENT_OF',
            maxLevel:           $max_depth,
            terminatorLabels:   ['Method'],
            filterStartNode:    false,
            bfs:                true,
            uniqueness:         'NODE_GLOBAL'
        }}) YIELD path
                WITH cs, nodes(path)[-1] AS caller
                WHERE caller:Method
                    AND ($project_id IS NULL OR caller.project_id = $project_id)
        RETURN caller.name     AS caller_method,
               caller.file_path AS caller_file,
               caller.id       AS caller_id,
               cs.code         AS call_code,
               cs.line_start   AS call_line
        ORDER BY caller_file, call_line
        LIMIT $limit
    """

    results = adapter.query(
        query,
        method_name=method_name,
        file_path_contains=file_path_contains,
        max_depth=max_depth,
        limit=limit,
        project_id=project_id,
    )

    formatted: list[dict[str, Any]] = [
        {
            "caller_method": row.get("caller_method"),
            "caller_file": row.get("caller_file"),
            "caller_id": row.get("caller_id"),
            "call_code": row.get("call_code"),
            "call_line": row.get("call_line"),
        }
        for row in results
    ]

    logger.info(
        "find_callers_of: method=%r file_filter=%r -> %d callers",
        method_name,
        file_path_contains,
        len(formatted),
    )
    return formatted


# ── 10. batch_callsite_methods ────────────────────────────────────────────────


def batch_callsite_methods(
    callsite_names: list[str],
    file_path_contains: str = "",
    max_depth: int = 15,
    limit_per_name: int = 10,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Batch call-graph lookup: for each CallSite name, find its containing Method.

    Equivalent to running ``find_callsite_method`` for every name in
    *callsite_names* in a single round-trip, using an ``UNWIND`` + ``APOC``
    pattern.  Use this for bulk "who implements X / who calls Y" analyses.

    Args:
        callsite_names: List of CallSite ``name`` values to look up, e.g.
            ``["execute", "save", "delete", "update", "find"]``.
        file_path_contains: Optional path substring to restrict the search
            (e.g. ``"hcscore"``).
        max_depth: Maximum ``PARENT_OF`` hops (default 15).
        limit_per_name: Maximum (CallSite, Method) pairs *per name* to return
            (default 10).  Total rows can be up to
            ``len(callsite_names) * limit_per_name``.
        project_id: Optional project scope used to filter CallSite and
            enclosing Method nodes.

    Returns:
        List of dicts each containing:
        - ``callsite_name``: the queried call name.
        - ``callsite_code``: source snippet of the call expression.
        - ``callsite_line``: line number.
        - ``callsite_file``: file path of the CallSite.
        - ``method_name``: name of the enclosing Method.
        - ``method_file``: file path of the enclosing Method.
        - ``method_id``: CPG node ID of the enclosing Method.

    Raises:
        ValueError: If *callsite_names* is empty, *limit_per_name* < 1, or
            *max_depth* < 1.
        ConnectionError: If Neo4j connection fails.
    """
    if not callsite_names:
        raise ValueError("callsite_names must not be empty")
    if limit_per_name < 1:
        raise ValueError("limit_per_name must be at least 1")
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    filter_conditions: list[str] = []
    if file_path_contains:
        filter_conditions.append("cs.file_path CONTAINS $file_path_contains")
    if project_id:
        filter_conditions.append("cs.project_id = $project_id")
    filter_clause = f"WHERE {' AND '.join(filter_conditions)}" if filter_conditions else ""

    query = f"""
        UNWIND $callsite_names AS csName
        MATCH (cs:CallSite {{name: csName}})
        {filter_clause}
        WITH csName, cs LIMIT $limit_per_name
        CALL apoc.path.expandConfig(cs, {{
            relationshipFilter: '<PARENT_OF',
            maxLevel:           $max_depth,
            terminatorLabels:   ['Method'],
            filterStartNode:    false,
            bfs:                true,
            uniqueness:         'NODE_GLOBAL'
        }}) YIELD path
                WITH csName, cs, nodes(path)[-1] AS method
                WHERE method:Method
                    AND ($project_id IS NULL OR method.project_id = $project_id)
        RETURN csName          AS callsite_name,
               cs.code        AS callsite_code,
               cs.line_start  AS callsite_line,
               cs.file_path   AS callsite_file,
               method.name    AS method_name,
               method.file_path AS method_file,
               method.id      AS method_id
        ORDER BY csName, callsite_file, callsite_line
    """

    results = adapter.query(
        query,
        callsite_names=callsite_names,
        file_path_contains=file_path_contains,
        max_depth=max_depth,
        limit_per_name=limit_per_name,
        project_id=project_id,
    )

    formatted: list[dict[str, Any]] = [
        {
            "callsite_name": row.get("callsite_name"),
            "callsite_code": row.get("callsite_code"),
            "callsite_line": row.get("callsite_line"),
            "callsite_file": row.get("callsite_file"),
            "method_name": row.get("method_name"),
            "method_file": row.get("method_file"),
            "method_id": row.get("method_id"),
        }
        for row in results
    ]

    logger.info(
        "batch_callsite_methods: %d names, file_filter=%r -> %d results",
        len(callsite_names),
        file_path_contains,
        len(formatted),
    )
    return formatted


# ── 11. apoc_run_timeboxed_query ───────────────────────────────────────────────


def apoc_run_timeboxed_query(
    cypher: str,
    params: dict[str, Any] | None = None,
    timeout_ms: int = 10_000,
    limit: int = 100,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only Cypher query with a hard timeout via ``apoc.cypher.runTimeboxed``.

    Wraps ``apoc.cypher.runTimeboxed(query, params, timeoutMs)`` so that
    potentially slow traversals (e.g. deep ``REACHES`` data-flow paths)
    terminate gracefully instead of blocking the MCP server.

    The same write-operation guard used by ``apoc_run_read_query`` is applied
    here, rejecting queries that contain ``CREATE``, ``MERGE``, ``DELETE``,
    ``SET``, ``REMOVE``, ``DROP``, or ``LOAD``.

    Args:
        cypher: A read-only Cypher query string.  Use ``$paramName`` syntax
            for any named parameters; pass their values via *params*.
        params: Optional dict of Cypher parameter names to values.
        timeout_ms: Millisecond budget for the inner query.  The procedure
            returns partial results if it exceeds this value.  Default 10 000
            (10 seconds) - recommended for REACHES depth > 2 traversals.
            Maximum 60 000 (1 minute).
        limit: Maximum number of rows to return after the timeboxed execution
            (default 100, max 500).
        project_id: Required project scope. The Cypher text must explicitly
            reference ``project_id`` to preserve multi-project isolation.

    Returns:
        List of result row dicts.  Each dict has the same keys as the
        ``RETURN`` clause of *cypher*, accessed via the ``value`` alias that
        ``apoc.cypher.runTimeboxed`` wraps them in.

    Raises:
        ValueError: If *cypher* contains write operations, is empty, *limit*
            is out of range, or *timeout_ms* is out of the 1-60 000 range.
        ConnectionError: If Neo4j connection fails.
    """
    if timeout_ms < 1 or timeout_ms > 60_000:
        raise ValueError("timeout_ms must be between 1 and 60000")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if not cypher or not cypher.strip():
        raise ValueError("cypher must not be empty")
    if not _is_safe_read_query(cypher):
        raise ValueError(
            "Query rejected: write operations (CREATE, MERGE, DELETE, SET, REMOVE, "
            "DROP, LOAD) are not permitted."
        )
    _requires_project_scope(cypher, project_id)

    adapter = get_adapter()
    adapter.ensure_connected()

    effective_params: dict[str, Any] = params or {}

    # apoc.cypher.runTimeboxed returns rows as {key: value} via YIELD value
    wrapper = f"""
        CALL apoc.cypher.runTimeboxed($cypher, $params, $timeout_ms)
        YIELD value
        RETURN value
        LIMIT {limit}
    """

    rows = adapter.query(
        wrapper,
        cypher=cypher,
        params=effective_params,
        timeout_ms=timeout_ms,
        project_id=project_id,
    )

    # Unwrap the `value` map so callers get plain dicts like apoc_run_read_query
    unwrapped: list[dict[str, Any]] = [
        dict(row["value"]) if isinstance(row.get("value"), dict) else row for row in rows
    ]

    logger.info(
        "apoc_run_timeboxed_query: returned %d rows (timeout=%dms) for %.80r",
        len(unwrapped),
        timeout_ms,
        cypher,
    )
    return unwrapped
