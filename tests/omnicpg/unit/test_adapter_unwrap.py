"""Unit tests for the APOC read-path unwrapping in ``MCPNeo4jAdapter.query``.

These tests encode plan item **P0** (the adapter unwrap bug). They simulate
neo4j ``Record`` semantics — where ``__contains__`` checks *values* rather than
*keys* — to prove that ``MCPNeo4jAdapter.query`` must return *flat* row dicts
(e.g. ``{"id": "x1", "name": "foo"}``) and never the APOC ``{"value": {...}}``
wrapper.

They run WITHOUT a live Neo4j by injecting a fake driver/session that yields
fake records.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp_server_omnicpg.neo4j_adapter import MCPNeo4jAdapter


class _FakeRecord:
    """A stand-in for ``neo4j.Record`` whose membership test checks values.

    The real ``neo4j.Record.__contains__`` returns ``True`` only when the
    argument is one of the record's *values*, not one of its *keys*. This class
    reproduces that exact behaviour so the adapter's unwrap logic is exercised
    faithfully.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        """Store the backing mapping for this fake record."""
        self._data = data

    def keys(self) -> list[str]:
        """Return the record keys (mirrors ``neo4j.Record.keys``)."""
        return list(self._data.keys())

    def values(self) -> list[Any]:
        """Return the record values (mirrors ``neo4j.Record.values``)."""
        return list(self._data.values())

    def __getitem__(self, key: str) -> Any:
        """Return the value for ``key`` so ``dict(record)`` works."""
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        """Return ``self._data[key]`` or ``default`` (mirrors ``Record.get``)."""
        return self._data.get(key, default)

    def __contains__(self, item: Any) -> bool:
        """Check membership against VALUES, like the real ``neo4j.Record``."""
        return item in self._data.values()

    def __iter__(self) -> Any:
        """Iterate over values, like the real ``neo4j.Record``."""
        return iter(self._data.values())


class _FakeSession:
    """A fake Neo4j session that returns a fixed list of fake records."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        """Store the records this session will yield from ``run``."""
        self._records = records

    def __enter__(self) -> _FakeSession:
        """Enter the ``with`` context, returning self."""
        return self

    def __exit__(self, *exc: object) -> bool:
        """Exit the ``with`` context without suppressing exceptions."""
        return False

    def run(self, query: str, **params: Any) -> list[_FakeRecord]:
        """Return the canned records regardless of the query/params."""
        return list(self._records)


class _FakeDriver:
    """A fake Neo4j driver handing out :class:`_FakeSession` instances."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        """Store the records every session produced by this driver will yield."""
        self._records = records

    def session(self) -> _FakeSession:
        """Return a fresh fake session bound to the canned records."""
        return _FakeSession(self._records)


class TestAdapterUnwrap:
    """P0: ``MCPNeo4jAdapter.query`` must unwrap APOC ``value`` rows flatly."""

    def test_query_returns_flat_rows_not_value_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P0: a single ``value``-keyed APOC record must unwrap to a flat dict.

        Current (buggy) behaviour: ``"value" not in record`` is always True
        because ``Record.__contains__`` checks values, so the adapter appends
        ``dict(record)`` == ``{"value": {<row>}}`` instead of unwrapping. This
        assertion therefore FAILS until the adapter checks keys, not values.
        """
        # APOC returns rows shaped as {"value": <real row>}.
        real_row = {"id": "x1", "name": "foo"}
        records = [_FakeRecord({"value": real_row})]
        fake_driver = _FakeDriver(records)

        adapter = MCPNeo4jAdapter()
        monkeypatch.setattr(adapter, "_driver", fake_driver)
        monkeypatch.setattr(adapter, "_connected", True)

        rows = adapter.query("MATCH (n) RETURN n.id AS id, n.name AS name")

        assert rows == [real_row]
        assert "value" not in rows[0]
        assert rows[0]["id"] == "x1"
        assert rows[0]["name"] == "foo"

    def test_query_unwraps_multiple_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P0: every APOC ``value`` row in a multi-row result must be flattened.

        Confirms the fix applies uniformly across all returned records, not just
        the first. FAILS now because each record is wrapped as ``{"value": ...}``.
        """
        rows_in = [
            {"id": "a", "name": "alpha"},
            {"id": "b", "name": "beta"},
        ]
        records = [_FakeRecord({"value": r}) for r in rows_in]
        fake_driver = _FakeDriver(records)

        adapter = MCPNeo4jAdapter()
        monkeypatch.setattr(adapter, "_driver", fake_driver)
        monkeypatch.setattr(adapter, "_connected", True)

        rows = adapter.query("MATCH (n) RETURN n.id AS id, n.name AS name")

        assert rows == rows_in
        assert all("value" not in row for row in rows)
