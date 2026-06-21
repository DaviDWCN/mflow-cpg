"""TDD red-phase tests for the ``advanced_analysis`` MCP tool module.

These tests encode the behavioural contract for the six *advanced analysis*
tools introduced by the ``mcp-advanced-tools`` openspec change:

    * ``detect_security_issues``   (REQ-MCP-007, SC-MCP-006, SC-TAINT-004)
    * ``analyze_code_complexity``  (SC-MCP-007)
    * ``find_dead_code``
    * ``analyze_change_impact``
    * ``find_similar_code``
    * ``get_architecture_metrics`` (SC-MCP-007 coverage marker)

They are written BEFORE the production code exists, so the tool imports happen
*inside* each test body: every test therefore fails individually with a clear
``ModuleNotFoundError``/``ImportError`` (the intended RED) while the file still
collects cleanly. No live Neo4j is used — a programmable fake adapter captures
the parameters forwarded into ``query`` and returns canned rows.
"""

from __future__ import annotations

import asyncio
from typing import Any

import mcp_server_omnicpg.mcp_server as server
import mcp_server_omnicpg.neo4j_adapter as adapter_mod
import pytest
from mcp_server_omnicpg.config import Config


class _ProgrammableAdapter:
    """A fake adapter that returns canned rows and records query parameters.

    The fake stands in for :class:`mcp_server_omnicpg.neo4j_adapter.MCPNeo4jAdapter`
    so the advanced-analysis tools can be exercised without a live Neo4j. Every
    ``query``/``execute_write`` call appends its keyword parameters to
    :attr:`calls` and returns :attr:`rows` (a programmable canned result set).
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        """Store the canned rows and initialise the captured-calls list.

        Args:
            rows: The canned result set returned by every ``query`` call. When
                ``None`` an empty list is used to model a missing-enrichment
                graph.
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


def _findings(result: Any) -> list[dict[str, Any]]:
    """Normalise a security result into a flat list of finding dicts.

    The spec is ambiguous about whether ``detect_security_issues`` returns a
    bare list or a ``{"findings": [...]}`` envelope, so this helper accepts
    either shape.

    Args:
        result: The raw value returned by ``detect_security_issues``.

    Returns:
        The list of finding dictionaries (empty when none are present).
    """
    if isinstance(result, list):
        return [f for f in result if isinstance(f, dict)]
    if isinstance(result, dict):
        for key in ("findings", "results", "issues"):
            value = result.get(key)
            if isinstance(value, list):
                return [f for f in value if isinstance(f, dict)]
    return []


# A single rich row whose superset of keys covers whatever RETURN aliases the
# production tools end up using. Two distances let impact-analysis group rows.
def _rich_rows() -> list[dict[str, Any]]:
    """Return canned rows with a superset of plausible RETURN aliases.

    Returns:
        Two method-shaped rows (distance 1 and 2) carrying ids, locations,
        complexity, fan-in/out, similarity scores and taint-edge evidence.
    """
    return [
        {
            "id": "m1",
            "node_id": "m1",
            "name": "doQuery",
            "fqn": "com.x.A.doQuery",
            "file_path": "A.java",
            "line": 42,
            "complexity": 14,
            "mccabe": 14,
            "fan_in": 3,
            "fan_out": 5,
            "distance": 1,
            "score": 0.92,
            "similarity": 0.92,
            "role": "Controller",
            "layer": "web",
            "source_id": "s1",
            "sink_id": "k1",
            "interprocedural": "argument",
            "rule": "sql-injection",
            "severity": "high",
        },
        {
            "id": "m2",
            "node_id": "m2",
            "name": "helper",
            "fqn": "com.x.A.helper",
            "file_path": "A.java",
            "line": 88,
            "complexity": 7,
            "mccabe": 7,
            "fan_in": 1,
            "fan_out": 2,
            "distance": 2,
            "score": 0.80,
            "similarity": 0.80,
            "role": "Service",
            "layer": "service",
            "source_id": "s2",
            "sink_id": "k2",
            "interprocedural": "return",
            "rule": "sql-injection",
            "severity": "medium",
        },
    ]


