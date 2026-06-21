"""Advanced analysis MCP tools for OmniCPG.

This module implements the six *advanced analysis* tools introduced by the
``mcp-advanced-tools`` openspec change. Each tool is deterministic (no LLM
calls, no network, no extra dependencies — REQ-MCP-007), scopes every Cypher
query to the configured ``project_id`` (REQ-SCHEMA-006), and returns a
JSON-friendly structure (or ``{"error": ...}`` on failure rather than raising).

Tools:
    * ``detect_security_issues``   (REQ-MCP-007, SC-MCP-006, SC-TAINT-004)
    * ``analyze_code_complexity``  (SC-MCP-007)
    * ``find_dead_code``
    * ``analyze_change_impact``
    * ``find_similar_code``
    * ``get_architecture_metrics`` (SC-MCP-007 coverage marker)
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server_omnicpg.neo4j_adapter import get_adapter

logger = logging.getLogger(__name__)


def _location(row: dict[str, Any]) -> str | None:
    """Build a ``file_path:line`` location string from a result row.

    Args:
        row: A query result row that may carry ``file_path`` and ``line``.

    Returns:
        A ``"file_path:line"`` string, the bare ``file_path`` when no line is
        present, or ``None`` when the row carries no file path.
    """
    file_path = row.get("file_path")
    if not file_path:
        return None
    line = row.get("line")
    if line is None:
        return str(file_path)
    return f"{file_path}:{line}"


def detect_security_issues(
    rules: dict[str, Any] | None = None,
    limit: int = 50,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Detect security findings via dataflow reachability and literal scanning.

    Combines two finding classes:

    * **Dataflow** (e.g. SQL injection): source -> sink ``REACHES`` reachability,
      surfacing the interprocedural edge evidence on each finding.
    * **Literal** (e.g. hardcoded secrets): property/full-text matches on
      suspicious literal assignments.

    Args:
        rules: Optional rule overrides (source/sink/secret patterns). When
            ``None`` the built-in rule set is used.
        limit: Maximum number of findings to return.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006). When
            ``None`` the query spans all projects.

    Returns:
        An envelope ``{"findings": [...], "count": int}``. Each finding carries
        ``rule``, ``severity``, ``node_id``, a ``location`` (``file_path:line``),
        and — for dataflow findings — ``source``, ``sink`` and ``interprocedural``
        edge evidence. On failure returns ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        literal_proj = " AND n.project_id = $project_id" if project_id else ""

        dataflow_query = f"""
                        MATCH path = (src:Node)-[r:REACHES|FLOWS_TO*1..10]->(tainted:Node)
            WHERE (src.is_source = true OR src.security_role = 'source')
                            {"AND src.project_id = $project_id AND tainted.project_id = $project_id" if project_id else ""}
                        MATCH sink_path = (snk:Node)-[:PARENT_OF*0..6]->(tainted)
                        WHERE (snk.is_sink = true OR snk.security_role = 'sink')
                            {"AND snk.project_id = $project_id" if project_id else ""}
            RETURN src.id AS source_id, snk.id AS sink_id, snk.id AS node_id,
                   snk.file_path AS file_path,
                   coalesce(snk.line, snk.line_start) AS line,
                   coalesce(snk.security_category, snk.rule, 'dataflow') AS rule,
                   coalesce(snk.severity, 'high') AS severity,
                                     tainted.id AS tainted_id,
                                     tainted.type AS tainted_type,
                   coalesce(
                       head([
                           rel IN relationships(path)
                           WHERE rel.interprocedural IS NOT NULL
                           | rel.interprocedural
                       ]),
                       'unknown'
                   ) AS interprocedural,
                   [
                       rel IN relationships(path)
                       | coalesce(rel.interprocedural, rel.kind, type(rel))
                   ] AS path
            LIMIT $limit
        """

        literal_query = f"""
            MATCH (n:Node)
            WHERE n.type IN ['literal', 'string_literal', 'string_fragment']
              AND coalesce(n.code, n.source_code, n.name, n.value, '')
                  =~ '(?i).*(password|secret|api_key|token).*'{literal_proj}
            RETURN n.id AS node_id, n.file_path AS file_path, n.line AS line,
                   coalesce(n.code, n.source_code, n.name, n.value) AS code,
                   'hardcoded-secret' AS rule,
                   'high' AS severity
            LIMIT $limit
        """

        findings: list[dict[str, Any]] = []
        branch_counts = {"dataflow": 0, "literal": 0}
        seen: set[tuple[Any, Any]] = set()

        for query, default_rule, branch in (
            (dataflow_query, "sql-injection", "dataflow"),
            (literal_query, "hardcoded-secret", "literal"),
        ):
            rows = adapter.query(query, project_id=project_id, limit=limit)
            for row in rows:
                node_id = row.get("node_id") or row.get("id")
                rule = row.get("rule") or default_rule
                key = (node_id, rule)
                if key in seen:
                    continue
                seen.add(key)
                finding: dict[str, Any] = {
                    "rule": rule,
                    "severity": row.get("severity", "medium"),
                    "node_id": node_id,
                    "location": _location(row),
                    "file_path": row.get("file_path"),
                    "line": row.get("line"),
                }
                if row.get("source_id") is not None:
                    finding["source"] = row.get("source_id")
                if row.get("sink_id") is not None:
                    finding["sink"] = row.get("sink_id")
                if row.get("tainted_id") is not None:
                    finding["tainted"] = row.get("tainted_id")
                if row.get("tainted_type") is not None:
                    finding["tainted_type"] = row.get("tainted_type")
                if row.get("interprocedural") is not None:
                    finding["interprocedural"] = row.get("interprocedural")
                if row.get("path") is not None:
                    finding["path"] = row.get("path")
                if row.get("code") is not None:
                    finding["code"] = row.get("code")
                findings.append(finding)
                branch_counts[branch] += 1

        limited = findings[:limit]
        result: dict[str, Any] = {
            "findings": limited,
            "count": len(limited),
            "dataflow_count": branch_counts["dataflow"],
            "literal_count": branch_counts["literal"],
        }
        if branch_counts["dataflow"] == 0:
            result["dataflow_coverage"] = "empty"
            result["dataflow_warning"] = "no source-to-sink REACHES/FLOWS_TO path found"
        if not limited:
            result["coverage"] = "empty"
            result["warning"] = (
                "no security indicators found; source/sink enrichment may be missing"
            )
        return result
    except Exception as exc:
        logger.exception("detect_security_issues failed")
        return {"error": str(exc)}


def analyze_code_complexity(
    top: int = 20,
    min_complexity: int = 0,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Rank methods by cyclomatic complexity.

    Reads ``coalesce(m.complexity, m.mccabe)`` (Python + Java) and ranks methods
    in descending complexity. When no method carries a complexity value the
    result is marked ``metric_source="approx"`` so a missing-enrichment graph is
    never reported as an all-null success (SC-MCP-007).

    Args:
        top: Maximum number of methods to return.
        min_complexity: Minimum complexity threshold to include a method.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"methods": [...], "count": int, "metric_source": "exact"|"approx",
        "warning"?: str}``. On failure returns ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND m.project_id = $project_id" if project_id else ""
        query = f"""
            MATCH (m:Method)
            WHERE coalesce(m.complexity, m.mccabe) IS NOT NULL{proj}
            RETURN m.id AS id, m.fqn AS fqn, m.file_path AS file_path,
                   m.line AS line,
                   coalesce(m.complexity, m.mccabe) AS complexity,
                   count {{ (m)-[:CALLS]->() }} AS fan_out,
                   count {{ ()-[:CALLS]->(m) }} AS fan_in
            ORDER BY complexity DESC
            LIMIT $top
        """
        rows = adapter.query(query, project_id=project_id, top=top)

        methods: list[dict[str, Any]] = []
        any_complexity = False
        for row in rows:
            complexity = row.get("complexity")
            if complexity is None:
                complexity = row.get("mccabe")
            if complexity is not None:
                any_complexity = True
            if complexity is not None and complexity < min_complexity:
                continue
            methods.append(
                {
                    "id": row.get("id") or row.get("node_id"),
                    "fqn": row.get("fqn"),
                    "file_path": row.get("file_path"),
                    "line": row.get("line"),
                    "complexity": complexity,
                    "fan_in": row.get("fan_in"),
                    "fan_out": row.get("fan_out"),
                }
            )

        methods.sort(
            key=lambda m: m["complexity"] if m["complexity"] is not None else -1,
            reverse=True,
        )
        methods = methods[:top]

        result: dict[str, Any] = {
            "methods": methods,
            "count": len(methods),
            "metric_source": "exact" if any_complexity else "approx",
        }
        if not any_complexity:
            result["warning"] = "no complexity/mccabe enrichment found; ranking is approximate"
        if not methods:
            result["coverage"] = "empty"
        return result
    except Exception as exc:
        logger.exception("analyze_code_complexity failed")
        return {"error": str(exc)}


def find_dead_code(
    exclude_entrypoints: bool = True,
    limit: int = 100,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Find methods with no incoming call edges (candidate dead code).

    Args:
        exclude_entrypoints: When ``True``, skip framework entry points
            (``main``, controller handlers, test methods).
        limit: Maximum number of methods to return.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"dead_code": [...], "count": int}`` where each entry carries ``id``,
        ``fqn``, ``file_path`` and ``reason``. On failure returns
        ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND m.project_id = $project_id" if project_id else ""
        entry = ""
        if exclude_entrypoints:
            entry = (
                " AND NOT m.name IN ['main', 'execute'] AND coalesce(m.role, '') <> 'Controller'"
            )
        query = f"""
            MATCH (m:Method)
            WHERE NOT ( ()-[:CALLS]->(m) ){proj}{entry}
            RETURN m.id AS id, m.fqn AS fqn, m.file_path AS file_path,
                   'no incoming CALLS' AS reason
            LIMIT $limit
        """
        rows = adapter.query(query, project_id=project_id, limit=limit)

        dead: list[dict[str, Any]] = [
            {
                "id": row.get("id") or row.get("node_id"),
                "fqn": row.get("fqn"),
                "file_path": row.get("file_path"),
                "reason": row.get("reason", "no incoming CALLS"),
            }
            for row in rows
        ]
        result: dict[str, Any] = {"dead_code": dead, "count": len(dead)}
        if not dead:
            result["coverage"] = "empty"
            result["warning"] = "no dead-code candidates found"
        return result
    except Exception as exc:
        logger.exception("find_dead_code failed")
        return {"error": str(exc)}


def analyze_change_impact(
    node_id: str,
    max_depth: int = 5,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Compute reverse reachability (blast radius) for a changed node.

    Walks ``CALLS``/``DEPENDS_ON`` edges backwards from ``node_id`` up to
    ``max_depth`` hops and groups impacted nodes by distance.

    Args:
        node_id: ID of the node being changed.
        max_depth: Maximum reverse-reachability depth.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"target": node_id, "impacted": [...], "total": int,
        "by_distance": {...}}``. On failure returns ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND caller.project_id = $project_id" if project_id else ""
        depth = max(1, min(int(max_depth), 50))
        query = f"""
            MATCH path = (caller:Node)-[:CALLS|DEPENDS_ON*1..{depth}]->(target:Node)
            WHERE target.id = $node_id{proj}
            RETURN DISTINCT caller.id AS id, caller.fqn AS fqn,
                   caller.file_path AS file_path, caller.layer AS layer,
                   length(path) AS distance
            ORDER BY distance
        """
        rows = adapter.query(query, project_id=project_id, node_id=node_id)

        impacted: list[dict[str, Any]] = []
        by_distance: dict[str, list[Any]] = {}
        by_layer: dict[str, list[Any]] = {}
        for row in rows:
            ident = row.get("id") or row.get("node_id")
            distance = row.get("distance")
            impacted.append(
                {
                    "id": ident,
                    "fqn": row.get("fqn"),
                    "file_path": row.get("file_path"),
                    "layer": row.get("layer"),
                    "distance": distance,
                }
            )
            by_distance.setdefault(str(distance), []).append(ident)
            layer = row.get("layer") or "unknown"
            by_layer.setdefault(str(layer), []).append(ident)

        result: dict[str, Any] = {
            "target": node_id,
            "impacted": impacted,
            "total": len(impacted),
            "by_distance": by_distance,
            "by_layer": by_layer,
        }
        if not impacted:
            result["coverage"] = "empty"
            result["warning"] = "no reverse-reachability impact found"
        return result
    except Exception as exc:
        logger.exception("analyze_change_impact failed")
        return {"error": str(exc)}


def find_similar_code(
    node_id: str,
    threshold: float = 0.7,
    limit: int = 10,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Find structurally similar methods using a structural fingerprint.

    The similarity is a deterministic structural heuristic (called-name set,
    parameter-type sequence and control-flow node-type histogram); no token-level
    diffing or vectors are used.

    Args:
        node_id: ID of the reference method.
        threshold: Minimum similarity score (0..1) to include a match.
        limit: Maximum number of matches to return.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"target": node_id, "matches": [...], "count": int}`` where each match
        carries ``id``, ``fqn`` and ``similarity``. On failure returns
        ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND cand.project_id = $project_id" if project_id else ""
        target_rows = adapter.query(
            """
            MATCH (m:Method)
            WHERE m.id = $node_id
            RETURN m.name AS name, m.role AS role, m.layer AS layer,
                   m.file_path AS file_path, m.signature AS signature
            """,
            project_id=project_id,
            node_id=node_id,
        )
        if not target_rows:
            return {
                "target": node_id,
                "matches": [],
                "count": 0,
                "coverage": "empty",
                "warning": "target node not found",
            }

        target = target_rows[0]
        query = f"""
            MATCH (cand:Method)
            WHERE cand.id <> $node_id{proj}
              AND (
                    ($target_role IS NOT NULL AND cand.role = $target_role)
                 OR ($target_layer IS NOT NULL AND cand.layer = $target_layer)
                 OR ($target_file_path IS NOT NULL AND cand.file_path = $target_file_path)
              )
            WITH cand,
                 CASE
                     WHEN cand.name IS NOT NULL AND cand.name = $target_name THEN 0.5
                     ELSE 0
                 END +
                 CASE
                     WHEN coalesce(cand.role, '') = coalesce($target_role, '') THEN 0.2
                     ELSE 0
                 END +
                 CASE
                     WHEN coalesce(cand.layer, '') = coalesce($target_layer, '') THEN 0.1
                     ELSE 0
                 END +
                 CASE
                     WHEN coalesce(cand.signature, '') = coalesce($target_signature, '') THEN 0.2
                     ELSE 0
                 END AS similarity
            RETURN cand.id AS id, cand.fqn AS fqn, cand.file_path AS file_path,
                   similarity AS similarity
            ORDER BY similarity DESC
            LIMIT $limit
        """
        rows = adapter.query(
            query,
            project_id=project_id,
            node_id=node_id,
            limit=limit,
            target_name=target.get("name"),
            target_role=target.get("role"),
            target_layer=target.get("layer"),
            target_file_path=target.get("file_path"),
            target_signature=target.get("signature"),
        )

        matches: list[dict[str, Any]] = []
        for row in rows:
            similarity = row.get("similarity")
            if similarity is None:
                similarity = row.get("score")
            if similarity is not None and similarity < threshold:
                continue
            matches.append(
                {
                    "id": row.get("id") or row.get("node_id"),
                    "fqn": row.get("fqn"),
                    "file_path": row.get("file_path"),
                    "similarity": similarity,
                }
            )
        result: dict[str, Any] = {
            "target": node_id,
            "matches": matches[:limit],
            "count": len(matches),
        }
        if not matches:
            result["coverage"] = "empty"
            result["warning"] = "no similar-code candidates found"
        return result
    except Exception as exc:
        logger.exception("find_similar_code failed")
        return {"error": str(exc)}


def get_architecture_metrics(project_id: str | None = None) -> dict[str, Any]:
    """Aggregate architectural metrics by layer and role.

    Computes per-layer / per-role node counts and detects layering violations
    (e.g. ``data`` -> ``web`` reverse dependencies). When the graph carries no
    role/layer enrichment a ``coverage``/``warning`` marker is returned instead
    of an all-null shell (SC-MCP-007).

    Args:
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"layers": {...}, "role_counts": {...}, "layering_violations": [...],
        "coverage"?: str, "warning"?: str}``. On failure returns
        ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND n.project_id = $project_id" if project_id else ""
        agg_query = f"""
            MATCH (n:Node)
            WHERE (n.layer IS NOT NULL OR n.role IS NOT NULL){proj}
            RETURN n.layer AS layer, n.role AS role, count(n) AS count
        """
        rows = adapter.query(agg_query, project_id=project_id)

        if not rows:
            return {
                "layers": {},
                "role_counts": {},
                "layering_violations": [],
                "coverage": "empty",
                "warning": (
                    "no role/layer enrichment found; run graph enrichment to "
                    "populate architectural metrics"
                ),
            }

        layers: dict[str, int] = {}
        role_counts: dict[str, int] = {}
        for row in rows:
            count = int(row.get("count") or 0)
            layer = row.get("layer")
            role = row.get("role")
            if layer:
                layers[str(layer)] = layers.get(str(layer), 0) + count
            if role:
                role_counts[str(role)] = role_counts.get(str(role), 0) + count

        viol_proj = " AND a.project_id = $project_id" if project_id else ""
        viol_query = f"""
            MATCH (a:Node)-[:DEPENDS_ON]->(b:Node)
            WHERE a.layer = 'data' AND b.layer = 'web'{viol_proj}
            RETURN a.id AS source_id, b.id AS target_id,
                   a.layer AS source_layer, b.layer AS target_layer
            LIMIT 100
        """
        viol_rows = adapter.query(viol_query, project_id=project_id)
        violations: list[dict[str, Any]] = [
            {
                "source": row.get("source_id"),
                "target": row.get("target_id"),
                "source_layer": row.get("source_layer"),
                "target_layer": row.get("target_layer"),
            }
            for row in viol_rows
            if row.get("source_layer") and row.get("target_layer")
        ]

        return {
            "layers": layers,
            "role_counts": role_counts,
            "layering_violations": violations,
            "coverage": "ok",
        }
    except Exception as exc:
        logger.exception("get_architecture_metrics failed")
        return {"error": str(exc)}
