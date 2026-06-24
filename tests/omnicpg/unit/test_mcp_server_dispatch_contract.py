"""Unit tests for MCP dispatch normalization and project scoping."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp_server_omnicpg.mcp_server as server
from mcp_server_omnicpg.config import Config


class _AdapterStub:
    """Small adapter stub for call_tool tests."""

    def __init__(self, project_rows: list[dict[str, Any]] | None = None) -> None:
        self.project_rows = project_rows or []

    def ensure_connected(self) -> None:
        """No-op connection guard used by tests."""

    def query(self, _query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        """Return configured project-id rows for resolver probes."""
        return self.project_rows

    def is_connected(self) -> bool:
        """Pretend Neo4j is connected for get_server_info."""
        return True


class TestCallToolAliasNormalization:
    """Compatibility aliases should map to canonical dispatch arguments."""

    def test_find_control_flow_accepts_source_target_aliases(self, monkeypatch) -> None:
        """source/target aliases should map to start/end for control-flow queries."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, Any] = {}

        def _fake_find_control_flow(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True}

        monkeypatch.setitem(server.registry.handlers, "find_control_flow", _fake_find_control_flow)

        result = asyncio.run(
            server.call_tool(
                "find_control_flow",
                {"source_node_id": "src-1", "target_node_id": "dst-1"},
            )
        )

        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        assert captured["start_node_id"] == "src-1"
        assert captured["end_node_id"] == "dst-1"
        assert captured["project_id"] == "proj-fixed"

    def test_apoc_spanning_tree_accepts_node_id_alias(self, monkeypatch) -> None:
        """node_id alias should map to start_node_id for spanning tree calls."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, Any] = {}

        def _fake_apoc_spanning_tree(**kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return [{"depth": 0}]

        monkeypatch.setitem(server.registry.handlers, "apoc_spanning_tree", _fake_apoc_spanning_tree)

        result = asyncio.run(server.call_tool("apoc_spanning_tree", {"node_id": "root-1"}))

        payload = json.loads(result[0].text)
        assert isinstance(payload, list)
        assert captured["start_node_id"] == "root-1"

    def test_semantic_search_accepts_query_alias(self, monkeypatch) -> None:
        """Query alias should map to intent for semantic search."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, Any] = {}

        def _fake_semantic_search(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"count": 0}

        monkeypatch.setitem(server.registry.handlers, "semantic_search", _fake_semantic_search)

        result = asyncio.run(server.call_tool("semantic_search", {"query": "Action"}))

        payload = json.loads(result[0].text)
        assert payload["count"] == 0
        assert captured["intent"] == "Action"
        assert captured["project_id"] == "proj-fixed"