class TestDetectSecurityIssues:
    """Contract for ``detect_security_issues`` (REQ-MCP-007, SC-MCP-006)."""

    def test_taint_finding_has_rule_node_and_location(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path: a REACHES source->sink row yields a structured finding.

        SC-MCP-006 mandates at least one finding carrying ``rule``, ``node_id``
        and a ``file_path:line`` location, with interprocedural-edge evidence.
        """
        from mcp_server_omnicpg.tools.advanced_analysis import detect_security_issues

        _install(monkeypatch, _rich_rows())
        result = detect_security_issues(project_id="proj-x")

        findings = _findings(result)
        assert findings, f"expected at least one finding, got {result!r}"
        first = findings[0]
        assert first.get("rule"), "finding must carry a non-empty 'rule'"
        assert first.get("node_id") is not None, "finding must carry 'node_id'"
        located = first.get("location") or first.get("file_path")
        assert located, "finding must carry a file_path/line location"
        assert "argument" in str(result) or "interprocedural" in str(result), (
            "SC-MCP-006: a cross-procedural finding must surface its interprocedural edge evidence"
        )

    def test_hardcoded_secret_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: a hardcoded-secret literal yields a finding.

        SC-TAINT-004 requires the literal/hardcoded-secret class to be covered
        in addition to the dataflow (SQL-injection) class.
        """
        from mcp_server_omnicpg.tools.advanced_analysis import detect_security_issues

        secret_rows = [
            {
                "id": "lit1",
                "node_id": "lit1",
                "name": "password",
                "fqn": "com.x.Conf.PASSWORD",
                "file_path": "Conf.java",
                "line": 5,
                "code": 'String password = "p@ssw0rd";',
                "rule": "hardcoded-secret",
                "severity": "high",
            }
        ]
        _install(monkeypatch, secret_rows)
        result = detect_security_issues(project_id="proj-x")

        findings = _findings(result)
        assert findings, f"expected a hardcoded-secret finding, got {result!r}"
        rules = " ".join(str(f.get("rule", "")) for f in findings).lower()
        assert any(token in rules for token in ("secret", "password", "hardcoded")), (
            f"expected a secret-class rule, got rules={rules!r}"
        )
        assert all(f.get("node_id") is not None for f in findings)

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.advanced_analysis import detect_security_issues

        fake = _install(monkeypatch, _rich_rows())
        detect_security_issues(project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)

    def test_empty_graph_returns_no_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty path: no matching rows yields an empty finding collection."""
        from mcp_server_omnicpg.tools.advanced_analysis import detect_security_issues

        _install(monkeypatch, [])
        result = detect_security_issues(project_id="proj-x")

        assert _findings(result) == []

    def test_queries_java_security_roles_and_literal_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Java graphs use security_role and string literal node types."""
        import mcp_server_omnicpg.tools.advanced_analysis as advanced_analysis

        class _QueryCaptureAdapter:
            """Capture query text while returning an empty result set."""

            def __init__(self) -> None:
                """Initialise captured query storage."""
                self.queries: list[str] = []

            def ensure_connected(self) -> None:
                """No-op: the fake is always connected."""

            def query(self, query_string: str, **_params: Any) -> list[dict[str, Any]]:
                """Record Cypher text and return no rows."""
                self.queries.append(query_string)
                return []

        fake = _QueryCaptureAdapter()
        monkeypatch.setattr(advanced_analysis, "get_adapter", lambda: fake)

        advanced_analysis.detect_security_issues(project_id="proj-x")

        cypher = "\n".join(fake.queries)
        assert "src.security_role = 'source'" in cypher
        assert "snk.security_role = 'sink'" in cypher
        assert "PARENT_OF*0..6" in cypher
        assert "tainted.id AS tainted_id" in cypher
        assert "string_literal" in cypher
        assert "string_fragment" in cypher


class TestAnalyzeCodeComplexity:
    """Contract for ``analyze_code_complexity`` (SC-MCP-007)."""

    def test_ranks_methods_by_complexity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: methods are returned ranked by descending complexity."""
        from mcp_server_omnicpg.tools.advanced_analysis import analyze_code_complexity

        _install(monkeypatch, _rich_rows())
        result = analyze_code_complexity(top=20, project_id="proj-x")

        text = str(result)
        assert "m1" in text and "14" in text, (
            f"expected the high-complexity method to surface, got {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.advanced_analysis import analyze_code_complexity

        fake = _install(monkeypatch, _rich_rows())
        analyze_code_complexity(top=20, project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)

    def test_missing_complexity_yields_metric_source_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SC-MCP-007: when ``complexity`` is absent an explicit marker appears.

        Rows lacking ``complexity``/``mccabe`` must NOT produce an all-null
        result; the tool must surface ``metric_source`` (e.g. ``"approx"``),
        ``coverage`` or ``warning``.
        """
        from mcp_server_omnicpg.tools.advanced_analysis import analyze_code_complexity

        no_complexity = [{"id": "m1", "fqn": "com.x.A.doQuery", "file_path": "A.java"}]
        _install(monkeypatch, no_complexity)
        result = analyze_code_complexity(top=20, project_id="proj-x")

        assert _has_marker(result, {"metric_source", "coverage", "warning"}), (
            f"SC-MCP-007 marker missing from {result!r}"
        )


class TestFindDeadCode:
    """Contract for ``find_dead_code``."""

    def test_returns_unreferenced_methods(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: zero in-degree methods are reported with a reason."""
        from mcp_server_omnicpg.tools.advanced_analysis import find_dead_code

        rows = [
            {
                "id": "dead1",
                "fqn": "com.x.A.unused",
                "file_path": "A.java",
                "reason": "no incoming CALLS",
            }
        ]
        _install(monkeypatch, rows)
        result = find_dead_code(project_id="proj-x")

        assert "dead1" in str(result), f"dead method not surfaced: {result!r}"

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.advanced_analysis import find_dead_code

        fake = _install(monkeypatch, [])
        find_dead_code(project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)


class TestAnalyzeChangeImpact:
    """Contract for ``analyze_change_impact`` (reverse reachability)."""

    def test_groups_impacted_nodes_by_distance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: impacted nodes carry distances spanning multiple layers."""
        from mcp_server_omnicpg.tools.advanced_analysis import analyze_change_impact

        _install(monkeypatch, _rich_rows())
        result = analyze_change_impact(node_id="m0", project_id="proj-x")

        text = str(result)
        assert "m1" in text and "m2" in text, f"both impacted nodes must surface: {result!r}"
        # Two source rows carry distance 1 and 2 -> grouping must reflect both.
        assert "distance" in text or _has_marker(result, {"by_distance", "by_layer"}), (
            f"impact must be grouped/annotated by distance: {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.advanced_analysis import analyze_change_impact

        fake = _install(monkeypatch, _rich_rows())
        analyze_change_impact(node_id="m0", project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)

    def test_no_impact_returns_empty_collection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty path: an isolated node yields no impacted nodes."""
        from mcp_server_omnicpg.tools.advanced_analysis import analyze_change_impact

        _install(monkeypatch, [])
        result = analyze_change_impact(node_id="m0", project_id="proj-x")

        assert "m1" not in str(result)


class TestFindSimilarCode:
    """Contract for ``find_similar_code`` (structural fingerprint)."""

    def test_returns_scored_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: similar methods are returned with a similarity score."""
        from mcp_server_omnicpg.tools.advanced_analysis import find_similar_code

        _install(monkeypatch, _rich_rows())
        result = find_similar_code(node_id="m1", project_id="proj-x")

        assert _has_marker(result, {"score", "similarity"}) or "0.9" in str(result), (
            f"similarity score must be surfaced: {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.advanced_analysis import find_similar_code

        fake = _install(monkeypatch, _rich_rows())
        find_similar_code(node_id="m1", project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)


class TestGetArchitectureMetrics:
    """Contract for ``get_architecture_metrics`` (role/layer aggregation)."""

    def test_aggregates_layers_and_roles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: role/layer rows are aggregated into metrics."""
        from mcp_server_omnicpg.tools.advanced_analysis import get_architecture_metrics

        rows = [
            {"layer": "web", "role": "Controller", "count": 4},
            {"layer": "service", "role": "Service", "count": 9},
        ]
        _install(monkeypatch, rows)
        result = get_architecture_metrics(project_id="proj-x")

        assert _has_marker(result, {"layers", "role_counts", "layering_violations"}), (
            f"architecture metrics must expose layer/role aggregates: {result!r}"
        )

    def test_forwards_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_id scoping: the configured project id reaches the adapter."""
        from mcp_server_omnicpg.tools.advanced_analysis import get_architecture_metrics

        fake = _install(monkeypatch, [])
        get_architecture_metrics(project_id="proj-x")

        assert "proj-x" in _forwarded_project_ids(fake)

    def test_missing_enrichment_returns_coverage_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SC-MCP-007: empty role/layer enrichment yields a coverage marker."""
        from mcp_server_omnicpg.tools.advanced_analysis import get_architecture_metrics

        _install(monkeypatch, [])
        result = get_architecture_metrics(project_id="proj-x")

        assert _has_marker(result, {"coverage", "warning"}), (
            f"empty enrichment must surface coverage/warning: {result!r}"
        )


class TestAdvancedAnalysisViaCallTool:
    """``call_tool`` must dispatch the advanced tools and inject ``PROJECT_ID``."""

    def test_call_tool_dispatches_and_injects_project_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """call_tool: dispatch ``detect_security_issues`` and scope by project.

        Fails now because ``call_tool`` replies ``"Unknown tool: ..."``. Once
        registered, the configured ``Config.PROJECT_ID`` must be forwarded into
        the adapter (REQ-SCHEMA-006).
        """
        fake = _install(monkeypatch, _rich_rows())
        monkeypatch.setattr(Config, "PROJECT_ID", "proj-call")

        result = asyncio.run(server.call_tool("detect_security_issues", {}))
        text = result[0].text

        assert not text.startswith("Unknown tool"), (
            f"detect_security_issues is not dispatched by call_tool: {text!r}"
        )
        assert "proj-call" in _forwarded_project_ids(fake), (
            f"call_tool must inject Config.PROJECT_ID; captured={fake.calls}"
        )
