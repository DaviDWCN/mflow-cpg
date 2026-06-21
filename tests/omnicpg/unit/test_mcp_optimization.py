"""Unit tests for MCP Server optimizations and new helper tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp_server_omnicpg.mcp_server as server
import mcp_server_omnicpg.neo4j_adapter as adapter_mod
import pytest
from mcp_server_omnicpg.config import Config


class _AdapterStub:
    """Small adapter stub for testing query results."""

    def __init__(
        self,
        project_rows: list[dict[str, Any]] | None = None,
        node_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.project_rows = project_rows or []
        self.node_rows = node_rows or []

    def ensure_connected(self) -> None:
        """No-op."""

    def query(self, query_str: str, **_kwargs: Any) -> list[dict[str, Any]]:
        """Return configured rows depending on the query type."""
        # Simple heuristics for routing
        if "project_id" in query_str and "COUNT" in query_str:
            return self.project_rows
        if "id: $node_id" in query_str or "n.id = $node_id" in query_str:
            return self.node_rows
        return self.project_rows

    def is_connected(self) -> bool:
        return True


def _install(
    monkeypatch: pytest.MonkeyPatch,
    project_rows: list[dict[str, Any]] | None = None,
    node_rows: list[dict[str, Any]] | None = None,
) -> _AdapterStub:
    fake = _AdapterStub(project_rows, node_rows)
    monkeypatch.setattr(adapter_mod, "_adapter", fake)
    monkeypatch.setattr(server, "adapter", fake)
    return fake


def test_list_projects(monkeypatch) -> None:
    """Verify list_projects returns project stats correctly."""
    _install(
        monkeypatch,
        project_rows=[
            {
                "project_id": "proj-test-1",
                "total_nodes": 100,
                "total_files": 5,
                "languages": ["python", "java"],
            }
        ],
    )

    result = asyncio.run(server.call_tool("list_projects", {}))
    payload = json.loads(result[0].text)

    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["project_id"] == "proj-test-1"
    assert payload[0]["total_nodes"] == 100
    assert payload[0]["total_files"] == 5
    assert payload[0]["languages"] == ["java", "python"]


def test_get_node_source_code_found(monkeypatch) -> None:
    """Verify get_node_source_code returns source code formatted with lines."""
    _install(
        monkeypatch,
        node_rows=[
            {
                "id": "abc123abc123abc123abc123abc123ab",
                "name": "my_method",
                "labels": ["Method", "Node"],
                "file_path": "src/main.py",
                "line": 10,
                "source_code": "def my_method():\n    print('hello')\n    return 42",
            }
        ],
    )

    result = asyncio.run(
        server.call_tool(
            "get_node_source_code",
            {"node_id": "abc123abc123abc123abc123abc123ab", "project_id": "proj-test-1"},
        )
    )
    payload = json.loads(result[0].text)

    assert "error" not in payload
    assert payload["id"] == "abc123abc123abc123abc123abc123ab"
    assert payload["name"] == "my_method"
    assert payload["labels"] == ["Method"]
    assert payload["file_path"] == "src/main.py"
    assert payload["start_line"] == 10
    assert "10: def my_method():" in payload["formatted_code"]
    assert "11:     print('hello')" in payload["formatted_code"]
    assert "12:     return 42" in payload["formatted_code"]


def test_get_node_source_code_not_found(monkeypatch) -> None:
    """Verify get_node_source_code returns an error when node is not found."""
    _install(monkeypatch, node_rows=[])

    result = asyncio.run(
        server.call_tool(
            "get_node_source_code",
            {"node_id": "abc123abc123abc123abc123abc123ab"},
        )
    )
    payload = json.loads(result[0].text)

    assert "error" in payload
    assert "not found" in payload["error"]


def test_smart_validation_invalid_id_format(monkeypatch) -> None:
    """Verify passing an invalid ID (e.g. human-readable name) raises format error."""
    _install(monkeypatch)
    monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

    result = asyncio.run(
        server.call_tool(
            "get_node_by_id",
            {"node_id": "my_human_readable_method_name"},
        )
    )
    payload = json.loads(result[0].text)

    assert "error" in payload
    assert "Invalid format for parameter 'node_id'" in payload["error"]
    assert "Expected a 32-character hex ID hash" in payload["error"]


def test_smart_validation_valid_id_format(monkeypatch) -> None:
    """Verify passing a valid 32-character hex ID hash passes validation."""
    _install(
        monkeypatch,
        node_rows=[
            {
                "id": "abc123abc123abc123abc123abc123ab",
                "name": "my_method",
                "labels": ["Method"],
                "file_path": "src/main.py",
                "properties": {},
            }
        ],
    )
    monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

    result = asyncio.run(
        server.call_tool(
            "get_node_by_id",
            {"node_id": "abc123abc123abc123abc123abc123ab"},
        )
    )
    payload = json.loads(result[0].text)

    assert "error" not in payload
    assert payload["id"] == "abc123abc123abc123abc123abc123ab"
