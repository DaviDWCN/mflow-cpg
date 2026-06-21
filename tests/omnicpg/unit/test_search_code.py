"""Unit tests for the search_code full-text MCP tool."""

from __future__ import annotations

from typing import Any

import mcp_server_omnicpg.tools.basic_queries as basic_queries
import pytest
from mcp_server_omnicpg.tools.basic_queries import search_code


class FakeAdapter:
    """Captures the query/params and returns canned full-text rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Store canned rows to return from query."""
        self._rows = rows
        self.last_query: str | None = None
        self.last_params: dict[str, Any] = {}

    def ensure_connected(self) -> None:
        """No-op connection check."""
        return None

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """Record the query and params, return canned rows."""
        self.last_query = cypher
        self.last_params = params
        return self._rows


@pytest.fixture
def fake_rows() -> list[dict[str, Any]]:
    """Canned full-text result rows."""
    return [
        {
            "id": "m1",
            "label": "Method",
            "name": "transfer",
            "fqn": "com.app.AccountService.transfer",
            "file_path": "AccountService.java",
            "line_start": 42,
            "source_code": "x" * 800,
            "score": 3.5,
        }
    ]


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: FakeAdapter) -> None:
    """Patch get_adapter to return the fake adapter."""
    monkeypatch.setattr(basic_queries, "get_adapter", lambda: adapter)


def test_search_code_formats_and_truncates(
    monkeypatch: pytest.MonkeyPatch, fake_rows: list[dict[str, Any]]
) -> None:
    """Results are formatted and source_code preview is truncated to 500."""
    adapter = FakeAdapter(fake_rows)
    _patch_adapter(monkeypatch, adapter)

    results = search_code("transfer")

    assert len(results) == 1
    row = results[0]
    assert row["id"] == "m1"
    assert row["label"] == "Method"
    assert row["score"] == 3.5
    assert len(row["code_preview"]) == 500
    assert "source_code" not in row


def test_search_code_passes_project_and_label_filters(
    monkeypatch: pytest.MonkeyPatch, fake_rows: list[dict[str, Any]]
) -> None:
    """project_id and label filters are injected into query and params."""
    adapter = FakeAdapter(fake_rows)
    _patch_adapter(monkeypatch, adapter)

    search_code("transfer", label="Method", project_id="proj-1", limit=5)

    assert adapter.last_params["keyword"] == "transfer"
    assert adapter.last_params["limit"] == 5
    assert adapter.last_params["project_id"] == "proj-1"
    assert adapter.last_params["label"] == "Method"
    assert "node.project_id = $project_id" in adapter.last_query
    assert "$label IN labels(node)" in adapter.last_query


def test_search_code_handles_missing_source_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing source_code yields a None code_preview."""
    adapter = FakeAdapter([{"id": "c1", "label": "Class", "source_code": None}])
    _patch_adapter(monkeypatch, adapter)

    results = search_code("Account")
    assert results[0]["code_preview"] is None


def test_search_code_empty_keyword_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty keyword raises ValueError."""
    _patch_adapter(monkeypatch, FakeAdapter([]))
    with pytest.raises(ValueError, match="keyword must not be empty"):
        search_code("   ")


def test_search_code_invalid_limit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limit < 1 raises ValueError."""
    _patch_adapter(monkeypatch, FakeAdapter([]))
    with pytest.raises(ValueError, match="limit must be at least 1"):
        search_code("transfer", limit=0)