class TestProjectScopeResolution:
    """Project scope resolution must be safe on multi-project graphs."""

    def test_scoped_tool_rejects_ambiguous_project_graph(self, monkeypatch) -> None:
        """Scoped tools should fail fast when project_id cannot be uniquely inferred."""
        monkeypatch.setattr(
            server,
            "adapter",
            _AdapterStub(
                project_rows=[
                    {"project_id": "proj-a", "c": 10},
                    {"project_id": "proj-b", "c": 5},
                ]
            ),
        )
        monkeypatch.setattr(Config, "PROJECT_ID", None)
        monkeypatch.setattr(server, "_RESOLVED_PROJECT_ID", None)

        result = asyncio.run(server.call_tool("query_nodes", {"limit": 1}))
        payload = json.loads(result[0].text)

        assert "error" in payload
        assert "Multiple project_id values detected" in payload["error"]

    def test_explicit_project_id_overrides_ambiguous_graph(self, monkeypatch) -> None:
        """Passing project_id in arguments should bypass ambiguous auto-discovery."""
        monkeypatch.setattr(
            server,
            "adapter",
            _AdapterStub(
                project_rows=[
                    {"project_id": "proj-a", "c": 10},
                    {"project_id": "proj-b", "c": 5},
                ]
            ),
        )
        monkeypatch.setattr(Config, "PROJECT_ID", None)
        monkeypatch.setattr(server, "_RESOLVED_PROJECT_ID", None)

        captured: dict[str, Any] = {}

        def _fake_query_nodes(**kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return [{"id": "n1"}]

        monkeypatch.setitem(server.registry.handlers, "query_nodes", _fake_query_nodes)

        result = asyncio.run(server.call_tool("query_nodes", {"project_id": "proj-a", "limit": 1}))
        payload = json.loads(result[0].text)

        assert isinstance(payload, list)
        assert captured["project_id"] == "proj-a"

    def test_unscoped_tool_still_works_on_ambiguous_graph(self, monkeypatch) -> None:
        """Unscoped status tools should still respond even with multiple projects."""
        monkeypatch.setattr(
            server,
            "adapter",
            _AdapterStub(
                project_rows=[
                    {"project_id": "proj-a", "c": 10},
                    {"project_id": "proj-b", "c": 5},
                ]
            ),
        )
        monkeypatch.setattr(Config, "PROJECT_ID", None)
        monkeypatch.setattr(server, "_RESOLVED_PROJECT_ID", None)

        result = asyncio.run(server.call_tool("get_server_info", {}))
        payload = json.loads(result[0].text)

        assert payload["neo4j_connected"] is True
        assert payload["tool_count"] == server.TOOL_COUNT


class TestRawApocDispatch:
    """Raw APOC tools should receive the resolved project_id."""

    def test_apoc_run_read_query_forwards_project_id(self, monkeypatch) -> None:
        """Dispatcher should forward project_id into the raw read query tool."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, Any] = {}

        def _fake_read_query(**kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return [{"id": "n1"}]

        monkeypatch.setitem(server.registry.handlers, "apoc_run_read_query", _fake_read_query)

        result = asyncio.run(
            server.call_tool(
                "apoc_run_read_query",
                {"cypher": "MATCH (n:Node {project_id: $project_id}) RETURN n"},
            )
        )

        payload = json.loads(result[0].text)
        assert isinstance(payload, list)
        assert captured["project_id"] == "proj-fixed"

    def test_apoc_run_timeboxed_query_forwards_project_id(self, monkeypatch) -> None:
        """Dispatcher should forward project_id into the timeboxed query tool."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, Any] = {}

        def _fake_timeboxed_query(**kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return [{"id": "n1"}]

        monkeypatch.setitem(server.registry.handlers, "apoc_run_timeboxed_query", _fake_timeboxed_query)

        result = asyncio.run(
            server.call_tool(
                "apoc_run_timeboxed_query",
                {"cypher": "MATCH (n:Node {project_id: $project_id}) RETURN n"},
            )
        )

        payload = json.loads(result[0].text)
        assert isinstance(payload, list)
        assert captured["project_id"] == "proj-fixed"


class TestRequiredParamValidation:
    """Missing required params must yield a clear, actionable error."""

    def test_missing_keyword_returns_structured_error(self, monkeypatch) -> None:
        """search_code without keyword should name the param, not raise KeyError."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        result = asyncio.run(server.call_tool("search_code", {}))
        payload = json.loads(result[0].text)

        assert "error" in payload
        assert "keyword" in payload["error"]
        assert "search_code" in payload["error"]

    def test_blank_node_id_rejected_with_hint(self, monkeypatch) -> None:
        """A blank node_id should be rejected with a provenance hint."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        result = asyncio.run(server.call_tool("explain_code", {"node_id": "   "}))
        payload = json.loads(result[0].text)

        assert "error" in payload
        assert "node_id" in payload["error"]
        assert "search_code" in payload["error"]


class TestNodeIdAliasNormalization:
    """The canonical node_id should satisfy id-typed params named differently."""

    def test_analyze_function_accepts_node_id(self, monkeypatch) -> None:
        """node_id should map to function_id for analyze_function."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, Any] = {}

        def _fake_analyze_function(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True}

        monkeypatch.setitem(server.registry.handlers, "analyze_function", _fake_analyze_function)

        result = asyncio.run(server.call_tool("analyze_function", {"node_id": "fn-1"}))
        payload = json.loads(result[0].text)

        assert payload["ok"] is True
        assert captured["function_id"] == "fn-1"

    def test_expand_method_on_demand_accepts_node_id(self, monkeypatch) -> None:
        """node_id should map to method_id for expand_method_on_demand."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, Any] = {}

        def _fake_expand(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"expanded": True}

        monkeypatch.setitem(server.registry.handlers, "expand_method_on_demand", _fake_expand)

        result = asyncio.run(server.call_tool("expand_method_on_demand", {"node_id": "m-1"}))
        payload = json.loads(result[0].text)

        assert payload["expanded"] is True
        assert captured["method_id"] == "m-1"


class TestProjectIdSchemaExposure:
    """High-priority scoped tools should expose project_id in tool schema."""

    def test_high_priority_tools_expose_project_id_property(self) -> None:
        """Schema should advertise project_id for multi-project-safe usage."""
        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}

        required_tools = {
            "get_architecture_metrics",
            "apoc_expand_path",
            "apoc_subgraph_around_node",
            "apoc_shortest_path",
            "apoc_spanning_tree",
            "find_callsite_method",
            "find_callers_of",
            "batch_callsite_methods",
        }

        for tool_name in required_tools:
            assert tool_name in by_name, f"tool not registered: {tool_name}"
            schema = by_name[tool_name].inputSchema
            props = schema.get("properties", {})
            assert "project_id" in props, f"project_id missing in {tool_name} schema"


class TestAliasSchemaUsability:
    """Alias-enabled tools should express alias-friendly schema constraints."""

    def test_alias_tools_use_union_required_schema(self) -> None:
        """Schemas should allow canonical or alias parameter names."""
        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}

        expected_one_of = {
            "find_control_flow": (
                {"start_node_id", "end_node_id"},
                {"source_node_id", "target_node_id"},
            ),
            "analyze_function": ({"function_id"}, {"node_id"}),
            "expand_method_on_demand": ({"method_id"}, {"node_id"}),
            "semantic_search": ({"intent"}, {"query"}),
            "apoc_spanning_tree": ({"start_node_id"}, {"node_id"}),
        }

        for tool_name, options in expected_one_of.items():
            schema = by_name[tool_name].inputSchema
            assert "oneOf" in schema, f"{tool_name} missing oneOf"
            actual = [set(option.get("required", [])) for option in schema["oneOf"]]
            for required_set in options:
                assert required_set in actual, (
                    f"{tool_name} oneOf missing required set {required_set}; actual={actual}"
                )


