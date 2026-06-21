"""Unit tests for the ExpansionCache."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnicpg.cache.expansion_cache import ExpansionCache, SupportsMethodExpansion
from omnicpg.interfaces.language_plugin import LanguagePlugin
from omnicpg.models.edge import CPGEdge
from omnicpg.models.node import CPGNode
from omnicpg.slicer.code_slicer import CodeSlicer


@pytest.fixture
def mock_neo4j_adapter() -> MagicMock:
    """Provide a mock SupportsMethodExpansion adapter."""
    return MagicMock(spec=SupportsMethodExpansion)


@pytest.fixture
def mock_slicer() -> MagicMock:
    """Provide a mock CodeSlicer."""
    return MagicMock(spec=CodeSlicer)


@pytest.fixture
def mock_plugin() -> MagicMock:
    """Provide a mock LanguagePlugin."""
    return MagicMock(spec=LanguagePlugin)


@pytest.fixture
def expansion_cache(mock_neo4j_adapter: MagicMock) -> ExpansionCache:
    """Provide an ExpansionCache instance."""
    return ExpansionCache(neo4j_adapter=mock_neo4j_adapter)


def test_get_expanded_method_cache_miss(
    expansion_cache: ExpansionCache,
    mock_slicer: MagicMock,
    mock_plugin: MagicMock,
    mock_neo4j_adapter: MagicMock,
) -> None:
    """Test get_expanded_method when the method is not in cache."""
    # Setup mock return value
    mock_nodes = [MagicMock(spec=CPGNode)]
    mock_edges = [MagicMock(spec=CPGEdge)]
    mock_slicer.expand_method_to_neo4j.return_value = (mock_nodes, mock_edges)

    # Call get_expanded_method
    nodes, edges = expansion_cache.get_expanded_method("method_1", mock_slicer, mock_plugin)

    # Verify calls and returns
    mock_slicer.expand_method_to_neo4j.assert_called_once_with(
        "method_1", mock_plugin, mock_neo4j_adapter
    )
    assert nodes == mock_nodes
    assert edges == mock_edges

    # Verify it is now in cache
    assert "method_1" in expansion_cache._cache
    assert expansion_cache._cache["method_1"] == (mock_nodes, mock_edges)


def test_get_expanded_method_cache_hit(
    expansion_cache: ExpansionCache,
    mock_slicer: MagicMock,
    mock_plugin: MagicMock,
) -> None:
    """Test get_expanded_method when the method is already in cache."""
    # Populate cache manually
    mock_nodes: list[CPGNode] = [MagicMock(spec=CPGNode)]
    mock_edges: list[CPGEdge] = [MagicMock(spec=CPGEdge)]
    expansion_cache._cache["method_1"] = (mock_nodes, mock_edges)

    # Call get_expanded_method
    nodes, edges = expansion_cache.get_expanded_method("method_1", mock_slicer, mock_plugin)

    # Verify expand_method_to_neo4j was not called
    mock_slicer.expand_method_to_neo4j.assert_not_called()

    # Verify returns
    assert nodes == mock_nodes
    assert edges == mock_edges


def test_batch_expand_methods(
    expansion_cache: ExpansionCache,
    mock_slicer: MagicMock,
    mock_plugin: MagicMock,
    mock_neo4j_adapter: MagicMock,
) -> None:
    """Test batch_expand_methods correctly caches new methods and skips existing ones."""
    # Setup cache and mocks
    mock_nodes: list[CPGNode] = [MagicMock(spec=CPGNode)]
    mock_edges: list[CPGEdge] = [MagicMock(spec=CPGEdge)]

    # 1. Method in memory cache
    expansion_cache._cache["method_1"] = (mock_nodes, mock_edges)

    # 2. Method in neo4j database
    def check_expanded_side_effect(method_id: str) -> bool:
        return method_id == "method_2"

    mock_neo4j_adapter.check_method_expanded.side_effect = check_expanded_side_effect

    # 3. Method to be expanded
    mock_slicer.expand_method_to_neo4j.return_value = (mock_nodes, mock_edges)

    # Call batch_expand_methods
    method_ids = ["method_1", "method_2", "method_3"]
    results = expansion_cache.batch_expand_methods(method_ids, mock_slicer, mock_plugin)

    # Verify returns
    assert "method_1" not in results  # Skipped
    assert "method_2" not in results  # Skipped
    assert "method_3" in results  # Expanded
    assert results["method_3"] == (mock_nodes, mock_edges)

    # Verify slicer calls
    mock_slicer.expand_method_to_neo4j.assert_called_once_with(
        "method_3", mock_plugin, mock_neo4j_adapter
    )

    # Verify cache state
    assert "method_3" in expansion_cache._cache


def test_preload_hot_methods_default(
    expansion_cache: ExpansionCache,
    mock_slicer: MagicMock,
    mock_plugin: MagicMock,
    mock_neo4j_adapter: MagicMock,
) -> None:
    """Test preload_hot_methods with default criteria."""
    # Setup mock query results
    mock_neo4j_adapter.query.return_value = [{"id": "method_1"}, {"id": "method_2"}]

    # Setup slicer
    mock_nodes: list[CPGNode] = [MagicMock(spec=CPGNode)]
    mock_edges: list[CPGEdge] = [MagicMock(spec=CPGEdge)]
    mock_slicer.expand_method_to_neo4j.return_value = (mock_nodes, mock_edges)

    # Database check shouldn't skip these, let's say they're not expanded
    mock_neo4j_adapter.check_method_expanded.return_value = False

    # Call preload_hot_methods
    expansion_cache.preload_hot_methods(mock_slicer, mock_plugin, limit=2)

    # Verify query
    mock_neo4j_adapter.query.assert_called_once()
    query_called_with = mock_neo4j_adapter.query.call_args[0][0]
    assert "MATCH (m:Method)" in query_called_with
    assert "WHERE m.expanded IS NULL OR m.expanded = false" in query_called_with

    # Verify methods were expanded and cached
    assert "method_1" in expansion_cache._cache
    assert "method_2" in expansion_cache._cache
    assert mock_slicer.expand_method_to_neo4j.call_count == 2


def test_preload_hot_methods_custom_pattern(
    expansion_cache: ExpansionCache,
    mock_slicer: MagicMock,
    mock_plugin: MagicMock,
    mock_neo4j_adapter: MagicMock,
) -> None:
    """Test preload_hot_methods with custom name pattern."""
    # Setup mock query results
    mock_neo4j_adapter.query.return_value = [{"id": "method_1"}]
    mock_slicer.expand_method_to_neo4j.return_value = ([], [])
    mock_neo4j_adapter.check_method_expanded.return_value = False

    # Call preload_hot_methods
    criteria = {"name_pattern": "process_*"}
    expansion_cache.preload_hot_methods(mock_slicer, mock_plugin, criteria=criteria, limit=5)

    # Verify query
    mock_neo4j_adapter.query.assert_called_once()
    query_called = mock_neo4j_adapter.query.call_args[0][0]
    kwargs = mock_neo4j_adapter.query.call_args[1]

    assert "STARTS WITH $pattern" in query_called
    assert kwargs.get("pattern") == "process_*"
    assert kwargs.get("limit") == 5

    # Verify cache
    assert "method_1" in expansion_cache._cache


def test_preload_hot_methods_unknown_criteria(
    expansion_cache: ExpansionCache,
    mock_slicer: MagicMock,
    mock_plugin: MagicMock,
    mock_neo4j_adapter: MagicMock,
) -> None:
    """Test preload_hot_methods with unknown criteria."""
    # Call preload_hot_methods with unknown criteria
    criteria = {"unknown_key": "value"}
    expansion_cache.preload_hot_methods(mock_slicer, mock_plugin, criteria=criteria)

    # Verify query not called
    mock_neo4j_adapter.query.assert_not_called()
    mock_slicer.expand_method_to_neo4j.assert_not_called()


def test_clear_cache(expansion_cache: ExpansionCache) -> None:
    """Test clear_cache clears memory cache."""
    # Populate cache
    expansion_cache._cache["method_1"] = ([], [])
    assert len(expansion_cache._cache) == 1

    # Clear cache
    expansion_cache.clear_cache()

    # Verify
    assert len(expansion_cache._cache) == 0


def test_get_cache_stats(
    expansion_cache: ExpansionCache,
    mock_neo4j_adapter: MagicMock,
) -> None:
    """Test get_cache_stats returns correct info."""
    # Setup state
    expansion_cache._cache["m1"] = ([], [])
    expansion_cache._cache["m2"] = ([], [])
    expansion_cache._pending_expansions.add("m3")

    # Setup db query
    mock_neo4j_adapter.query.return_value = [{"expanded_count": 5}]

    # Call get_cache_stats
    stats = expansion_cache.get_cache_stats()

    # Verify
    assert stats["memory_cache_size"] == 2
    assert stats["expanded_in_db"] == 5
    assert stats["pending_expansions"] == 1


def test_invalidate_method(expansion_cache: ExpansionCache) -> None:
    """Test invalidate_method removes specific method from cache."""
    # Populate cache
    expansion_cache._cache["method_1"] = ([], [])
    expansion_cache._cache["method_2"] = ([], [])

    # Invalidate method_1
    expansion_cache.invalidate_method("method_1")

    # Verify
    assert "method_1" not in expansion_cache._cache
    assert "method_2" in expansion_cache._cache

    # Invalidating a method not in cache shouldn't error
    expansion_cache.invalidate_method("method_3")
