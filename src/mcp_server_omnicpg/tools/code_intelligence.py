"""Code intelligence MCP tools for OmniCPG.

This module implements the six *code intelligence* tools introduced by the
``mcp-advanced-tools`` openspec change. Each tool is deterministic (no LLM
calls, no network, no extra dependencies — REQ-MCP-007), scopes every Cypher
query to the configured ``project_id`` (REQ-SCHEMA-006), and returns a
JSON-friendly structure (or ``{"error": ...}`` on failure rather than raising).

Tools:
    * ``get_code_context``
    * ``semantic_search``
    * ``suggest_refactoring``
    * ``explain_code``
    * ``trace_variable``         (max_depth capped at <= 50)
    * ``get_test_coverage_info`` (SC-MCP-007 coverage marker)
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server_omnicpg.neo4j_adapter import get_adapter

logger = logging.getLogger(__name__)

_MAX_TRACE_DEPTH = 50


def get_code_context(
    node_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Return a compact context card for a node plus its 1-hop neighbours.

    Collects the node body (signature/fqn/location), its direct callers and
    callees so an LLM consumer can reason about the node without further calls.

    Args:
        node_id: ID of the node to describe.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"node": {...}, "callers": [...], "callees": [...]}``. On failure
        returns ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND n.project_id = $project_id" if project_id else ""
        query = f"""
            MATCH (n:Node) WHERE n.id = $node_id{proj}
            OPTIONAL MATCH (caller:Node)-[:CALLS]->(n)
            OPTIONAL MATCH (n)-[:CALLS]->(callee:Node)
            RETURN n.id AS id, n.name AS name, n.fqn AS fqn,
                   n.signature AS signature, n.file_path AS file_path,
                   n.line AS line, n.role AS role, n.layer AS layer,
                   collect(DISTINCT caller.fqn) AS callers,
                   collect(DISTINCT callee.fqn) AS callees
        """
        rows = adapter.query(query, project_id=project_id, node_id=node_id)

        if not rows:
            return {"node": None, "callers": [], "callees": [], "coverage": "empty"}

        row = rows[0]
        node = {
            "id": row.get("id") or row.get("node_id") or node_id,
            "name": row.get("name"),
            "fqn": row.get("fqn"),
            "signature": row.get("signature"),
            "file_path": row.get("file_path"),
            "line": row.get("line"),
            "role": row.get("role"),
            "layer": row.get("layer"),
        }
        callers = row.get("callers")
        callees = row.get("callees")
        if not isinstance(callers, list):
            callers = [r.get("caller") for r in rows if r.get("caller")]
        if not isinstance(callees, list):
            callees = [r.get("callee") for r in rows if r.get("callee")]
        return {"node": node, "callers": callers, "callees": callees}
    except Exception as exc:
        logger.exception("get_code_context failed")
        return {"error": str(exc)}


def semantic_search(
    intent: str,
    label: str | None = None,
    limit: int = 10,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Search code entities by natural-language intent over the full-text index.

    Rewrites ``intent`` into keywords and queries the ``code_fulltext`` index,
    optionally filtered by ``label``. No vectors are introduced. When nothing
    matches an explicit ``coverage`` marker is returned (never a null shell).

    Args:
        intent: Natural-language description of what to find.
        label: Optional label filter (e.g. ``"Method"``, ``"Class"``).
        limit: Maximum number of hits to return.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"results": [...], "count": int, "coverage"?: str}`` where each hit
        carries ``id``, ``fqn`` and ``score``. On failure returns
        ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND node.project_id = $project_id" if project_id else ""
        label_filter = " AND label IN labels(node)" if label else ""
        query = f"""
            CALL db.index.fulltext.queryNodes('code_fulltext', $intent)
            YIELD node, score
            WHERE true{proj}{label_filter}
            RETURN node.id AS id, node.fqn AS fqn, node.name AS name,
                   node.file_path AS file_path, score AS score
            ORDER BY score DESC
            LIMIT $limit
        """
        rows = adapter.query(
            query,
            project_id=project_id,
            intent=intent,
            label=label,
            limit=limit,
        )

        results: list[dict[str, Any]] = [
            {
                "id": row.get("id") or row.get("node_id"),
                "fqn": row.get("fqn"),
                "name": row.get("name"),
                "file_path": row.get("file_path"),
                "score": row.get("score"),
            }
            for row in rows
        ]
        if not results:
            fallback_query = f"""
                MATCH (node:Node)
                WHERE toLower(coalesce(node.fqn, node.name, node.type, node.role, node.layer, ''))
                      CONTAINS toLower($intent){proj}{label_filter}
                RETURN node.id AS id, node.fqn AS fqn, node.name AS name,
                       node.file_path AS file_path, 0.5 AS score
                ORDER BY score DESC
                LIMIT $limit
            """
            rows = adapter.query(
                fallback_query,
                project_id=project_id,
                intent=intent,
                label=label,
                limit=limit,
            )
            results = [
                {
                    "id": row.get("id") or row.get("node_id"),
                    "fqn": row.get("fqn"),
                    "name": row.get("name"),
                    "file_path": row.get("file_path"),
                    "score": row.get("score"),
                }
                for row in rows
            ]
        if not results:
            return {
                "results": [],
                "count": 0,
                "coverage": "empty",
                "warning": f"no full-text matches for intent {intent!r}",
            }
        return {"results": results, "count": len(results)}
    except Exception as exc:
        logger.exception("semantic_search failed")
        return {"error": str(exc)}


def suggest_refactoring(
    node_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Produce rule-based refactoring suggestions for a node.

    Combines complexity (extract method), duplication (dedupe) and coupling
    (reduce coupling) signals into a typed suggestion list. No code is changed.

    Args:
        node_id: ID of the node to analyse.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"target": node_id, "suggestions": [{"kind", "evidence", "target"}]}``.
        On failure returns ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND m.project_id = $project_id" if project_id else ""
        query = f"""
            MATCH (m:Node) WHERE m.id = $node_id{proj}
            RETURN m.id AS id, m.fqn AS fqn,
                   coalesce(m.complexity, m.mccabe) AS complexity,
                   count {{ (m)-[:CALLS]->() }} AS fan_out,
                   count {{ ()-[:CALLS]->(m) }} AS fan_in
        """
        rows = adapter.query(query, project_id=project_id, node_id=node_id)

        suggestions: list[dict[str, Any]] = []
        for row in rows:
            target = row.get("id") or row.get("node_id") or node_id
            complexity = row.get("complexity")
            fan_in = row.get("fan_in")
            fan_out = row.get("fan_out")
            if complexity is not None and complexity >= 10:
                suggestions.append(
                    {
                        "kind": "extract_method",
                        "evidence": f"high cyclomatic complexity ({complexity})",
                        "target": target,
                    }
                )
            if fan_out is not None and fan_out >= 5:
                suggestions.append(
                    {
                        "kind": "reduce_coupling",
                        "evidence": f"high fan-out ({fan_out})",
                        "target": target,
                    }
                )
            if fan_in is not None and fan_in >= 10:
                suggestions.append(
                    {
                        "kind": "dedupe",
                        "evidence": f"high fan-in ({fan_in}); candidate shared helper",
                        "target": target,
                    }
                )

        result: dict[str, Any] = {
            "target": node_id,
            "suggestions": suggestions,
            "count": len(suggestions),
        }
        if not suggestions:
            result["coverage"] = "empty"
            result["warning"] = "no rule-based refactoring signals found"
        return result
    except Exception as exc:
        logger.exception("suggest_refactoring failed")
        return {"error": str(exc)}


def explain_code(
    node_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Return a structured fact card explaining a node.

    Rule-based summary (what it is, what it calls, who calls it, its role/layer)
    built from the graph — no external model is invoked.

    Args:
        node_id: ID of the node to explain.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        A structured dict describing the node. On failure returns
        ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj = " AND n.project_id = $project_id" if project_id else ""
        query = f"""
            MATCH (n:Node) WHERE n.id = $node_id{proj}
            OPTIONAL MATCH (caller:Node)-[:CALLS]->(n)
            OPTIONAL MATCH (n)-[:CALLS]->(callee:Node)
            RETURN n.id AS id, n.name AS name, n.fqn AS fqn,
                   n.signature AS signature, n.type AS type,
                   n.file_path AS file_path, n.line AS line,
                   n.role AS role, n.layer AS layer,
                   collect(DISTINCT caller.fqn) AS callers,
                   collect(DISTINCT callee.fqn) AS callees
        """
        rows = adapter.query(query, project_id=project_id, node_id=node_id)

        if not rows:
            return {
                "node_id": node_id,
                "summary": "node not found",
                "coverage": "empty",
            }

        row = rows[0]
        callers = row.get("callers")
        callees = row.get("callees")
        callers = callers if isinstance(callers, list) else []
        callees = callees if isinstance(callees, list) else []
        name = row.get("name") or row.get("fqn") or node_id
        return {
            "node_id": row.get("id") or node_id,
            "name": name,
            "fqn": row.get("fqn"),
            "signature": row.get("signature"),
            "type": row.get("type"),
            "role": row.get("role"),
            "layer": row.get("layer"),
            "file_path": row.get("file_path"),
            "line": row.get("line"),
            "calls": callees,
            "called_by": callers,
            "summary": (
                f"{name} is a {row.get('type') or 'node'} "
                f"(role={row.get('role')}, layer={row.get('layer')}) "
                f"that calls {len(callees)} and is called by {len(callers)} nodes."
            ),
        }
    except Exception as exc:
        logger.exception("explain_code failed")
        return {"error": str(exc)}


def trace_variable(
    node_id: str,
    direction: str = "forward",
    max_depth: int = 10,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Trace a variable's data flow along ``REACHES``/``FLOWS_TO`` edges.

    Args:
        node_id: ID of the variable/parameter node to trace from.
        direction: ``"forward"`` (downstream) or ``"backward"`` (upstream).
        max_depth: Maximum traversal depth. Must be ``<= 50``.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"target": node_id, "direction": str, "trace": [...]}`` carrying the
        ordered flow nodes with their ``interprocedural``/``variable`` evidence.
        On failure returns ``{"error": str}``.

    Raises:
        ValueError: If ``max_depth`` exceeds 50 (the trace cap).
    """
    if max_depth > _MAX_TRACE_DEPTH:
        raise ValueError(f"max_depth must be <= {_MAX_TRACE_DEPTH}, got {max_depth}")
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        depth = max(1, min(int(max_depth), _MAX_TRACE_DEPTH))
        proj = " AND n.project_id = $project_id" if project_id else ""
        if direction == "backward":
            pattern = f"(n:Node)-[:REACHES|FLOWS_TO*1..{depth}]->(start:Node)"
        else:
            pattern = f"(start:Node)-[:REACHES|FLOWS_TO*1..{depth}]->(n:Node)"
        query = f"""
            MATCH (start:Node) WHERE start.id = $node_id
            MATCH path = {pattern}
            WHERE true{proj}
            RETURN n.id AS id, n.fqn AS fqn, n.file_path AS file_path,
                   n.line AS line, n.variable AS variable,
                   n.interprocedural AS interprocedural,
                   length(path) AS distance
            ORDER BY distance
        """
        rows = adapter.query(query, project_id=project_id, node_id=node_id, max_depth=depth)

        trace: list[dict[str, Any]] = [
            {
                "id": row.get("id") or row.get("node_id"),
                "fqn": row.get("fqn"),
                "file_path": row.get("file_path"),
                "line": row.get("line"),
                "variable": row.get("variable"),
                "interprocedural": row.get("interprocedural"),
                "distance": row.get("distance"),
            }
            for row in rows
        ]
        result: dict[str, Any] = {
            "target": node_id,
            "direction": direction,
            "trace": trace,
            "count": len(trace),
        }
        if not trace:
            result["coverage"] = "empty"
            result["warning"] = "no REACHES/FLOWS_TO trace found"
        return result
    except Exception as exc:
        logger.exception("trace_variable failed")
        return {"error": str(exc)}


