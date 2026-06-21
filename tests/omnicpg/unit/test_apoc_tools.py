"""Unit tests for APOC-powered MCP tools.

Tests are structured to run without a live Neo4j instance; the adapter is
mocked so that every test exercises the tool's business logic and Cypher
construction without requiring an actual APOC installation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp_server_omnicpg.tools.apoc_tools import (
    _is_safe_read_query,
    apoc_expand_path,
    apoc_graph_schema,
    apoc_meta_stats,
    apoc_run_read_query,
    apoc_run_timeboxed_query,
    apoc_shortest_path,
    apoc_spanning_tree,
    apoc_subgraph_around_node,
    batch_callsite_methods,
    find_callers_of,
    find_callsite_method,
)
from neo4j.exceptions import ClientError

# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_adapter(return_value: list[dict[str, Any]]) -> MagicMock:
    """Return a mock MCPNeo4jAdapter that returns *return_value* from query()."""
    adapter = MagicMock()
    adapter.query.return_value = return_value
    adapter.ensure_connected.return_value = None
    return adapter


# ── _is_safe_read_query ───────────────────────────────────────────────────────


class TestIsSafeReadQuery:
    """Unit tests for the write-operation guard."""

    @pytest.mark.parametrize(
        "safe_query",
        [
            "MATCH (n:Node) RETURN n",
            "MATCH (n:Node) WHERE n.name = 'foo' RETURN n LIMIT 10",
            "MATCH (a)-[r]->(b) RETURN type(r)",
            "CALL apoc.meta.stats() YIELD nodeCount RETURN nodeCount",
            "CALL apoc.path.expandConfig(start, {}) YIELD path RETURN path",
            "WITH 1 AS x RETURN x",
        ],
    )
    def test_safe_queries_pass(self, safe_query: str) -> None:
        """Queries with no write keywords should return True."""
        assert _is_safe_read_query(safe_query) is True

    @pytest.mark.parametrize(
        "unsafe_query",
        [
            "CREATE (n:Node {id: 'x'})",
            "MERGE (n:Node {id: 'x'})",
            "MATCH (n) DELETE n",
            "MATCH (n) DETACH DELETE n",
            "MATCH (n) SET n.foo = 'bar'",
            "MATCH (n) REMOVE n.foo",
            "DROP INDEX foo",
            "LOAD CSV FROM 'file.csv' AS row RETURN row",
        ],
    )
    def test_write_queries_rejected(self, unsafe_query: str) -> None:
        """Write queries raise ValueError with descriptive message."""
        assert _is_safe_read_query(unsafe_query) is False

    def test_case_insensitive(self) -> None:
        """Detection must be case-insensitive."""
        assert _is_safe_read_query("create (n) return n") is False
        assert _is_safe_read_query("MATCH (n) set n.x = 1") is False


# ── apoc_expand_path ──────────────────────────────────────────────────────────


class TestApocExpandPath:
    """Tests for apoc_expand_path."""

    def test_returns_formatted_paths(self) -> None:
        """Path rows are normalised into node/edge dicts."""
        mock_rows = [
            {
                "node_ids": ["a", "b", "c"],
                "edge_types": ["CALLS", "CONTAINS"],
                "path_length": 2,
            }
        ]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(mock_rows),
        ):
            result = apoc_expand_path(start_node_id="a", max_level=3)

        assert len(result) == 1
        path = result[0]
        assert path["nodes"] == ["a", "b", "c"]
        assert path["length"] == 2
        assert len(path["edges"]) == 2
        assert path["edges"][0] == {"type": "CALLS", "source": "a", "target": "b"}
        assert path["edges"][1] == {"type": "CONTAINS", "source": "b", "target": "c"}

    def test_empty_result(self) -> None:
        """Empty adapter response maps to empty list."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = apoc_expand_path(start_node_id="x")
        assert result == []

    def test_invalid_max_level(self) -> None:
        """max_level < min_level raises ValueError."""
        with pytest.raises(ValueError, match="max_level must be >= min_level"):
            apoc_expand_path(start_node_id="a", min_level=5, max_level=2)

    def test_invalid_limit(self) -> None:
        """Limit < 1 raises ValueError."""
        with pytest.raises(ValueError, match="limit must be at least 1"):
            apoc_expand_path(start_node_id="a", limit=0)

    def test_query_receives_correct_params(self) -> None:
        """All parameters are forwarded to adapter.query()."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            apoc_expand_path(
                start_node_id="node-1",
                relationship_filter="CALLS>",
                label_filter="+Method",
                min_level=2,
                max_level=4,
                bfs=False,
                limit=20,
                uniqueness="RELATIONSHIP_GLOBAL",
                project_id="proj-1",
            )

        _, kwargs = adapter.query.call_args
        assert kwargs["start_node_id"] == "node-1"
        assert kwargs["relationship_filter"] == "CALLS>"
        assert kwargs["label_filter"] == "+Method"
        assert kwargs["min_level"] == 2
        assert kwargs["max_level"] == 4
        assert kwargs["bfs"] is False
        assert kwargs["limit"] == 20
        assert kwargs["uniqueness"] == "RELATIONSHIP_GLOBAL"
        assert kwargs["project_id"] == "proj-1"


# ── apoc_subgraph_around_node ─────────────────────────────────────────────────


class TestApocSubgraphAroundNode:
    """Tests for apoc_subgraph_around_node."""

    def test_returns_nodes_and_relationships(self) -> None:
        """Subgraph nodes and relationships are returned."""
        nodes = [{"id": "a", "type": "Method", "name": "foo", "file_path": "f.py"}]
        rels = [{"type": "CALLS", "source": "a", "target": "b"}]
        mock_rows = [{"nodes": nodes, "relationships": rels}]

        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(mock_rows),
        ):
            result = apoc_subgraph_around_node(node_id="a")

        assert result["center_id"] == "a"
        assert result["nodes"] == nodes
        assert result["relationships"] == rels

    def test_empty_graph_returns_empty(self) -> None:
        """Empty adapter response returns empty subgraph dict."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = apoc_subgraph_around_node(node_id="x")

        assert result == {"center_id": "x", "nodes": [], "relationships": []}

    def test_invalid_max_level(self) -> None:
        """max_level < min_level raises ValueError."""
        with pytest.raises(ValueError, match="max_level must be at least 1"):
            apoc_subgraph_around_node(node_id="x", max_level=0)


