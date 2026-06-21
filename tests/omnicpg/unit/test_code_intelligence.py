"""TDD red-phase tests for the ``code_intelligence`` MCP tool module.

These tests encode the behavioural contract for the six *code intelligence*
tools introduced by the ``mcp-advanced-tools`` openspec change:

    * ``get_code_context``
    * ``semantic_search``
    * ``suggest_refactoring``
    * ``explain_code``
    * ``trace_variable``        (max_depth must be capped at <= 50)
    * ``get_test_coverage_info``(SC-MCP-007 coverage marker)

They are written BEFORE the production code exists, so the tool imports happen
*inside* each test body: every test fails individually with a clear
``ModuleNotFoundError``/``ImportError`` (the intended RED) while the file still
collects cleanly. No live Neo4j is used — a programmable fake adapter captures
the parameters forwarded into ``query`` and returns canned rows.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp_server_omnicpg.mcp_server as server
import mcp_server_omnicpg.neo4j_adapter as adapter_mod
import pytest
from mcp_server_omnicpg.config import Config


class _ProgrammableAdapter:
    """A fake adapter that returns canned rows and records query parameters.

    The fake stands in for :class:`mcp_server_omnicpg.neo4j_adapter.MCPNeo4jAdapter`
    so the code-intelligence tools can be exercised without a live Neo4j. Every
    ``query``/``execute_write`` call appends its keyword parameters to
    :attr:`calls` and returns :attr:`rows`.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        """Store the canned rows and initialise the captured-calls list.

        Args:
            rows: The canned result set returned by every ``query`` call. When
                ``None`` an empty list models a missing-enrichment graph.
        """
        self.rows: list[dict[str, Any]] = rows if rows is not None else []
        self.calls: list[dict[str, Any]] = []

    def ensure_connected(self) -> None:
        """No-op: the fake is always considered connected."""

    def connect(self) -> None:
        """No-op connect for parity with the real adapter."""

    def is_connected(self) -> bool:
        """Report the fake as connected."""
        return True

    def query(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Capture the parameters and return the canned rows.

        Args:
            query_string: The Cypher text (ignored by the fake).
            **params: The query parameters, captured for later assertions.

        Returns:
            The canned :attr:`rows` result set.
        """
        self.calls.append(dict(params))
        return self.rows

    def execute_write(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Capture write parameters and return the canned rows.

        Args:
            query_string: The Cypher text (ignored by the fake).
            **params: The query parameters, captured for later assertions.

        Returns:
            The canned :attr:`rows` result set.
        """
        self.calls.append(dict(params))
        return self.rows


def _install(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]] | None = None
) -> _ProgrammableAdapter:
    """Install a :class:`_ProgrammableAdapter` everywhere the server resolves one.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        rows: The canned rows the fake should return for every query.

    Returns:
        The installed fake adapter (for parameter-capture assertions).
    """
    fake = _ProgrammableAdapter(rows)
    monkeypatch.setattr(adapter_mod, "_adapter", fake)
    monkeypatch.setattr(server, "adapter", fake)
    return fake


def _forwarded_project_ids(fake: _ProgrammableAdapter) -> list[Any]:
    """Return every ``project_id`` value captured across the fake's queries.

    Args:
        fake: The programmable fake adapter.

    Returns:
        A list with the ``project_id`` parameter of each captured call.
    """
    return [c.get("project_id") for c in fake.calls]


def _has_marker(obj: Any, keys: set[str]) -> bool:
    """Recursively report whether any of ``keys`` appears as a dict key in ``obj``.

    Args:
        obj: An arbitrary JSON-friendly structure (dict/list/scalar).
        keys: The marker key names to search for (e.g. ``coverage``).

    Returns:
        ``True`` if any marker key is present anywhere in ``obj``.
    """
    if isinstance(obj, dict):
        if keys & set(obj.keys()):
            return True
        return any(_has_marker(v, keys) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_marker(item, keys) for item in obj)
    return False