def get_test_coverage_info(
    node_id: str | None = None,
    file_path: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Report whether a node/file is covered by tests via ``TESTS`` edges.

    Args:
        node_id: ID of the node to check (mutually usable with ``file_path``).
        file_path: File path to check coverage for.
        project_id: Project ID to scope the query to (REQ-SCHEMA-006).

    Returns:
        ``{"covered": bool, "tests": [...], "coverage": str, "warning"?: str}``.
        When no ``TESTS`` edge exists, ``covered`` is ``False`` and a
        ``coverage`` marker is included (SC-MCP-007). On failure returns
        ``{"error": str}``.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        conditions: list[str] = []
        if node_id:
            conditions.append("target.id = $node_id")
        if file_path:
            conditions.append("target.file_path = $file_path")
        if project_id:
            conditions.append("target.project_id = $project_id")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            MATCH (test:Node)-[:TESTS]->(target:Node)
            {where}
            RETURN target.id AS id, target.fqn AS fqn,
                   test.id AS test_id, test.fqn AS test_fqn
        """
        rows = adapter.query(query, project_id=project_id, node_id=node_id, file_path=file_path)

        tests: list[dict[str, Any]] = [
            {
                "test_id": row.get("test_id"),
                "test_fqn": row.get("test_fqn"),
                "target_id": row.get("id") or row.get("node_id"),
            }
            for row in rows
            if row.get("test_id") is not None or row.get("test_fqn") is not None
        ]

        if not tests:
            return {
                "covered": False,
                "tests": [],
                "coverage": "uncovered",
                "warning": ("no TESTS edges found; node/file appears to lack test coverage"),
            }
        return {"covered": True, "tests": tests, "coverage": "covered"}
    except Exception as exc:
        logger.exception("get_test_coverage_info failed")
        return {"error": str(exc)}