# ── apoc_shortest_path ────────────────────────────────────────────────────────


class TestApocShortestPath:
    """Tests for apoc_shortest_path."""

    def test_returns_path_when_found(self) -> None:
        """A single path row is formatted correctly."""
        mock_rows = [
            {
                "node_ids": ["a", "b"],
                "edge_types": ["CALLS"],
                "path_length": 1,
            }
        ]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(mock_rows),
        ):
            result = apoc_shortest_path(start_node_id="a", end_node_id="b")

        assert result is not None
        assert result["nodes"] == ["a", "b"]
        assert result["length"] == 1
        assert result["edges"] == [{"type": "CALLS", "source": "a", "target": "b"}]

    def test_returns_none_when_no_path(self) -> None:
        """Returns None when no path exists."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = apoc_shortest_path(start_node_id="a", end_node_id="z")

        assert result is None

    def test_invalid_max_level(self) -> None:
        """max_level < min_level raises ValueError."""
        with pytest.raises(ValueError, match="max_level must be at least 1"):
            apoc_shortest_path(start_node_id="a", end_node_id="b", max_level=0)


# ── apoc_spanning_tree ────────────────────────────────────────────────────────


class TestApocSpanningTree:
    """Tests for apoc_spanning_tree."""

    def test_returns_tree_paths(self) -> None:
        """Spanning-tree rows are formatted into path dicts."""
        nodes = [
            {"id": "root", "type": "Class", "name": "MyClass"},
            {"id": "m1", "type": "Method", "name": "doStuff"},
        ]
        mock_rows = [
            {
                "nodes": nodes,
                "edge_types": ["CONTAINS"],
                "depth": 1,
            }
        ]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(mock_rows),
        ):
            result = apoc_spanning_tree(start_node_id="root")

        assert len(result) == 1
        assert result[0]["depth"] == 1
        assert len(result[0]["edges"]) == 1
        assert result[0]["edges"][0]["type"] == "CONTAINS"

    def test_empty_result(self) -> None:
        """Empty adapter response maps to empty list."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = apoc_spanning_tree(start_node_id="x")
        assert result == []

    def test_invalid_max_level(self) -> None:
        """max_level < min_level raises ValueError."""
        with pytest.raises(ValueError, match="max_level must be at least 1"):
            apoc_spanning_tree(start_node_id="x", max_level=0)

    def test_invalid_limit(self) -> None:
        """Limit < 1 raises ValueError."""
        with pytest.raises(ValueError, match="limit must be at least 1"):
            apoc_spanning_tree(start_node_id="x", limit=0)


