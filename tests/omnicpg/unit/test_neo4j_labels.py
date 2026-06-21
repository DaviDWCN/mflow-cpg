"""Unit tests for Neo4j semantic label enrichment.

These tests verify the ``_enrich_labels`` helper and the
``_SEMANTIC_LABEL_MAP`` without requiring a running Neo4j instance.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any
from unittest.mock import Mock

from neo4j.exceptions import ClientError

from omnicpg.adapters.neo4j_adapter import (
    _SEMANTIC_LABEL_MAP,
    Neo4jAdapter,
    _enrich_labels,
)
from omnicpg.models.node import CPGNode


class TestEnrichLabels:
    """Tests for :func:`_enrich_labels`."""

    def test_function_definition_gets_function_label(self) -> None:
        """A ``function_definition`` node should receive an extra ``Function`` label."""
        node = CPGNode(
            id="n1",
            labels=("Node", "Method"),
            properties=MappingProxyType({"type": "function_definition", "name": "foo"}),
        )
        labels = _enrich_labels(node)
        assert "Function" in labels
        assert "Method" in labels
        assert "Node" in labels

    def test_class_definition_keeps_class_label(self) -> None:
        """A ``class_definition`` node already has ``Class`` — no duplicate should appear."""
        node = CPGNode(
            id="n2",
            labels=("Node", "Class"),
            properties=MappingProxyType({"type": "class_definition", "name": "MyClass"}),
        )
        labels = _enrich_labels(node)
        # "Class" already present — should not be duplicated.
        assert labels.count("Class") == 1
        assert "Node" in labels

    def test_call_node_gets_callsite_label(self) -> None:
        """A ``call`` node should receive ``CallSite`` label."""
        node = CPGNode(
            id="n3",
            labels=("Node",),
            properties=MappingProxyType({"type": "call", "code": "foo()"}),
        )
        labels = _enrich_labels(node)
        assert "CallSite" in labels

    def test_method_invocation_gets_callsite_label(self) -> None:
        """A ``method_invocation`` node should receive ``CallSite`` label."""
        node = CPGNode(
            id="n4",
            labels=("Node",),
            properties=MappingProxyType({"type": "method_invocation", "code": "obj.foo()"}),
        )
        labels = _enrich_labels(node)
        assert "CallSite" in labels

    def test_identifier_gets_identifier_label(self) -> None:
        """An ``identifier`` node should receive ``Identifier`` label."""
        node = CPGNode(
            id="n5",
            labels=("Node", "Variable"),
            properties=MappingProxyType({"type": "identifier", "code": "x"}),
        )
        labels = _enrich_labels(node)
        assert "Identifier" in labels
        assert "Variable" in labels

    def test_import_from_statement_gets_import_label(self) -> None:
        """An ``import_from_statement`` node already has ``Import`` — no duplicate."""
        node = CPGNode(
            id="n6",
            labels=("Node", "Import"),
            properties=MappingProxyType(
                {"type": "import_from_statement", "code": "from os import path"}
            ),
        )
        labels = _enrich_labels(node)
        assert labels.count("Import") == 1

    def test_module_gets_module_label(self) -> None:
        """A ``module`` node already has ``Module`` — no duplicate."""
        node = CPGNode(
            id="n7",
            labels=("Node", "Module"),
            properties=MappingProxyType({"type": "module"}),
        )
        labels = _enrich_labels(node)
        assert labels.count("Module") == 1

    def test_unknown_type_unchanged(self) -> None:
        """A node with an unmapped ``type`` should keep its original labels only."""
        node = CPGNode(
            id="n8",
            labels=("Node",),
            properties=MappingProxyType({"type": "expression_statement"}),
        )
        labels = _enrich_labels(node)
        assert labels == ["Node"]

    def test_node_without_type_unchanged(self) -> None:
        """A node without a ``type`` property should keep its original labels."""
        node = CPGNode(
            id="n9",
            labels=("Node", "File"),
            properties=MappingProxyType({"name": "test.py"}),
        )
        labels = _enrich_labels(node)
        assert labels == ["Node", "File"]

    def test_method_declaration_gets_function_label(self) -> None:
        """A Java ``method_declaration`` node should receive ``Function`` label."""
        node = CPGNode(
            id="n10",
            labels=("Node", "Method"),
            properties=MappingProxyType({"type": "method_declaration", "name": "doSomething"}),
        )
        labels = _enrich_labels(node)
        assert "Function" in labels
        assert "Method" in labels


class TestSemanticLabelMap:
    """Tests for the :data:`_SEMANTIC_LABEL_MAP` dictionary."""

    def test_all_expected_mappings_exist(self) -> None:
        """All type→label mappings from the specification should be present."""
        expected = {
            "function_definition": "Function",
            "method_declaration": "Function",
            "class_definition": "Class",
            "class_declaration": "Class",
            "module": "Module",
            "call": "CallSite",
            "method_invocation": "CallSite",
            "import_statement": "Import",
            "import_from_statement": "Import",
            "import_declaration": "Import",
            "identifier": "Identifier",
        }
        for ts_type, label in expected.items():
            assert _SEMANTIC_LABEL_MAP[ts_type] == label, (
                f"Expected {ts_type!r} → {label!r}, got {_SEMANTIC_LABEL_MAP.get(ts_type)!r}"
            )

    def test_map_values_are_valid_identifiers(self) -> None:
        """All mapped labels must be valid Neo4j label identifiers."""
        for label in _SEMANTIC_LABEL_MAP.values():
            assert label.isidentifier(), f"{label!r} is not a valid identifier"


class TestNeo4jLabelFallback:
    """Tests for conflict-tolerant Neo4j label application."""

    def test_insert_nodes_skips_conflicting_labels(self) -> None:
        """A label conflict should not abort node insertion for the whole batch."""

        class _FakeResult:
            def consume(self) -> None:
                return None

        class _FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []
                self.batch_labels_attempted = False

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
                self.calls.append((query, params))
                if "CALL apoc.create.addLabels(node, n.labels)" in query:
                    self.batch_labels_attempted = True
                    raise ClientError(
                        "Failed to invoke procedure `apoc.create.addLabels`: "
                        "Caused by: IndexEntryConflictException"
                    )
                return _FakeResult()

        fake_session = _FakeSession()
        fake_driver = Mock()
        fake_driver.session.return_value = fake_session

        adapter = Neo4jAdapter(batch_size=10)
        adapter._driver = fake_driver

        node = CPGNode(
            id="node-1",
            labels=("Node", "Class"),
            properties=MappingProxyType(
                {
                    "type": "class_definition",
                    "name": "_FakeAdapter",
                    "file_path": r"D:\workspace\OmniCPG\tests\integration\test_mcp_tools.py",
                }
            ),
        )

        adapter.insert_nodes([node])

        assert fake_session.batch_labels_attempted is True
        single_label_calls = [
            params for query, params in fake_session.calls if "[$label]" in query
        ]
        assert single_label_calls == [{"id": "node-1", "label": "Class"}]

    def test_insert_nodes_drops_legacy_callsite_range_index_on_oversized_code(self) -> None:
        """Oversized CallSite.code should trigger legacy index drop and batch retry."""

        class _FakeResult:
            def consume(self) -> None:
                return None

        class _FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []
                self.batch_label_attempts = 0

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
                self.calls.append((query, params))
                if "CALL apoc.create.addLabels(node, n.labels)" in query:
                    self.batch_label_attempts += 1
                    if self.batch_label_attempts == 1:
                        raise ClientError(
                            "Failed to invoke procedure `apoc.create.addLabels`: "
                            "Caused by: java.lang.IllegalArgumentException: "
                            "Property value is too large to index. "
                            "Index: Index(name='idx_callsite_code')"
                        )
                return _FakeResult()

        fake_session = _FakeSession()
        fake_driver = Mock()
        fake_driver.session.return_value = fake_session

        adapter = Neo4jAdapter(batch_size=10)
        adapter._driver = fake_driver

        node = CPGNode(
            id="call-1",
            labels=("Node",),
            properties=MappingProxyType({"type": "call", "code": "x" * 20000}),
        )

        adapter.insert_nodes([node])

        drop_calls = [
            query
            for query, _params in fake_session.calls
            if "DROP INDEX idx_callsite_code" in query
        ]
        assert len(drop_calls) == 1
        assert fake_session.batch_label_attempts == 2


# ── Neo4j-safe property serialization ────────────────────────────────────


class TestNeo4jSafeProperties:
    """Tests for :func:`_neo4j_safe_properties`."""

    def test_tuples_converted_to_lists(self) -> None:
        """Tuple values are converted to lists."""
        from omnicpg.adapters.neo4j_adapter import _neo4j_safe_properties

        result = _neo4j_safe_properties({"param_names": ("a", "b", "c")})
        assert result["param_names"] == ["a", "b", "c"]
        assert isinstance(result["param_names"], list)

    def test_empty_tuple_converted_to_empty_list(self) -> None:
        """Empty tuples are converted to empty lists."""
        from omnicpg.adapters.neo4j_adapter import _neo4j_safe_properties

        result = _neo4j_safe_properties({"decorators": ()})
        assert result["decorators"] == []
        assert isinstance(result["decorators"], list)

    def test_mapping_proxy_converted_to_dict(self) -> None:
        """MappingProxyType values are converted to plain dicts."""
        from omnicpg.adapters.neo4j_adapter import _neo4j_safe_properties

        result = _neo4j_safe_properties({"meta": MappingProxyType({"k": "v"})})
        assert result["meta"] == {"k": "v"}
        assert isinstance(result["meta"], dict)

    def test_plain_values_unchanged(self) -> None:
        """Strings, ints, bools, None are passed through unchanged."""
        from omnicpg.adapters.neo4j_adapter import _neo4j_safe_properties

        props = {"name": "foo", "line_start": 1, "is_async": False, "ret": None}
        result = _neo4j_safe_properties(props)
        assert result == props

    def test_nested_tuples_recursively_converted(self) -> None:
        """Nested tuples inside lists are also converted."""
        from omnicpg.adapters.neo4j_adapter import _neo4j_safe_properties

        result = _neo4j_safe_properties({"data": (("a", "b"), ("c",))})
        assert result["data"] == [["a", "b"], ["c"]]

    def test_mixed_properties(self) -> None:
        """A realistic mix of CPG node properties is handled correctly."""
        from omnicpg.adapters.neo4j_adapter import _neo4j_safe_properties

        props = {
            "type": "function_definition",
            "name": "process",
            "param_names": ("self", "data"),
            "decorators": (),
            "return_type": "str",
            "complexity": 3,
            "is_async": True,
        }
        result = _neo4j_safe_properties(props)
        assert result["param_names"] == ["self", "data"]
        assert result["decorators"] == []
        assert result["return_type"] == "str"
        assert result["complexity"] == 3
        assert result["is_async"] is True