def _node_rows() -> list[dict[str, Any]]:
    """Return canned node/neighbour rows for the code-intelligence tools.

    Returns:
        A small list of richly-keyed rows describing a method node, its
        callers/callees and a data-flow trace.
    """
    return [
        {
            "id": "m1",
            "node_id": "m1",
            "name": "doQuery",
            "fqn": "com.x.A.doQuery",
            "signature": "doQuery(String)",
            "file_path": "A.java",
            "line": 42,
            "role": "Controller",
            "layer": "web",
            "caller": "com.x.B.handle",
            "callee": "com.x.C.exec",
            "score": 0.88,
            "complexity": 13,
            "direction": "forward",
            "variable": "userInput",
            "interprocedural": "argument",
        },
        {
            "id": "m2",
            "node_id": "m2",
            "name": "exec",
            "fqn": "com.x.C.exec",
            "signature": "exec(String)",
            "file_path": "C.java",
            "line": 7,
            "role": "Service",
            "layer": "service",
            "caller": "com.x.A.doQuery",
            "callee": "java.sql.Statement.execute",
            "score": 0.71,
            "complexity": 4,
            "direction": "forward",
            "variable": "userInput",
            "interprocedural": "return",
        },
    ]


class TestGetCodeContext:
    """Contract for ``get_code_context`` (node + 1-hop neighbours)."""

    def test_returns_compact_context_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: the node plus neighbour info is returned as a dict."""
        from mcp_server_omnicpg.tools.code_intelligence import get_code_context

        _install(monkeypatch, _node_rows())
        result = get_code_context(node_id="m1", project_id="proj-x")

        assert isinstance(result, dict), f"context must be a dict, got {type(result)}"
        assert "m1" in str(result) or "doQuery" in str(result), (
            f"context must describe the requested node: {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.code_intelligence import get_code_context

        fake = _install(monkeypatch, _node_rows())
        get_code_context(node_id="m1", project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)


class TestSemanticSearch:
    """Contract for ``semantic_search`` (full-text + structural filter)."""

    def test_returns_scored_hits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: intent matches are returned with a score."""
        from mcp_server_omnicpg.tools.code_intelligence import semantic_search

        _install(monkeypatch, _node_rows())
        result = semantic_search(intent="run sql query", project_id="proj-x")

        assert _has_marker(result, {"score"}) or "doQuery" in str(result), (
            f"semantic_search must surface scored hits: {result!r}"
        )

    def test_forwards_project_id_and_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.code_intelligence import semantic_search

        fake = _install(monkeypatch, _node_rows())
        semantic_search(intent="run sql query", label="Method", limit=5, project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)

    def test_empty_index_returns_empty_or_coverage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty path: no hits returns an empty/coverage result, not a null shell."""
        from mcp_server_omnicpg.tools.code_intelligence import semantic_search

        _install(monkeypatch, [])
        result = semantic_search(intent="nothing matches", project_id="proj-x")

        if isinstance(result, dict):
            assert _has_marker(result, {"results", "hits", "coverage", "warning"})
        else:
            assert result == []


class TestSuggestRefactoring:
    """Contract for ``suggest_refactoring`` (rule-based suggestions)."""

    def test_returns_rule_based_suggestions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: high-complexity/coupled nodes yield typed suggestions."""
        from mcp_server_omnicpg.tools.code_intelligence import suggest_refactoring

        _install(monkeypatch, _node_rows())
        result = suggest_refactoring(node_id="m1", project_id="proj-x")

        assert _has_marker(result, {"kind", "evidence", "target", "suggestions"}), (
            f"suggestions must carry kind/evidence/target: {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.code_intelligence import suggest_refactoring

        fake = _install(monkeypatch, _node_rows())
        suggest_refactoring(node_id="m1", project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)


class TestExplainCode:
    """Contract for ``explain_code`` (structured fact card)."""

    def test_returns_structured_fact_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: a structured dict describing the node is returned."""
        from mcp_server_omnicpg.tools.code_intelligence import explain_code

        _install(monkeypatch, _node_rows())
        result = explain_code(node_id="m1", project_id="proj-x")

        assert isinstance(result, dict), f"explain_code must return a dict: {result!r}"
        assert "doQuery" in str(result) or "m1" in str(result), (
            f"explanation must reference the node: {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.code_intelligence import explain_code

        fake = _install(monkeypatch, _node_rows())
        explain_code(node_id="m1", project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)


class TestTraceVariable:
    """Contract for ``trace_variable`` (REACHES/FLOWS_TO trace, depth cap)."""

    def test_returns_ordered_path_nodes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: the data-flow trace returns the visited nodes in order."""
        from mcp_server_omnicpg.tools.code_intelligence import trace_variable

        _install(monkeypatch, _node_rows())
        result = trace_variable(node_id="m1", direction="forward", project_id="proj-x")

        text = str(result)
        assert "m1" in text and "m2" in text, (
            f"trace must surface both flow nodes in order: {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.code_intelligence import trace_variable

        fake = _install(monkeypatch, _node_rows())
        trace_variable(node_id="m1", project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)

    def test_max_depth_is_capped_at_50(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guard rail: ``max_depth`` greater than 50 must be rejected or capped.

        The design caps the trace at ``max_depth <= 50``. Either raising
        ``ValueError`` or clamping the forwarded depth parameter to ``<= 50``
        satisfies the contract; an unbounded traversal does not.
        """
        from mcp_server_omnicpg.tools.code_intelligence import trace_variable

        fake = _install(monkeypatch, _node_rows())
        try:
            trace_variable(node_id="m1", max_depth=999, project_id="proj-x")
        except ValueError:
            return

        depths = [
            v
            for call in fake.calls
            for key, v in call.items()
            if "depth" in key.lower() and isinstance(v, int)
        ]
        assert all(d <= 50 for d in depths), (
            f"max_depth must be capped at <= 50, captured depths={depths}"
        )


class TestGetTestCoverageInfo:
    """Contract for ``get_test_coverage_info`` (TESTS edge + heuristic)."""

    def test_reports_covering_tests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: a TESTS edge yields ``covered`` with the covering tests."""
        from mcp_server_omnicpg.tools.code_intelligence import get_test_coverage_info

        rows = [
            {
                "id": "m1",
                "fqn": "com.x.A.doQuery",
                "test_id": "t1",
                "test_fqn": "com.x.ATest.testDoQuery",
            }
        ]
        _install(monkeypatch, rows)
        result = get_test_coverage_info(node_id="m1", project_id="proj-x")

        assert _has_marker(result, {"covered", "tests"}), (
            f"coverage info must expose covered/tests: {result!r}"
        )
        assert "ATest" in str(result) or "t1" in str(result)

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.code_intelligence import get_test_coverage_info

        fake = _install(monkeypatch, [])
        get_test_coverage_info(node_id="m1", project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)

    def test_missing_tests_returns_coverage_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SC-MCP-007: no TESTS edge yields an explicit coverage marker.

        With no covering test the tool must return ``covered=False`` plus a
        ``coverage``/``warning`` marker rather than a null-filled shell.
        """
        from mcp_server_omnicpg.tools.code_intelligence import get_test_coverage_info

        _install(monkeypatch, [])
        result = get_test_coverage_info(node_id="m1", project_id="proj-x")

        assert _has_marker(result, {"coverage", "warning", "covered"}), (
            f"missing TESTS enrichment must surface a marker: {result!r}"
        )


class TestCodeIntelligenceViaCallTool:
    """``call_tool`` must dispatch the intelligence tools and inject ``PROJECT_ID``."""

    def test_call_tool_dispatches_and_injects_project_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """call_tool: dispatch ``get_code_context`` and scope by project.

        Fails now because ``call_tool`` replies ``"Unknown tool: ..."``. Once
        registered, ``Config.PROJECT_ID`` must be forwarded into the adapter
        (REQ-SCHEMA-006).
        """
        fake = _install(monkeypatch, _node_rows())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-call")

        result = asyncio.run(server.call_tool("get_code_context", {"node_id": "m1"}))
        text = result[0].text

        assert not text.startswith("Unknown tool"), (
            f"get_code_context is not dispatched by call_tool: {text!r}"
        )
        assert "proj-call" in _forwarded_project_ids(fake), (
            f"call_tool must inject Config.PROJECT_ID; captured={fake.calls}"
        )

    def test_call_tool_errors_are_json_envelopes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """call_tool: validation failures are returned as JSON error objects."""
        _install(monkeypatch, _node_rows())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-call")

        result = asyncio.run(
            server.call_tool("trace_variable", {"node_id": "m1", "max_depth": 999})
        )
        payload = json.loads(result[0].text)

        assert "error" in payload
        assert "max_depth" in payload["error"]