# ── apoc_graph_schema ─────────────────────────────────────────────────────────


class TestApocGraphSchema:
    """Tests for apoc_graph_schema."""

    def test_returns_schema_dict(self) -> None:
        """Schema value is wrapped under the schema key."""
        schema_value = {"Node": {"properties": {"id": "String"}, "relationships": {}}}
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([{"value": schema_value}]),
        ):
            result = apoc_graph_schema()

        assert "schema" in result
        assert result["schema"] == schema_value

    def test_empty_db_returns_empty_schema(self) -> None:
        """Empty adapter response returns empty schema."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = apoc_graph_schema()

        assert result == {"schema": {}}

    def test_fallback_when_apoc_schema_is_restricted(self) -> None:
        """Restricted apoc.meta.schema should fall back to plain Cypher-derived schema."""
        adapter = MagicMock()
        adapter.ensure_connected.return_value = None

        def _query_side_effect(query: str, **kwargs: object) -> list[dict[str, Any]]:
            if "apoc.meta.schema" in query:
                raise ClientError(
                    "{neo4j_code: Neo.ClientError.Procedure.ProcedureRegistrationFailed} "
                    "{message: apoc.meta.schema is unavailable because it is sandboxed}"
                )
            if "UNWIND labels(n) AS label" in query:
                return [{"label": "Node", "properties": ["id", "name"]}]
            if "UNWIND labels(a) AS from_label" in query:
                return [{"from_label": "Node", "rel_type": "CALLS", "targets": ["Node"]}]
            return []

        adapter.query.side_effect = _query_side_effect

        with patch("mcp_server_omnicpg.tools.apoc_tools.get_adapter", return_value=adapter):
            result = apoc_graph_schema()

        assert "schema" in result
        assert "Node" in result["schema"]
        assert "CALLS" in result["schema"]["Node"]["relationships"]


# ── apoc_meta_stats ───────────────────────────────────────────────────────────


class TestApocMetaStats:
    """Tests for apoc_meta_stats."""

    def test_returns_stats(self) -> None:
        """Stats row is returned with expected keys."""
        stats_row = {
            "nodeCount": 1000,
            "relCount": 5000,
            "labelCount": 5,
            "relTypeCount": 10,
            "propertyKeyCount": 20,
            "labels": {"Node": 1000},
            "relTypes": {"CALLS": 3000},
        }
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([stats_row]),
        ):
            result = apoc_meta_stats()

        assert result["nodeCount"] == 1000
        assert result["relCount"] == 5000
        assert result["labels"] == {"Node": 1000}
        assert result["relTypes"] == {"CALLS": 3000}

    def test_empty_db_returns_empty(self) -> None:
        """Empty adapter response returns empty dict."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = apoc_meta_stats()
        assert result == {}

    def test_fallback_when_apoc_stats_is_restricted(self) -> None:
        """Restricted apoc.meta.stats should fall back to plain Cypher-derived stats."""
        adapter = MagicMock()
        adapter.ensure_connected.return_value = None

        def _query_side_effect(query: str, **kwargs: object) -> list[dict[str, Any]]:
            if "apoc.meta.stats" in query:
                raise ClientError(
                    "{neo4j_code: Neo.ClientError.Procedure.ProcedureRegistrationFailed} "
                    "{message: apoc.meta.stats is unavailable because it is sandboxed}"
                )
            if "MATCH (n) RETURN count(n) AS c" in query:
                return [{"c": 10}]
            if "MATCH ()-[r]->() RETURN count(r) AS c" in query:
                return [{"c": 20}]
            if "UNWIND labels(n) AS label" in query:
                return [{"label": "Node", "c": 10}]
            if "type(r) AS rel_type" in query:
                return [{"rel_type": "CALLS", "c": 20}]
            if "collect(DISTINCT key) AS node_keys" in query:
                return [{"node_keys": ["id", "name"]}]
            if "collect(DISTINCT key) AS rel_keys" in query:
                return [{"rel_keys": ["weight"]}]
            return []

        adapter.query.side_effect = _query_side_effect

        with patch("mcp_server_omnicpg.tools.apoc_tools.get_adapter", return_value=adapter):
            result = apoc_meta_stats()

        assert result["nodeCount"] == 10
        assert result["relCount"] == 20
        assert result["labelCount"] == 1
        assert result["relTypeCount"] == 1
        assert result["propertyKeyCount"] == 3
        assert result["labels"] == {"Node": 10}
        assert result["relTypes"] == {"CALLS": 20}


