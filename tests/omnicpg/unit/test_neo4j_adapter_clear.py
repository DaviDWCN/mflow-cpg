"""Unit tests for Neo4j adapter clearing behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from omnicpg.adapters.neo4j_adapter import Neo4jAdapter


class _FakeResult:
    def __init__(self, deleted: int) -> None:
        self._deleted = deleted

    def single(self) -> dict[str, int]:
        return {"deleted": self._deleted}


class _FakeSession:
    def __init__(self, deleted_counts: list[int]) -> None:
        self._deleted_counts = deleted_counts
        self.queries: list[str] = []
        self.params: list[dict[str, object]] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None

    def run(self, query: str, **params: object) -> _FakeResult:
        self.queries.append(query)
        self.params.append(params)
        return _FakeResult(self._deleted_counts.pop(0))


def test_clear_deletes_relationships_before_nodes_in_small_batches() -> None:
    """Full clear avoids large DETACH DELETE transactions."""
    fake_session = _FakeSession([3, 0, 2, 0])
    fake_driver = Mock()
    fake_driver.session.return_value = fake_session

    adapter = Neo4jAdapter(batch_size=10)
    adapter._driver = fake_driver

    adapter.clear()

    statements = "\n".join(fake_session.queries)
    assert "DETACH DELETE" not in statements
    assert "MATCH ()-[r]->()" in fake_session.queries[0]
    assert "DELETE r" in fake_session.queries[0]
    assert "MATCH (n)" in fake_session.queries[2]
    assert "DELETE n" in fake_session.queries[2]
    assert all(params["batch_size"] == 10000 for params in fake_session.params)
