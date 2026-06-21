"""Tests encoding the MCP correction plan (P0/P1) for OmniCPG.

These tests are written FIRST (TDD red phase). They are expected to FAIL against
the current implementation and to pass once a developer agent applies the
correction plan. They run WITHOUT a live Neo4j by injecting fake adapters.

Plan items covered here:
    * P1 project_id scoping  -> :class:`TestProjectIdScoping`
    * P1 dormant tool registration -> :class:`TestDormantToolRegistration`
    * P1 get_server_info tool -> :class:`TestGetServerInfoTool`

The P0 adapter unwrap bug is covered separately in
``tests/unit/test_adapter_unwrap.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import mcp_server_omnicpg.mcp_server as server
import mcp_server_omnicpg.neo4j_adapter as adapter_mod
import pytest
from mcp_server_omnicpg.config import Config


class _CapturingAdapter:
    """A fake adapter that records every parameter passed to ``query``.

    Used to assert that tools forward ``project_id`` (and other params) into the
    Cypher query layer. All query/write calls return empty result sets so the
    tools complete without a live Neo4j.
    """

    def __init__(self) -> None:
        """Initialise the captured-calls list."""
        self.calls: list[dict[str, Any]] = []

    def ensure_connected(self) -> None:
        """No-op: the fake is always 'connected'."""

    def connect(self) -> None:
        """No-op connect for parity with the real adapter."""

    def is_connected(self) -> bool:
        """Report the fake as connected."""
        return True

    def query(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Capture the parameters and return an empty result set."""
        self.calls.append(dict(params))
        return []

    def execute_write(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Capture write parameters and return an empty result set."""
        self.calls.append(dict(params))
        return []


def _install_fake_adapter(monkeypatch: pytest.MonkeyPatch) -> _CapturingAdapter:
    """Install a :class:`_CapturingAdapter` everywhere the server resolves one."""
    fake = _CapturingAdapter()
    # Tools call ``get_adapter()`` which returns the module global ``_adapter``.
    monkeypatch.setattr(adapter_mod, "_adapter", fake)
    # ``call_tool`` uses the module-level ``adapter`` captured at import time.
    monkeypatch.setattr(server, "adapter", fake)
    return fake


# Plan P1: the 8 tools that currently run UN-SCOPED (no project_id filter).
# Each entry is (tool_name, minimal valid arguments).
_UNSCOPED_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("get_node_by_id", {"node_id": "n1"}),
    ("find_path", {"start_node_id": "a", "end_node_id": "b"}),
    ("find_data_flow", {"source_node_id": "a", "target_node_id": "b"}),
    ("find_control_flow", {"start_node_id": "a", "end_node_id": "b"}),
    ("get_call_graph", {"function_name": "foo"}),
    ("get_dependencies", {"node_id": "n1"}),
    ("analyze_function", {"function_id": "f1"}),
    ("get_file_structure", {"file_path": "x.py"}),
]


class TestProjectIdScoping:
    """P1: every id/name-based tool must forward ``project_id`` to the adapter."""

    @pytest.mark.parametrize(("tool_name", "arguments"), _UNSCOPED_TOOLS)
    def test_tool_forwards_project_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        """P1: when ``Config.PROJECT_ID`` is set, the tool scopes its query.

        REQ-SCHEMA-006 requires every query to filter by ``project_id``. These 8
        tools currently never pass ``project_id`` to the adapter, so the
        assertion FAILS now. After the fix the configured project id must appear
        in at least one captured ``query`` call.
        """
        fake = _install_fake_adapter(monkeypatch)
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-test")

        asyncio.run(server.call_tool(tool_name, arguments))

        forwarded = [c.get("project_id") for c in fake.calls]
        assert "proj-test" in forwarded, (
            f"tool {tool_name!r} did not forward project_id; captured query params={fake.calls}"
        )


class TestDormantToolRegistration:
    """P1: fully implemented but unregistered tools must appear in list_tools()."""

    # Tools that are implemented under mcp_server_omnicpg/tools/ but NOT yet
    # surfaced via list_tools()/call_tool().
    _EXPECTED_DORMANT: ClassVar[set[str]] = {
        "find_callers_of",
        "find_callsite_method",
        "batch_callsite_methods",
        "apoc_shortest_path",
        "apoc_subgraph_around_node",
        "apoc_graph_schema",
        "apoc_meta_stats",
        "analyze_path",
        "find_data_flow_with_auto_expand",
        "find_control_flow_with_auto_expand",
        # Second wave: remaining implemented-but-dormant tools now registered.
        "apoc_expand_path",
        "apoc_spanning_tree",
        "apoc_run_read_query",
        "apoc_run_timeboxed_query",
        "expand_method_on_demand",
        "get_expansion_stats",
    }

    def test_dormant_tools_are_registered(self) -> None:
        """P1: the dormant tools must be present in ``list_tools()``.

        FAILS now because ``list_tools()`` returns only the original 11 tools.
        """
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}

        missing = self._EXPECTED_DORMANT - names
        assert not missing, f"dormant tools not registered: {sorted(missing)}"

    def test_tool_count_matches_registered(self) -> None:
        """P1: ``TOOL_COUNT`` must equal ``len(list_tools())``.

        Consistency guard: passes today (11 == 11) and must keep holding once
        the dormant tools and ``get_server_info`` are registered.
        """
        tools = asyncio.run(server.list_tools())
        assert len(tools) == server.TOOL_COUNT, (
            f"TOOL_COUNT={server.TOOL_COUNT} but list_tools() returned {len(tools)}"
        )


class TestGetServerInfoTool:
    """P1: a ``get_server_info`` tool must exist and report server status."""

    def test_get_server_info_is_registered(self) -> None:
        """P1: ``get_server_info`` must be listed as a tool.

        FAILS now because the tool does not exist.
        """
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        assert "get_server_info" in names

    def test_get_server_info_returns_status_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P1: calling ``get_server_info`` returns the mandated status keys.

        The spec (specs/AGENTS.md) mandates ``get_server_info`` as the first
        call, returning at least ``neo4j_connected``, ``has_data`` and
        ``tool_count``. FAILS now because ``call_tool`` replies with
        ``"Unknown tool: get_server_info"`` instead of a JSON payload.
        """
        _install_fake_adapter(monkeypatch)

        result = asyncio.run(server.call_tool("get_server_info", {}))
        text = result[0].text

        assert not text.startswith("Unknown tool"), (
            f"get_server_info is not dispatched by call_tool; got: {text!r}"
        )
        payload = json.loads(text)
        assert {"neo4j_connected", "has_data", "tool_count"} <= set(payload)


class TestAdvancedToolRegistration:
    """mcp-advanced-tools: the 12 task-level tools must be registered."""

    # The 6 advanced-analysis + 6 code-intelligence tools added by the
    # ``mcp-advanced-tools`` change. They must appear in ``list_tools()`` and be
    # dispatched by ``call_tool`` once implemented (TOOL_COUNT 28 -> 40).
    _EXPECTED_ADVANCED: ClassVar[set[str]] = {
        # advanced_analysis.py
        "detect_security_issues",
        "analyze_code_complexity",
        "find_dead_code",
        "analyze_change_impact",
        "find_similar_code",
        "get_architecture_metrics",
        # code_intelligence.py
        "get_code_context",
        "semantic_search",
        "suggest_refactoring",
        "explain_code",
        "trace_variable",
        "get_test_coverage_info",
    }

    def test_advanced_tools_are_registered(self) -> None:
        """The 12 advanced/intelligence tools must be present in ``list_tools()``.

        FAILS now because the tools do not exist yet; passes once the developer
        agent registers them and bumps ``TOOL_COUNT`` to 40.
        """
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}

        missing = self._EXPECTED_ADVANCED - names
        assert not missing, f"advanced tools not registered: {sorted(missing)}"
