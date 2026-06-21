"""Unit tests for CPGNode and CPGEdge domain models."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode

# ── CPGNode ───────────────────────────────────────────────────────────────────


class TestCPGNode:
    """Tests for :class:`CPGNode`."""

    def test_create_basic_node(self) -> None:
        """A node can be created with id and labels."""
        node = CPGNode(id="abc-123", labels=("Node", "Method"))
        assert node.id == "abc-123"
        assert node.labels == ("Node", "Method")
        assert dict(node.properties) == {}

    def test_create_node_with_properties(self) -> None:
        """A node can carry arbitrary properties."""
        props = MappingProxyType({"type": "function_definition", "name": "foo"})
        node = CPGNode(id="n1", labels=("Node",), properties=props)
        assert node.properties["type"] == "function_definition"
        assert node.properties["name"] == "foo"

    def test_frozen_node_is_immutable(self) -> None:
        """Attempting to mutate a frozen dataclass raises an error."""
        node = CPGNode(id="n1", labels=("Node",))
        with pytest.raises(AttributeError):
            node.id = "changed"  # type: ignore[misc]

    def test_empty_id_raises(self) -> None:
        """An empty ``id`` is rejected at construction time."""
        with pytest.raises(ValueError, match="non-empty"):
            CPGNode(id="", labels=("Node",))

    def test_empty_labels_raises(self) -> None:
        """Empty ``labels`` is rejected at construction time."""
        with pytest.raises(ValueError, match="at least one label"):
            CPGNode(id="n1", labels=())

    def test_dict_properties_coerced_to_mapping_proxy(self) -> None:
        """A plain dict passed as properties is auto-wrapped."""
        node = CPGNode(id="n1", labels=("Node",), properties={"k": "v"})  # type: ignore[arg-type]
        assert isinstance(node.properties, MappingProxyType)
        assert node.properties["k"] == "v"

    def test_with_properties_returns_new_node(self) -> None:
        """``with_properties`` creates a copy, not a mutation."""
        original = CPGNode(id="n1", labels=("Node",), properties=MappingProxyType({"a": 1}))
        updated = original.with_properties(b=2)
        assert updated.properties["a"] == 1
        assert updated.properties["b"] == 2
        assert "b" not in original.properties

    def test_has_label(self) -> None:
        """``has_label`` returns correct boolean."""
        node = CPGNode(id="n1", labels=("Node", "Method"))
        assert node.has_label("Method") is True
        assert node.has_label("Class") is False


# ── CPGEdge ───────────────────────────────────────────────────────────────────


class TestCPGEdge:
    """Tests for :class:`CPGEdge`."""

    def test_create_basic_edge(self) -> None:
        """An edge links two nodes with a typed relationship."""
        edge = CPGEdge(source_id="a", target_id="b", edge_type=EdgeType.PARENT_OF)
        assert edge.source_id == "a"
        assert edge.target_id == "b"
        assert edge.edge_type == EdgeType.PARENT_OF

    def test_frozen_edge_is_immutable(self) -> None:
        """Attempting to mutate a frozen edge raises an error."""
        edge = CPGEdge(source_id="a", target_id="b", edge_type=EdgeType.FLOWS_TO)
        with pytest.raises(AttributeError):
            edge.source_id = "changed"  # type: ignore[misc]

    def test_empty_source_raises(self) -> None:
        """An empty ``source_id`` is rejected."""
        with pytest.raises(ValueError, match="source_id"):
            CPGEdge(source_id="", target_id="b", edge_type=EdgeType.PARENT_OF)

    def test_empty_target_raises(self) -> None:
        """An empty ``target_id`` is rejected."""
        with pytest.raises(ValueError, match="target_id"):
            CPGEdge(source_id="a", target_id="", edge_type=EdgeType.PARENT_OF)

    def test_invalid_edge_type_raises(self) -> None:
        """A non-EdgeType string is rejected."""
        with pytest.raises(TypeError, match="EdgeType"):
            CPGEdge(source_id="a", target_id="b", edge_type="INVALID")  # type: ignore[arg-type]

    def test_edge_with_properties(self) -> None:
        """An edge can carry extra properties (e.g. condition)."""
        edge = CPGEdge(
            source_id="a",
            target_id="b",
            edge_type=EdgeType.FLOWS_TO,
            properties=MappingProxyType({"condition": "True"}),
        )
        assert edge.properties["condition"] == "True"

    def test_edge_type_values(self) -> None:
        """All expected EdgeType members exist."""
        assert EdgeType.PARENT_OF == "PARENT_OF"
        assert EdgeType.FLOWS_TO == "FLOWS_TO"
        assert EdgeType.REACHES == "REACHES"
        assert EdgeType.CALLS == "CALLS"
        assert EdgeType.CONTAINS == "CONTAINS"
        assert EdgeType.DEPENDS_ON == "DEPENDS_ON"
        assert EdgeType.IMPLEMENTS_CONCEPT == "IMPLEMENTS_CONCEPT"
        assert EdgeType.IMPLEMENTS == "IMPLEMENTS"
        assert EdgeType.TESTS == "TESTS"
