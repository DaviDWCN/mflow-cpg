"""Unit tests for cross-file inheritance edge materialization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

from omnicpg.orchestrator.graph_enrichment import (
    _build_indexes,
    _classify_role,
    _resolve_base,
    classify_architectural_roles,
    enrich_semantic_intent,
    materialize_inheritance_edges,
    materialize_java_parameter_reaches_edges,
)

if TYPE_CHECKING:
    from omnicpg.adapters.neo4j_adapter import Neo4jAdapter


class FakeAdapter:
    """Minimal adapter capturing write batches for assertions."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Store the rows returned for read queries."""
        self._rows = rows
        self.queries: list[str] = []
        self.write_batches: list[list[dict[str, str]]] = []

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """Return canned rows for reads, capture batches for writes."""
        self.queries.append(cypher)
        if "rows" in params:
            self.write_batches.append(params["rows"])
            return []
        return self._rows


class ErrorAdapter(FakeAdapter):
    """Adapter that raises an Exception for specific queries."""

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """Raise an exception if creating an index, else delegate."""
        if cypher.strip().startswith("CREATE INDEX"):
            raise RuntimeError("Simulated index creation error")
        return super().query(cypher, **params)


class TestBuildIndexes:
    """Tests for _build_indexes."""

    def test_builds_fqn_and_simple_indexes(self) -> None:
        """FQN and simple-name indexes are built correctly."""
        rows = [
            {"id": "1", "fqn": "com.app.Foo", "name": "Foo"},
            {"id": "2", "fqn": "com.lib.Foo", "name": "Foo"},
            {"id": "3", "fqn": "com.app.Bar", "name": "Bar"},
        ]
        fqn_index, simple_index = _build_indexes(rows)
        assert fqn_index == {
            "com.app.Foo": "1",
            "com.lib.Foo": "2",
            "com.app.Bar": "3",
        }
        assert sorted(simple_index["Foo"]) == ["1", "2"]
        assert simple_index["Bar"] == ["3"]


class TestResolveBase:
    """Tests for _resolve_base resolution order."""

    def test_exact_fqn_match(self) -> None:
        """Exact FQN match wins."""
        fqn_index = {"com.app.Base": "10"}
        assert _resolve_base("com.app.Base", "1", "com.app.Sub", fqn_index, {}) == "10"

    def test_fqn_suffix_match(self) -> None:
        """Qualified name resolves by FQN suffix."""
        fqn_index = {"com.app.service.Base": "10"}
        simple_index = {"Base": ["10"]}
        assert _resolve_base("service.Base", "1", "com.app.Sub", fqn_index, simple_index) == "10"

    def test_fqn_suffix_match_self_reference_returns_none(self) -> None:
        """FQN suffix match resolving to self returns None."""
        fqn_index = {"com.app.service.Base": "1"}
        simple_index = {"Base": ["1"]}
        assert (
            _resolve_base("service.Base", "1", "com.app.service.Base", fqn_index, simple_index)
            is None
        )

    def test_fqn_suffix_match_not_single(self) -> None:
        """FQN suffix match with multiple candidates falls back to simple index."""
        fqn_index = {"com.app.service.Base": "10", "com.other.service.Base": "20"}
        simple_index = {"Base": ["10", "20"]}
        assert _resolve_base("service.Base", "1", "com.app.Sub", fqn_index, simple_index) == "10"

    def test_unique_simple_name(self) -> None:
        """Unique simple name resolves."""
        simple_index = {"Base": ["10"]}
        assert _resolve_base("Base", "1", "com.app.Sub", {}, simple_index) == "10"

    def test_self_reference_returns_none(self) -> None:
        """A class never resolves to itself."""
        fqn_index = {"com.app.Foo": "1"}
        assert _resolve_base("com.app.Foo", "1", "com.app.Foo", fqn_index, {}) is None

    def test_external_base_unresolved(self) -> None:
        """External bases with no project node return None."""
        assert _resolve_base("Serializable", "1", "com.app.Foo", {}, {}) is None

    def test_empty_base_returns_none(self) -> None:
        """Blank base name returns None."""
        assert _resolve_base("  ", "1", "com.app.Foo", {}, {}) is None

    def test_ambiguous_resolved_by_shared_prefix(self) -> None:
        """Ambiguous simple names resolve by longest shared FQN prefix."""
        fqn_index = {"com.app.Base": "10", "com.other.Base": "20"}
        simple_index = {"Base": ["10", "20"]}
        assert _resolve_base("Base", "1", "com.app.Sub", fqn_index, simple_index) == "10"

    def test_ambiguous_tie_returns_none(self) -> None:
        """Ambiguous names with equal prefix length return None."""
        fqn_index = {"a.Base": "10", "b.Base": "20"}
        simple_index = {"Base": ["10", "20"]}
        assert _resolve_base("Base", "1", "x.Sub", fqn_index, simple_index) is None