class TestCoverageInputValidation:
    """Coverage tool should require node_id or file_path."""

    def test_coverage_tool_schema_declares_any_of(self) -> None:
        """Schema should advertise at-least-one requirement for coverage target."""
        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}
        schema = by_name["get_test_coverage_info"].inputSchema

        assert "anyOf" in schema
        required_sets = [set(option.get("required", [])) for option in schema["anyOf"]]
        assert {"node_id"} in required_sets
        assert {"file_path"} in required_sets

    def test_coverage_tool_rejects_missing_node_and_file(self, monkeypatch) -> None:
        """Missing both node_id and file_path should return structured error."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        result = asyncio.run(
            server.call_tool("get_test_coverage_info", {"project_id": "proj-fixed"})
        )
        payload = json.loads(result[0].text)

        assert "error" in payload
        assert "node_id" in payload["error"]
        assert "file_path" in payload["error"]


class TestProjectIdDispatchForwarding:
    """Scoped traversal tools should receive resolved project_id from dispatcher."""

    def test_dispatch_forwards_project_id_to_high_priority_tools(self, monkeypatch) -> None:
        """call_tool should pass project_id to traversal/callsite/architecture tools."""
        monkeypatch.setattr(server, "adapter", _AdapterStub())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-fixed")

        captured: dict[str, dict[str, Any]] = {}

        def _capture(name: str):
            def _inner(**kwargs: Any) -> dict[str, Any]:
                captured[name] = kwargs
                return {"ok": True}

            return _inner

        monkeypatch.setitem(
            server.registry.handlers,
            "get_architecture_metrics",
            _capture("get_architecture_metrics"),
        )
        monkeypatch.setitem(server.registry.handlers, "apoc_expand_path", _capture("apoc_expand_path"))
        monkeypatch.setitem(
            server.registry.handlers,
            "apoc_subgraph_around_node",
            _capture("apoc_subgraph_around_node"),
        )
        monkeypatch.setitem(server.registry.handlers, "apoc_shortest_path", _capture("apoc_shortest_path"))
        monkeypatch.setitem(server.registry.handlers, "apoc_spanning_tree", _capture("apoc_spanning_tree"))
        monkeypatch.setitem(server.registry.handlers, "find_callsite_method", _capture("find_callsite_method"))
        monkeypatch.setitem(server.registry.handlers, "find_callers_of", _capture("find_callers_of"))
        monkeypatch.setitem(server.registry.handlers, "batch_callsite_methods", _capture("batch_callsite_methods"))

        calls = [
            ("get_architecture_metrics", {}),
            ("apoc_expand_path", {"start_node_id": "n1"}),
            ("apoc_subgraph_around_node", {"node_id": "n1"}),
            ("apoc_shortest_path", {"start_node_id": "n1", "end_node_id": "n2"}),
            ("apoc_spanning_tree", {"start_node_id": "n1"}),
            ("find_callsite_method", {"callsite_name": "execute"}),
            ("find_callers_of", {"method_name": "execute"}),
            ("batch_callsite_methods", {"callsite_names": ["execute"]}),
        ]

        for tool_name, arguments in calls:
            result = asyncio.run(server.call_tool(tool_name, arguments))
            payload = json.loads(result[0].text)
            assert payload["ok"] is True
            assert captured[tool_name]["project_id"] == "proj-fixed"
