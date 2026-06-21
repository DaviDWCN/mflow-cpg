"""Helper tools for OmniCPG MCP Server.

Provides:
- list_projects: Lists all analyzed projects.
- get_node_source_code: Retrieves source code snippets of a node.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server_omnicpg.neo4j_adapter import get_adapter

logger = logging.getLogger(__name__)


def list_projects() -> list[dict[str, Any]]:
    """List all projects currently in the Neo4j database with their metadata.

    Returns:
        A list of project details.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        query = """
            MATCH (n:Node)
            WHERE n.project_id IS NOT NULL
            RETURN n.project_id AS project_id,
                   count(n) AS total_nodes,
                   count(DISTINCT n.file_path) AS total_files,
                   collect(DISTINCT n.language) AS languages
        """
        rows = adapter.query(query)
        results = []
        for r in rows:
            languages = [lang for lang in r.get("languages", []) if lang]
            results.append(
                {
                    "project_id": r.get("project_id"),
                    "total_nodes": r.get("total_nodes", 0),
                    "total_files": r.get("total_files", 0),
                    "languages": sorted(list(set(languages))),
                }
            )
        return results
    except Exception as exc:
        logger.exception("list_projects failed")
        return [{"error": str(exc)}]


def get_node_source_code(node_id: str, project_id: str | None = None) -> dict[str, Any]:
    """Retrieve the source code snippet of a node by its ID.

    Args:
        node_id: The unique ID of the node.
        project_id: Optional project scope override.

    Returns:
        A dictionary containing source code, file path, and start line information.
    """
    try:
        adapter = get_adapter()
        adapter.ensure_connected()

        proj_where = " AND n.project_id = $project_id" if project_id else ""
        query = f"""
            MATCH (n:Node {{id: $node_id}})
            WHERE true{proj_where}
            RETURN n.id AS id,
                   n.name AS name,
                   labels(n) AS labels,
                   n.file_path AS file_path,
                   n.line AS line,
                   n.line_start AS line_start,
                   n.code AS code,
                   n.source_code AS source_code
        """
        rows = adapter.query(query, node_id=node_id, project_id=project_id)
        if not rows:
            return {"error": f"Node with ID '{node_id}' not found."}

        row = rows[0]
        code = row.get("source_code") or row.get("code")
        if not code:
            return {
                "error": f"Node '{node_id}' has no source code properties.",
                "file_path": row.get("file_path"),
            }

        start_line = row.get("line") or row.get("line_start") or 1
        lines = code.splitlines()
        formatted_lines = [f"{int(start_line) + idx}: {line}" for idx, line in enumerate(lines)]

        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "labels": [lbl for lbl in row.get("labels", []) if lbl != "Node"],
            "file_path": row.get("file_path"),
            "start_line": start_line,
            "source_code": code,
            "formatted_code": "\n".join(formatted_lines),
        }
    except Exception as exc:
        logger.exception("get_node_source_code failed")
        return {"error": str(exc)}
