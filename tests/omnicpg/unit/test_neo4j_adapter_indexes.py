"""Unit tests for Neo4j adapter index management."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from omnicpg.adapters.neo4j_adapter import Neo4jAdapter


class _FakeResult:
    def consume(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None

    def run(self, query: str, **_params: object) -> _FakeResult:
        self.queries.append(query)
        return _FakeResult()


def test_ensure_constraints_creates_project_and_graph_health_indexes() -> None:
    """Adapter indexes cover project-scoped and Java V2 health queries."""
    fake_session = _FakeSession()
    fake_driver = Mock()
    fake_driver.session.return_value = fake_session

    adapter = Neo4jAdapter(batch_size=10)
    adapter._driver = fake_driver

    adapter._ensure_constraints()

    statements = "\n".join(fake_session.queries)
    assert "node_project_id_index" in statements
    assert "node_project_type_index" in statements
    assert "node_project_name_index" in statements
    assert "node_project_file_path_index" in statements
    assert "node_project_fqn_index" in statements
    assert "calls_project_id_index" in statements
    assert "reaches_project_id_index" in statements
    assert "contains_project_id_index" in statements
    assert "calls_callsite_id_index" in statements
    assert "calls_resolution_index" in statements
    assert "reaches_interprocedural_index" in statements


def test_drop_secondary_indexes_includes_project_and_relationship_indexes() -> None:
    """Bulk-load index drop uses the same secondary-index set."""
    fake_session = _FakeSession()
    fake_driver = Mock()
    fake_driver.session.return_value = fake_session

    adapter = Neo4jAdapter(batch_size=10)
    adapter._driver = fake_driver

    adapter.drop_secondary_indexes()

    statements = "\n".join(fake_session.queries)
    assert "DROP INDEX node_project_id_index IF EXISTS" in statements
    assert "DROP INDEX calls_project_id_index IF EXISTS" in statements
    assert "DROP INDEX reaches_project_id_index IF EXISTS" in statements
    assert "DROP INDEX calls_callsite_id_index IF EXISTS" in statements


def test_ensure_architectural_indexes_replaces_legacy_callsite_range_index() -> None:
    """Architectural index setup drops legacy RANGE index on CallSite.code."""
    fake_session = _FakeSession()
    fake_driver = Mock()
    fake_driver.session.return_value = fake_session

    adapter = Neo4jAdapter(batch_size=10)
    adapter._driver = fake_driver

    adapter.ensure_architectural_indexes()

    statements = "\n".join(fake_session.queries)
    assert "DROP INDEX idx_callsite_code IF EXISTS" in statements
    assert "idx_callsite_code_fulltext" in statements
    assert "FOR (n:CallSite) ON EACH [n.code]" in statements
