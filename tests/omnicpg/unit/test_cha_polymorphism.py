"""Unit tests for virtual CHA polymorphism resolution and DFG edge materialization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from omnicpg.orchestrator.graph_enrichment import materialize_cha_polymorphism_edges

if TYPE_CHECKING:
    from omnicpg.adapters.neo4j_adapter import Neo4jAdapter


class PolymorphismFakeAdapter:
    """Mock adapter capturing Cypher queries and returning mock results."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[dict[str, Any]] = []

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append(cypher)
        self.params.append(params)

        # 1. Match the initial override matching query
        if "MATCH (caller:Method)-[c:CALLS]->(callee:Method)" in cypher:
            return [
                {
                    "caller_id": "caller-1",
                    "callee_name": "process",
                    "callsite_id": "callsite-1",
                    "override_id": "override-1",
                }
            ]

        # 2. Match the argument retrieval query
        if "MATCH (callsite:Node {id: $callsite_id})-[:PARENT_OF]->(arg_list:Node {type: 'argument_list'})" in cypher:
            return [
                {"id": "arg-1", "line_start": 5, "column_start": 20},
            ]

        # 3. Match the parameter retrieval query
        if 'WHERE "Parameter" IN labels(param) OR param.type = \'formal_parameter\'' in cypher:
            return [
                {"id": "param-1", "name": "inputData", "line_start": 10, "column_start": 25},
            ]

        # 4. Match the return retrieval query
        if 'WHERE "Return" IN labels(ret)' in cypher:
            return [
                {"id": "return-1"},
            ]

        # Defaults for other updates
        return []


def test_materialize_cha_polymorphism_edges() -> None:
    """Polymorphism resolution should generate virtual CALLS and REACHES edges."""
    adapter = PolymorphismFakeAdapter()
    summary = materialize_cha_polymorphism_edges(cast("Neo4jAdapter", adapter), "proj-test")

    # Verify summary results
    assert summary["virtual_calls_created"] == 1
    assert summary["virtual_reaches_created"] == 2  # 1 argument -> parameter, 1 return -> callsite

    # Verify query patterns were executed
    cypher_text = "\n".join(adapter.queries)
    assert 'r.resolution = "cha_polymorphism"' in cypher_text
    assert 'r.virtual = true' in cypher_text
    assert "r.interprocedural = \"argument\"" in cypher_text
    assert "r.interprocedural = \"return\"" in cypher_text