class TestMaterializeInheritanceEdges:
    """Tests for materialize_inheritance_edges."""

    def test_extends_and_implements_edges(self) -> None:
        """Superclass yields extends and interfaces yield implements edges."""
        rows = [
            {
                "id": "1",
                "fqn": "com.app.Sub",
                "name": "Sub",
                "superclass": "Base",
                "base_classes": ["Runnable"],
            },
            {
                "id": "2",
                "fqn": "com.app.Base",
                "name": "Base",
                "superclass": None,
                "base_classes": [],
            },
            {
                "id": "3",
                "fqn": "com.app.Runnable",
                "name": "Runnable",
                "superclass": None,
                "base_classes": [],
            },
        ]
        adapter = FakeAdapter(cast("list[dict[str, Any]]", rows))
        summary = materialize_inheritance_edges(cast("Neo4jAdapter", adapter), "proj-test")

        assert summary["classes_scanned"] == 3
        assert summary["edges_created"] == 2
        assert summary["unresolved_bases"] == 0

        edges = adapter.write_batches[0]
        kinds = {(e["src"], e["dst"]): e["kind"] for e in edges}
        assert kinds[("1", "2")] == "extends"
        assert kinds[("1", "3")] == "implements"
        assert all(e["project_id"] == "proj-test" for e in edges)

    def test_external_base_counted_unresolved(self) -> None:
        """Unresolved external bases are counted and not written."""
        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "fqn": "com.app.Foo",
                "name": "Foo",
                "superclass": "Serializable",
                "base_classes": [],
            },
        ]
        adapter = FakeAdapter(rows)
        summary = materialize_inheritance_edges(cast("Neo4jAdapter", adapter), "proj-test")
        assert summary["edges_created"] == 0
        assert summary["unresolved_bases"] == 1
        assert adapter.write_batches == []


class TestMaterializeJavaParameterReachesEdges:
    """Tests for materialize_java_parameter_reaches_edges."""

    def test_targets_formal_parameter_nodes(self) -> None:
        """The repair query bridges argument edges to formal_parameter nodes."""
        adapter = FakeAdapter([{"edges_materialized": 2}])
        summary = materialize_java_parameter_reaches_edges(
            cast("Neo4jAdapter", adapter), "proj-test"
        )

        cypher = "\n".join(adapter.queries)
        assert summary == {"edges_materialized": 2}
        assert "old.interprocedural = 'argument'" in cypher
        assert "paramName.type = 'identifier'" in cypher
        assert "param.type = 'formal_parameter'" in cypher
        assert "MERGE (src)-[fixed:REACHES]->(param)" in cypher


class TestClassifyRole:
    """Tests for the _classify_role rule precedence."""

    def test_annotation_wins(self) -> None:
        """A stereotype annotation takes precedence over name/inheritance."""
        assert _classify_role("Foo", ["@Service"], ["FooDao"]) == ("Service", "service")

    def test_jpa_entity_annotation(self) -> None:
        """@Entity / @Table classify as Entity in the model layer."""
        assert _classify_role("Account", ["Entity"], []) == ("Entity", "model")

    def test_base_type_naming(self) -> None:
        """Inheritance from a *Dao base classifies as Repository."""
        assert _classify_role("AccountStore", [], ["AbstractAccountDao"]) == (
            "Repository",
            "data",
        )

    def test_name_suffix_serviceimpl(self) -> None:
        """ServiceImpl suffix classifies as Service."""
        assert _classify_role("AccountServiceImpl", [], []) == ("Service", "service")

    def test_name_suffix_action_is_web(self) -> None:
        """Struts Action classes are web-layer controllers."""
        assert _classify_role("LoginAction", [], []) == ("Controller", "web")

    def test_name_suffix_dto(self) -> None:
        """DTO suffix classifies as model-layer DTO."""
        assert _classify_role("AccountDTO", [], []) == ("DTO", "model")

    def test_unmatched_returns_none(self) -> None:
        """A class matching no rule is left unclassified."""
        assert _classify_role("RandomThing", [], ["Serializable"]) is None