# ── apoc_run_read_query ───────────────────────────────────────────────────────


class TestApocRunReadQuery:
    """Tests for apoc_run_read_query."""

    def test_safe_query_executes(self) -> None:
        """Safe read query is forwarded to the adapter."""
        rows = [{"n": "value"}]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(rows),
        ):
            result = apoc_run_read_query(
                "MATCH (n:Node {project_id: $project_id}) RETURN n",
                project_id="proj-1",
            )

        assert result == rows

    def test_limit_appended_when_missing(self) -> None:
        """LIMIT clause is appended when not present in query."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            apoc_run_read_query(
                "MATCH (n:Node {project_id: $project_id}) RETURN n",
                limit=42,
                project_id="proj-1",
            )

        query_arg: str = adapter.query.call_args[0][0]
        assert "LIMIT 42" in query_arg

    def test_existing_limit_not_doubled(self) -> None:
        """Existing LIMIT in query is not duplicated."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            apoc_run_read_query(
                "MATCH (n:Node {project_id: $project_id}) RETURN n LIMIT 5",
                limit=100,
                project_id="proj-1",
            )

        query_arg: str = adapter.query.call_args[0][0]
        # Should not append another LIMIT
        assert query_arg.count("LIMIT") == 1

    @pytest.mark.parametrize(
        "unsafe",
        [
            "CREATE (n:Node)",
            "MERGE (n:Node {id: '1'})",
            "MATCH (n) DELETE n",
            "MATCH (n) SET n.x = 1",
            "MATCH (n) REMOVE n.x",
            "DROP INDEX foo",
            "LOAD CSV FROM 'x' AS row RETURN row",
        ],
    )
    def test_write_queries_rejected(self, unsafe: str) -> None:
        """Write queries raise ValueError with descriptive message."""
        with pytest.raises(ValueError, match="Query rejected"):
            apoc_run_read_query(unsafe, project_id="proj-1")

    def test_empty_query_rejected(self) -> None:
        """Blank query string raises ValueError."""
        with pytest.raises(ValueError, match="cypher must not be empty"):
            apoc_run_read_query("  ", project_id="proj-1")

    def test_project_scope_rejected_when_missing_filter(self) -> None:
        """Raw read query must explicitly mention project_id to preserve scope."""
        with pytest.raises(ValueError, match="explicitly filter by project_id"):
            apoc_run_read_query("MATCH (n:Node) RETURN n", project_id="proj-1")

    def test_project_scope_rejected_when_missing_project_id(self) -> None:
        """Raw read query must receive a project_id."""
        with pytest.raises(ValueError, match="project_id must be provided"):
            apoc_run_read_query("MATCH (n:Node {project_id: $project_id}) RETURN n")

    def test_limit_out_of_range(self) -> None:
        """Limit outside 1-500 raises ValueError."""
        with pytest.raises(ValueError, match="limit must be between 1 and 500"):
            apoc_run_read_query("MATCH (n) RETURN n", limit=0)

        with pytest.raises(ValueError, match="limit must be between 1 and 500"):
            apoc_run_read_query("MATCH (n) RETURN n", limit=501)

    def test_params_forwarded(self) -> None:
        """All parameters are forwarded to adapter.query()."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            apoc_run_read_query(
                "MATCH (n:Node {project_id: $project_id, id: $nid}) RETURN n",
                params={"nid": "abc"},
                project_id="proj-1",
            )

        _, kwargs = adapter.query.call_args
        assert kwargs.get("nid") == "abc"
        assert kwargs.get("project_id") == "proj-1"


# ── find_callsite_method ──────────────────────────────────────────────────────


class TestFindCallsiteMethod:
    """Tests for find_callsite_method (CallSite -> Method via <PARENT_OF)."""

    def _row(self, **kwargs: object) -> dict[str, object]:
        defaults: dict[str, object] = {
            "callsite_id": "cs-1",
            "callsite_code": "obj.execute(arg)",
            "callsite_line": 42,
            "callsite_file": "/src/hcscore/Foo.java",
            "method_name": "process",
            "method_file": "/src/hcscore/Foo.java",
            "method_id": "m-1",
            "depth": 3,
        }
        defaults.update(kwargs)
        return defaults

    def test_returns_formatted_results(self) -> None:
        """Matching (CallSite, Method) rows are formatted."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([self._row()]),
        ):
            result = find_callsite_method(callsite_name="execute")

        assert len(result) == 1
        r = result[0]
        assert r["callsite_id"] == "cs-1"
        assert r["method_name"] == "process"
        assert r["depth"] == 3

    def test_empty_result(self) -> None:
        """Empty adapter response maps to empty list."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = find_callsite_method(callsite_name="nonexistent")
        assert result == []

    def test_invalid_limit(self) -> None:
        """Limit < 1 raises ValueError."""
        with pytest.raises(ValueError, match="limit must be at least 1"):
            find_callsite_method(callsite_name="foo", limit=0)

    def test_invalid_max_depth(self) -> None:
        """max_depth < 1 raises ValueError."""
        with pytest.raises(ValueError, match="max_depth must be at least 1"):
            find_callsite_method(callsite_name="foo", max_depth=0)

    def test_file_path_filter_forwarded(self) -> None:
        """file_path_contains and other params are forwarded."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            find_callsite_method(
                callsite_name="save",
                file_path_contains="hcscore",
                max_depth=10,
                limit=5,
                project_id="proj-1",
            )
        _, kwargs = adapter.query.call_args
        assert kwargs["callsite_name"] == "save"
        assert kwargs["file_path_contains"] == "hcscore"
        assert kwargs["max_depth"] == 10
        assert kwargs["limit"] == 5
        assert kwargs["project_id"] == "proj-1"

    def test_no_file_filter_query_omits_where(self) -> None:
        """Without a file_path_contains, no WHERE clause is injected."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            find_callsite_method(callsite_name="execute", file_path_contains="")
        query_text: str = adapter.query.call_args[0][0]
        # No "file_path_contains" WHERE condition should appear in the query
        assert "file_path_contains" not in query_text


# ── find_callers_of ───────────────────────────────────────────────────────────


class TestFindCallersOf:
    """Tests for find_callers_of (impact analysis)."""

    def _row(self, **kwargs: object) -> dict[str, object]:
        defaults: dict[str, object] = {
            "caller_method": "handleRequest",
            "caller_file": "/src/hcscore/Controller.java",
            "caller_id": "m-2",
            "call_code": "service.getMap(request)",
            "call_line": 77,
        }
        defaults.update(kwargs)
        return defaults

    def test_returns_callers(self) -> None:
        """Callers list is returned with expected fields."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([self._row(), self._row(caller_method="init")]),
        ):
            result = find_callers_of(method_name="getMap")

        assert len(result) == 2
        assert result[0]["caller_method"] == "handleRequest"
        assert result[1]["caller_method"] == "init"

    def test_empty_result(self) -> None:
        """Empty adapter response maps to empty list."""
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter([]),
        ):
            result = find_callers_of(method_name="orphaned")
        assert result == []

    def test_invalid_limit(self) -> None:
        """Limit < 1 raises ValueError."""
        with pytest.raises(ValueError, match="limit must be at least 1"):
            find_callers_of(method_name="foo", limit=0)

    def test_invalid_max_depth(self) -> None:
        """max_depth < 1 raises ValueError."""
        with pytest.raises(ValueError, match="max_depth must be at least 1"):
            find_callers_of(method_name="foo", max_depth=0)

    def test_params_forwarded(self) -> None:
        """All parameters are forwarded to adapter.query()."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            find_callers_of(
                method_name="delete",
                file_path_contains="hcscore",
                max_depth=12,
                limit=15,
                project_id="proj-1",
            )
        _, kwargs = adapter.query.call_args
        assert kwargs["method_name"] == "delete"
        assert kwargs["file_path_contains"] == "hcscore"
        assert kwargs["max_depth"] == 12
        assert kwargs["limit"] == 15
        assert kwargs["project_id"] == "proj-1"


# ── batch_callsite_methods ────────────────────────────────────────────────────


class TestBatchCallsiteMethods:
    """Tests for batch_callsite_methods."""

    def _row(self, name: str = "execute") -> dict[str, object]:
        return {
            "callsite_name": name,
            "callsite_code": f"obj.{name}()",
            "callsite_line": 10,
            "callsite_file": "/src/hcscore/A.java",
            "method_name": "doWork",
            "method_file": "/src/hcscore/A.java",
            "method_id": "m-99",
        }

    def test_returns_batch_results(self) -> None:
        """Results for all callsite names are returned."""
        rows = [self._row("execute"), self._row("save"), self._row("delete")]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(rows),
        ):
            result = batch_callsite_methods(callsite_names=["execute", "save", "delete"])

        assert len(result) == 3
        names = {r["callsite_name"] for r in result}
        assert names == {"execute", "save", "delete"}

    def test_empty_names_raises(self) -> None:
        """Empty callsite_names list raises ValueError."""
        with pytest.raises(ValueError, match="callsite_names must not be empty"):
            batch_callsite_methods(callsite_names=[])

    def test_invalid_limit_per_name(self) -> None:
        """limit_per_name < 1 raises ValueError."""
        with pytest.raises(ValueError, match="limit_per_name must be at least 1"):
            batch_callsite_methods(callsite_names=["x"], limit_per_name=0)

    def test_invalid_max_depth(self) -> None:
        """max_depth < 1 raises ValueError."""
        with pytest.raises(ValueError, match="max_depth must be at least 1"):
            batch_callsite_methods(callsite_names=["x"], max_depth=0)

    def test_names_forwarded_to_query(self) -> None:
        """callsite_names and other params are forwarded."""
        adapter = _mock_adapter([])
        names = ["execute", "save"]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            batch_callsite_methods(
                callsite_names=names,
                file_path_contains="hcscore",
                limit_per_name=5,
                project_id="proj-1",
            )
        _, kwargs = adapter.query.call_args
        assert kwargs["callsite_names"] == names
        assert kwargs["file_path_contains"] == "hcscore"
        assert kwargs["limit_per_name"] == 5
        assert kwargs["project_id"] == "proj-1"

    def test_no_file_filter_omits_where(self) -> None:
        """Empty file_path_contains omits WHERE from query."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            batch_callsite_methods(callsite_names=["x"], file_path_contains="")
        query_text: str = adapter.query.call_args[0][0]
        assert "file_path_contains" not in query_text


