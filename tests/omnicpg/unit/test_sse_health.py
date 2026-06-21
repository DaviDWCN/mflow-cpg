"""Unit tests for the /health endpoint of sse_transport."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_fake_adapter(*, connected: bool) -> Any:
    """Return a minimal adapter stub with is_connected() and query() methods."""
    adapter = MagicMock()
    adapter.is_connected.return_value = connected
    adapter.query.return_value = [{"c": 5}] if connected else []
    return adapter


class TestHealthHandler:
    """Tests for handle_health in mcp_server_omnicpg.sse_transport."""

    def test_health_ok_when_connected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Health endpoint reports status=ok and neo4j=True when adapter is connected."""
        import mcp_server_omnicpg.sse_transport as transport_mod

        monkeypatch.setattr(transport_mod, "adapter", _make_fake_adapter(connected=True))

        from mcp_server_omnicpg.sse_transport import handle_health

        response = asyncio.run(handle_health(MagicMock()))
        body = json.loads(response.body)

        assert body["status"] == "ok"
        assert body["neo4j"] is True
        assert isinstance(body["tools"], int) and body["tools"] > 0

    def test_health_neo4j_false_when_disconnected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Health endpoint reports neo4j=False when adapter is not connected."""
        import mcp_server_omnicpg.sse_transport as transport_mod

        monkeypatch.setattr(transport_mod, "adapter", _make_fake_adapter(connected=False))

        from mcp_server_omnicpg.sse_transport import handle_health

        response = asyncio.run(handle_health(MagicMock()))
        body = json.loads(response.body)

        assert body["status"] == "ok"
        assert body["neo4j"] is False

    def test_health_response_schema(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Health response contains exactly the expected top-level keys."""
        import mcp_server_omnicpg.sse_transport as transport_mod

        monkeypatch.setattr(transport_mod, "adapter", _make_fake_adapter(connected=True))

        from mcp_server_omnicpg.sse_transport import handle_health

        response = asyncio.run(handle_health(MagicMock()))
        body = json.loads(response.body)

        assert set(body.keys()) == {"status", "neo4j", "has_data", "tools"}

    def test_health_status_code_is_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Health endpoint always returns HTTP 200."""
        import mcp_server_omnicpg.sse_transport as transport_mod

        monkeypatch.setattr(transport_mod, "adapter", _make_fake_adapter(connected=False))

        from mcp_server_omnicpg.sse_transport import handle_health

        response = asyncio.run(handle_health(MagicMock()))

        assert response.status_code == 200