class TestClassifyArchitecturalRoles:
    """Tests for classify_architectural_roles."""

    def test_skips_nameless_class(self) -> None:
        """Classes missing a name are skipped entirely."""
        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "name": None,
                "annotations": [],
                "superclass": None,
                "base_classes": [],
            },
            {
                "id": "2",
                "name": "",
                "annotations": [],
                "superclass": None,
                "base_classes": [],
            },
        ]
        adapter = FakeAdapter(rows)
        summary = classify_architectural_roles(cast("Neo4jAdapter", adapter), "proj-test")
        assert summary["classes_classified"] == 0
        assert not adapter.write_batches

    def test_index_creation_error_ignored(self) -> None:
        """Index creation exceptions are silently ignored."""
        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "name": "LoginAction",
                "annotations": [],
                "superclass": None,
                "base_classes": [],
            },
        ]
        adapter = ErrorAdapter(rows)
        # Should not raise
        summary = classify_architectural_roles(cast("Neo4jAdapter", adapter), "proj-test")
        assert summary["classes_classified"] == 1

    def test_superclass_role(self) -> None:
        """Classes are classified based on their superclass suffix."""
        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "name": "CustomAccountStore",
                "annotations": [],
                "superclass": "AbstractAccountDao",
                "base_classes": [],
            },
        ]
        adapter = FakeAdapter(rows)
        summary = classify_architectural_roles(cast("Neo4jAdapter", adapter), "proj-test")
        assert summary["classes_classified"] == 1
        assert summary["by_role"]["Repository"] == 1

        update = adapter.write_batches[0][0]
        assert update["id"] == "1"
        assert update["role"] == "Repository"
        assert update["layer"] == "data"

    def test_tags_and_summarizes(self) -> None:
        """Classes are tagged with role/layer and summarized by role."""
        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "name": "LoginAction",
                "annotations": [],
                "superclass": None,
                "base_classes": [],
            },
            {
                "id": "2",
                "name": "AccountServiceImpl",
                "annotations": [],
                "superclass": None,
                "base_classes": [],
            },
            {
                "id": "3",
                "name": "Mystery",
                "annotations": [],
                "superclass": None,
                "base_classes": [],
            },
        ]
        adapter = FakeAdapter(rows)
        summary = classify_architectural_roles(cast("Neo4jAdapter", adapter), "proj-test")

        assert summary["classes_scanned"] == 3
        assert summary["classes_classified"] == 2
        assert summary["by_role"] == {"Controller": 1, "Service": 1}

        updates = adapter.write_batches[0]
        by_id = {u["id"]: (u["role"], u["layer"]) for u in updates}
        assert by_id["1"] == ("Controller", "web")
        assert by_id["2"] == ("Service", "service")
        assert "3" not in by_id