# ── apoc_run_timeboxed_query ───────────────────────────────────────────────────


class TestApocRunTimboxedQuery:
    """Tests for apoc_run_timeboxed_query."""

    def test_safe_query_executes_and_unwraps(self) -> None:
        """Value maps in rows are unwrapped to plain dicts."""
        rows = [{"value": {"name": "foo", "cpx": 42}}]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(rows),
        ):
            cypher = (
                "MATCH (m:Method {project_id: $project_id}) "
                "RETURN m.name AS name, m.complexity AS cpx"
            )
            result = apoc_run_timeboxed_query(cypher, project_id="proj-1")

        assert len(result) == 1
        assert result[0]["name"] == "foo"
        assert result[0]["cpx"] == 42

    def test_non_dict_value_rows_returned_as_is(self) -> None:
        """Non-dict value rows are returned unchanged."""
        rows = [{"value": None}]
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=_mock_adapter(rows),
        ):
            result = apoc_run_timeboxed_query(
                "MATCH (n:Node {project_id: $project_id}) RETURN n",
                project_id="proj-1",
            )
        assert result == [{"value": None}]

    def test_write_query_rejected(self) -> None:
        """Write queries raise ValueError."""
        with pytest.raises(ValueError, match="Query rejected"):
            apoc_run_timeboxed_query("CREATE (n:Node)", project_id="proj-1")

    def test_empty_query_rejected(self) -> None:
        """Blank query string raises ValueError."""
        with pytest.raises(ValueError, match="cypher must not be empty"):
            apoc_run_timeboxed_query("  ", project_id="proj-1")

    def test_project_scope_rejected_when_missing_filter(self) -> None:
        """Raw timeboxed query must explicitly mention project_id to preserve scope."""
        with pytest.raises(ValueError, match="explicitly filter by project_id"):
            apoc_run_timeboxed_query("MATCH (n:Node) RETURN n", project_id="proj-1")

    def test_project_scope_rejected_when_missing_project_id(self) -> None:
        """Raw timeboxed query must receive a project_id."""
        with pytest.raises(ValueError, match="project_id must be provided"):
            apoc_run_timeboxed_query("MATCH (n:Node {project_id: $project_id}) RETURN n")

    def test_timeout_out_of_range(self) -> None:
        """timeout_ms outside 1-60000 raises ValueError."""
        with pytest.raises(ValueError, match="timeout_ms must be between 1 and 60000"):
            apoc_run_timeboxed_query("MATCH (n) RETURN n", timeout_ms=0)
        with pytest.raises(ValueError, match="timeout_ms must be between 1 and 60000"):
            apoc_run_timeboxed_query("MATCH (n) RETURN n", timeout_ms=60_001)

    def test_limit_out_of_range(self) -> None:
        """Limit outside 1-500 raises ValueError."""
        with pytest.raises(ValueError, match="limit must be between 1 and 500"):
            apoc_run_timeboxed_query("MATCH (n) RETURN n", limit=0)
        with pytest.raises(ValueError, match="limit must be between 1 and 500"):
            apoc_run_timeboxed_query("MATCH (n) RETURN n", limit=501)

    def test_params_and_timeout_forwarded(self) -> None:
        """Params and timeout_ms are forwarded to adapter.query()."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            apoc_run_timeboxed_query(
                (
                    "MATCH (m:Method {project_id: $project_id}) "
                    "WHERE m.complexity > $min RETURN m.name"
                ),
                params={"min": 50},
                timeout_ms=5_000,
                project_id="proj-1",
            )
        _, kwargs = adapter.query.call_args
        assert kwargs["timeout_ms"] == 5_000
        assert kwargs["params"] == {"min": 50}
        assert kwargs["project_id"] == "proj-1"

    def test_limit_embedded_in_wrapper_query(self) -> None:
        """LIMIT N is embedded in the wrapper Cypher."""
        adapter = _mock_adapter([])
        with patch(
            "mcp_server_omnicpg.tools.apoc_tools.get_adapter",
            return_value=adapter,
        ):
            apoc_run_timeboxed_query(
                "MATCH (n:Node {project_id: $project_id}) RETURN n",
                limit=42,
                project_id="proj-1",
            )
        query_text: str = adapter.query.call_args[0][0]
        assert "LIMIT 42" in query_text