class TestEnrichSemanticIntent:
    """Tests for enrich_semantic_intent."""

    @patch("omnicpg.orchestrator.graph_enrichment.urllib.request.urlopen")
    def test_enrich_semantic_intent_success(self, mock_urlopen: MagicMock) -> None:
        """Nodes with code get enriched with semantic summaries and embeddings."""
        # Setup mock responses: alternating between summary and embedding responses
        import json
        summary_response = MagicMock()
        content_val = json.dumps({"intent": "Provides user login functionality.", "side_effects": "none", "data_sources": "db", "taint_tags": ["Auth"]})
        resp_val = json.dumps({"choices": [{"message": {"content": content_val}}]}).encode('utf-8')
        summary_response.read.return_value = resp_val
        summary_response.__enter__.return_value = summary_response

        embedding_response = MagicMock()
        embedding_response.read.return_value = b'{"data": [{"embedding": [0.1, 0.2, 0.3]}]}'
        embedding_response.__enter__.return_value = embedding_response

        # the executor uses separate calls, order might not be strictly alternating.
        # it fetches summary then embedding sequentially for a given code.
        def mock_urlopen_side_effect(req: Any, **kwargs: Any) -> Any:
            if "embeddings" in req.full_url:
                return embedding_response
            return summary_response

        mock_urlopen.side_effect = mock_urlopen_side_effect

        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "code": "def login(): pass",
                "labels": ["Node", "Method"],
                "name": "login",
                "called_methods": [],
                "parameters": [],
                "parent_class": None,
                "class_methods": [],
            },
            {
                "id": "2",
                "code": "class Auth: pass",
                "labels": ["Node", "Class"],
                "name": "Auth",
                "called_methods": [],
                "parameters": [],
                "parent_class": None,
                "class_methods": ["login"],
            },
        ]
        adapter = FakeAdapter(rows)

        summary = enrich_semantic_intent(
            adapter=cast("Neo4jAdapter", adapter),
            project_id="proj-test",
            api_base="http://fake-llm:11434/v1",
        )

        assert summary["nodes_scanned"] == 2
        assert summary["nodes_enriched"] == 2
        assert mock_urlopen.call_count == 8

        updates = adapter.write_batches[0]
        assert len(updates) == 2
        by_id = {u["id"]: u for u in updates}
        assert by_id["1"]["intent"] == "Provides user login functionality."
        assert by_id["2"]["side_effects"] == "none"
        for u in updates:
            assert "intent_embedding" in u
            assert u["intent_embedding"] == [0.1, 0.2, 0.3]  # type: ignore

    @patch("omnicpg.orchestrator.graph_enrichment.urllib.request.urlopen")
    def test_enrich_semantic_intent_handles_error(self, mock_urlopen: MagicMock) -> None:
        """API errors do not crash the pipeline and unenriched nodes are skipped."""
        import urllib.error

        def side_effect(*args: Any, **kwargs: Any) -> Any:
            raise urllib.error.URLError("Connection refused")

        mock_urlopen.side_effect = side_effect

        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "code": "def login(): pass",
                "labels": ["Node", "Method"],
                "name": "login",
                "called_methods": [],
                "parameters": [],
                "parent_class": None,
                "class_methods": [],
            },
        ]
        adapter = FakeAdapter(rows)

        summary = enrich_semantic_intent(
            adapter=cast("Neo4jAdapter", adapter),
            project_id="proj-test",
            api_base="http://fake-llm:11434/v1",
        )

        assert summary["nodes_scanned"] == 1
        assert summary["nodes_enriched"] == 0
        assert not adapter.write_batches

    def test_enrich_semantic_intent_empty(self) -> None:
        """When no unenriched nodes match, it does nothing."""
        adapter = FakeAdapter([])
        summary = enrich_semantic_intent(
            adapter=cast("Neo4jAdapter", adapter),
            project_id="proj-test",
            api_base="http://fake-llm:11434/v1",
        )
        assert summary["nodes_scanned"] == 0
        assert summary["nodes_enriched"] == 0
        assert not adapter.write_batches


class TestEnrichLLMArchitecturalRoles:
    """Tests for enrich_llm_architectural_roles."""

    @patch("omnicpg.orchestrator.graph_enrichment.urllib.request.urlopen")
    def test_enrich_llm_architectural_roles_success(self, mock_urlopen: MagicMock) -> None:
        """Nodes get enriched with LLM-based roles and layers."""
        import json
        response = MagicMock()
        content_val = json.dumps({"role": "Message Queue Consumer", "layer": "service"})
        resp_val = json.dumps({"choices": [{"message": {"content": content_val}}]}).encode('utf-8')
        response.read.return_value = resp_val
        response.__enter__.return_value = response
        mock_urlopen.return_value = response

        rows: list[dict[str, Any]] = [
            {
                "id": "1",
                "code": "class EventListener: pass",
                "name": "EventListener",
                "annotations": [],
                "superclass": None,
                "base_classes": [],
                "class_methods": [],
            }
        ]
        adapter = FakeAdapter(rows)

        from omnicpg.orchestrator.graph_enrichment import enrich_llm_architectural_roles

        summary = enrich_llm_architectural_roles(
            adapter=cast("Neo4jAdapter", adapter),
            project_id="proj-test",
            api_base="http://fake-llm:11434/v1",
        )

        assert summary["nodes_scanned"] == 1
        assert summary["nodes_enriched"] == 1

        updates = adapter.write_batches[0]
        assert len(updates) == 1
        assert updates[0]["id"] == "1"
        assert updates[0]["role"] == "Message Queue Consumer"
        assert updates[0]["layer"] == "service"
