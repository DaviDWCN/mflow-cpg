"""Unit tests for streaming call-graph fanout limits."""

from __future__ import annotations

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.orchestrator import project_orchestrator
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder


def test_heuristic_streaming_targets_skip_broad_name_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broad name-only resolution should not expand to every same-named method."""
    monkeypatch.setattr(project_orchestrator, "_MAX_HEURISTIC_CALL_TARGETS", 2)
    definition_index = {"common": ["target-1", "target-2", "target-3"]}

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-1",
        callee_name="common",
        source_method_id="source-1",
        definition_index=definition_index,
        type_index=None,
    )

    assert targets == []
    assert resolution == "heuristic"


def test_heuristic_streaming_targets_prefer_same_file_when_broad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broad name-only resolution keeps precise same-file candidates when possible."""
    monkeypatch.setattr(project_orchestrator, "_MAX_HEURISTIC_CALL_TARGETS", 2)
    definition_index = {"common": ["target-1", "target-2", "target-3"]}
    node_file_map = {
        "target-1": "A.java",
        "target-2": "B.java",
        "target-3": "B.java",
    }

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-1",
        callee_name="common",
        source_method_id="source-1",
        definition_index=definition_index,
        type_index=None,
        node_file_map=node_file_map,
        caller_file="A.java",
    )

    assert targets == ["target-1"]
    assert resolution == "heuristic"


def test_typed_streaming_targets_are_not_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typed resolution may keep multiple valid overloads or implementations."""
    monkeypatch.setattr(project_orchestrator, "_MAX_HEURISTIC_CALL_TARGETS", 2)
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"class-1"},
        class_by_simple={},
        methods_by_class={"class-1": {"common": ["target-1", "target-2", "target-3"]}},
        method_enclosing_class={"source-1": "class-1"},
        fields_by_class={},
        vars_by_method={},
        call_receivers={},
        call_node_types={"call-1": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-1",
        callee_name="common",
        source_method_id="source-1",
        definition_index={"common": []},
        type_index=type_index,
    )

    assert targets == ["target-1", "target-2", "target-3"]
    assert resolution == "typed"


def test_streaming_chained_receiver_resolves_via_return_type() -> None:
    """A chained call ``getRepo().save()`` resolves through the return type."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "repo"},
        class_by_simple={"Svc": ["svc"], "Repo": ["repo"]},
        methods_by_class={
            "svc": {"getRepo": ["m-getrepo"], "a": ["m-a"]},
            "repo": {"save": ["m-save"]},
        },
        method_enclosing_class={"m-a": "svc", "m-getrepo": "svc", "m-save": "repo"},
        fields_by_class={},
        vars_by_method={},
        call_receivers={"call-save": "getRepo()"},
        call_node_types={"call-save": "method_invocation"},
        return_types_by_class={"svc": {"getRepo": "Repo"}},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-save",
        callee_name="save",
        source_method_id="m-a",
        definition_index={"save": ["m-save"]},
        type_index=type_index,
    )

    assert targets == ["m-save"]
    assert resolution == "typed"


def test_streaming_inherited_method_resolves_via_supertype() -> None:
    """A call on a subtype receiver resolves to a method inherited from a base."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "base", "child"},
        class_by_simple={"Svc": ["svc"], "Base": ["base"], "Child": ["child"]},
        methods_by_class={"base": {"ping": ["m-ping"]}},
        method_enclosing_class={"m-a": "svc", "m-ping": "base"},
        fields_by_class={"svc": {"child": "Child"}},
        vars_by_method={},
        call_receivers={"call-ping": "child"},
        call_node_types={"call-ping": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={"child": ["Base"]},
        supertypes_by_class={"child": ["base"]},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-ping",
        callee_name="ping",
        source_method_id="m-a",
        definition_index={"ping": ["m-ping"]},
        type_index=type_index,
    )

    assert targets == ["m-ping"]
    assert resolution == "typed"


def test_streaming_import_disambiguates_same_simple_name() -> None:
    """An explicit import selects the right class among same-named candidates."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "data-repo", "legacy-repo"},
        class_by_simple={"Svc": ["svc"], "Repo": ["data-repo", "legacy-repo"]},
        methods_by_class={
            "data-repo": {"save": ["m-data-save"]},
            "legacy-repo": {"save": ["m-legacy-save"]},
        },
        method_enclosing_class={"m-a": "svc"},
        fields_by_class={"svc": {"repo": "Repo"}},
        vars_by_method={},
        call_receivers={"call-save": "repo"},
        call_node_types={"call-save": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={"svc": "com.app", "data-repo": "com.data", "legacy-repo": "com.legacy"},
        imports_by_file={"Svc.java": {"Repo": "com.data.Repo"}},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-save",
        callee_name="save",
        source_method_id="m-a",
        definition_index={"save": ["m-data-save", "m-legacy-save"]},
        type_index=type_index,
        caller_file="Svc.java",
    )

    assert targets == ["m-data-save"]
    assert resolution == "typed"


def test_streaming_same_package_disambiguates_without_import() -> None:
    """Absent an import, a same-package candidate is preferred."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "local-repo", "far-repo"},
        class_by_simple={"Svc": ["svc"], "Repo": ["local-repo", "far-repo"]},
        methods_by_class={
            "local-repo": {"save": ["m-local-save"]},
            "far-repo": {"save": ["m-far-save"]},
        },
        method_enclosing_class={"m-a": "svc"},
        fields_by_class={"svc": {"repo": "Repo"}},
        vars_by_method={},
        call_receivers={"call-save": "repo"},
        call_node_types={"call-save": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={"svc": "com.app", "local-repo": "com.app", "far-repo": "com.legacy"},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-save",
        callee_name="save",
        source_method_id="m-a",
        definition_index={"save": ["m-local-save", "m-far-save"]},
        type_index=type_index,
        caller_file="Svc.java",
    )

    assert targets == ["m-local-save"]
    assert resolution == "typed"


def test_streaming_interprocedural_args_target_java_formal_parameter() -> None:
    """Streaming argument binding targets Java formal_parameter definitions."""
    nodes, ast_edges = ASTBuilder().build(
        "Svc.java",
        """
        package com.ex;
        public class Svc {
            void caller() { callee(source()); }
            void callee(String p) { sink(p); }
            String source() { return ""; }
            void sink(String s) {}
        }
        """,
    )
    definition_index: dict[str, list[str]] = {}
    call_sites: list[project_orchestrator._StreamingCallSite] = []
    node_file_map: dict[str, str] = {}
    child_to_parent: dict[str, str] = {}
    method_nodes: dict[str, str] = {}
    method_params: dict[str, list[str]] = {}
    method_returns: dict[str, list[str]] = {}
    call_args: dict[str, list[str]] = {}
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids=set(),
        class_by_simple={},
        methods_by_class={},
        method_enclosing_class={},
        fields_by_class={},
        vars_by_method={},
        call_receivers={},
        call_node_types={},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    project_orchestrator.ProjectOrchestrator._update_call_graph_index(
        nodes,
        ast_edges,
        definition_index,
        call_sites,
        node_file_map,
        child_to_parent,
        method_nodes,
        method_params,
        method_returns,
        call_args,
        type_index,
    )
    call_edges = project_orchestrator.ProjectOrchestrator._build_call_graph_from_index(
        definition_index,
        call_sites,
        node_file_map,
        child_to_parent,
        method_nodes,
        type_index,
    )
    inter_edges = project_orchestrator.ProjectOrchestrator._build_interprocedural_dfg_from_index(
        call_edges,
        method_params,
        method_returns,
        call_args,
    )

    node_by_id = {node.id: node for node in nodes}
    argument_edges = [
        edge
        for edge in inter_edges
        if edge.edge_type == EdgeType.REACHES
        and edge.properties.get("interprocedural") == "argument"
    ]
    assert argument_edges
    assert any(
        node_by_id[edge.target_id].properties.get("type") == "formal_parameter"
        for edge in argument_edges
    )
    assert not any(
        node_by_id[edge.target_id].properties.get("type") == "identifier"
        and node_by_id[edge.target_id].properties.get("code") == "p"
        for edge in argument_edges
    )


def test_streaming_resolved_receiver_suppresses_global_explosion() -> None:
    """A resolved receiver whose base is unanalysed must not explode by name.

    ``dao.insert()`` where ``Dao`` extends an out-of-scope ``DaoBase`` (so the
    ``insert`` method is not captured) should emit *no* edges rather than match
    every same-named ``insert`` across the project.
    """
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "dao", "other-a", "other-b"},
        class_by_simple={
            "Svc": ["svc"],
            "Dao": ["dao"],
            "OtherA": ["other-a"],
            "OtherB": ["other-b"],
        },
        methods_by_class={
            "svc": {"a": ["m-a"]},
            "other-a": {"insert": ["m-other-a-insert"]},
            "other-b": {"insert": ["m-other-b-insert"]},
        },
        method_enclosing_class={
            "m-a": "svc",
            "m-other-a-insert": "other-a",
            "m-other-b-insert": "other-b",
        },
        fields_by_class={"svc": {"dao": "Dao"}},
        vars_by_method={},
        call_receivers={"call-insert": "dao"},
        call_node_types={"call-insert": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={"dao": ["DaoBase"]},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-insert",
        callee_name="insert",
        source_method_id="m-a",
        definition_index={"insert": ["m-other-a-insert", "m-other-b-insert"]},
        type_index=type_index,
    )

    assert targets == []
    assert resolution == "typed"


def test_streaming_resolved_receiver_keeps_in_scope_base_method() -> None:
    """A resolved receiver whose base *is* analysed still resolves the method."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "dao", "dao-base", "other"},
        class_by_simple={
            "Svc": ["svc"],
            "Dao": ["dao"],
            "DaoBase": ["dao-base"],
            "Other": ["other"],
        },
        methods_by_class={
            "svc": {"a": ["m-a"]},
            "dao-base": {"insert": ["m-dao-base-insert"]},
            "other": {"insert": ["m-other-insert"]},
        },
        method_enclosing_class={
            "m-a": "svc",
            "m-dao-base-insert": "dao-base",
            "m-other-insert": "other",
        },
        fields_by_class={"svc": {"dao": "Dao"}},
        vars_by_method={},
        call_receivers={"call-insert": "dao"},
        call_node_types={"call-insert": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={"dao": ["DaoBase"]},
        supertypes_by_class={"dao": ["dao-base"]},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-insert",
        callee_name="insert",
        source_method_id="m-a",
        definition_index={"insert": ["m-dao-base-insert", "m-other-insert"]},
        type_index=type_index,
    )

    assert targets == ["m-dao-base-insert"]
    assert resolution == "typed"


def test_streaming_unanalyzed_receiver_type_suppresses_explosion() -> None:
    """A receiver typed as an out-of-scope class must not match by name.

    ``action.find()`` where ``action``'s declared type ``Action`` is never
    analysed should emit no edges rather than every same-named ``find``.
    """
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "other-a", "other-b"},
        class_by_simple={
            "Svc": ["svc"],
            "OtherA": ["other-a"],
            "OtherB": ["other-b"],
        },
        methods_by_class={
            "svc": {"a": ["m-a"]},
            "other-a": {"find": ["m-other-a-find"]},
            "other-b": {"find": ["m-other-b-find"]},
        },
        method_enclosing_class={
            "m-a": "svc",
            "m-other-a-find": "other-a",
            "m-other-b-find": "other-b",
        },
        fields_by_class={"svc": {"action": "Action"}},
        vars_by_method={},
        call_receivers={"call-find": "action"},
        call_node_types={"call-find": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-find",
        callee_name="find",
        source_method_id="m-a",
        definition_index={"find": ["m-other-a-find", "m-other-b-find"]},
        type_index=type_index,
    )

    assert targets == []
    assert resolution == "typed"


def test_streaming_unknown_receiver_still_uses_heuristic() -> None:
    """A receiver with no declared type at all keeps the global name fallback."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "other"},
        class_by_simple={"Svc": ["svc"], "Other": ["other"]},
        methods_by_class={"svc": {"a": ["m-a"]}, "other": {"find": ["m-other-find"]}},
        method_enclosing_class={"m-a": "svc", "m-other-find": "other"},
        fields_by_class={},
        vars_by_method={},
        call_receivers={"call-find": "mystery"},
        call_node_types={"call-find": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-find",
        callee_name="find",
        source_method_id="m-a",
        definition_index={"find": ["m-other-find"]},
        type_index=type_index,
    )

    assert targets == ["m-other-find"]
    assert resolution == "heuristic"


def test_streaming_class_literal_receiver_suppresses_reflection_call() -> None:
    """``Foo.class.getName()`` resolves to java.lang.Class, not a project method."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"foo", "dto-a", "dto-b"},
        class_by_simple={"Foo": ["foo"], "DtoA": ["dto-a"], "DtoB": ["dto-b"]},
        methods_by_class={
            "foo": {"m": ["m-foo"]},
            "dto-a": {"getName": ["m-a-getName"]},
            "dto-b": {"getName": ["m-b-getName"]},
        },
        method_enclosing_class={
            "m-foo": "foo",
            "m-a-getName": "dto-a",
            "m-b-getName": "dto-b",
        },
        fields_by_class={},
        vars_by_method={},
        call_receivers={"call-getName": "Foo.class"},
        call_node_types={"call-getName": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-getName",
        callee_name="getName",
        source_method_id="m-foo",
        definition_index={"getName": ["m-a-getName", "m-b-getName"]},
        type_index=type_index,
    )

    assert targets == []
    assert resolution == "typed"


def test_streaming_get_class_receiver_suppresses_reflection_call() -> None:
    """``this.getClass().getName()`` resolves to java.lang.Class, not a project method."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"foo", "dto-a", "dto-b"},
        class_by_simple={"Foo": ["foo"], "DtoA": ["dto-a"], "DtoB": ["dto-b"]},
        methods_by_class={
            "foo": {"m": ["m-foo"]},
            "dto-a": {"getName": ["m-a-getName"]},
            "dto-b": {"getName": ["m-b-getName"]},
        },
        method_enclosing_class={
            "m-foo": "foo",
            "m-a-getName": "dto-a",
            "m-b-getName": "dto-b",
        },
        fields_by_class={},
        vars_by_method={},
        call_receivers={"call-getName": "this.getClass()"},
        call_node_types={"call-getName": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-getName",
        callee_name="getName",
        source_method_id="m-foo",
        definition_index={"getName": ["m-a-getName", "m-b-getName"]},
        type_index=type_index,
    )

    assert targets == []
    assert resolution == "typed"


def test_streaming_new_unanalyzed_type_suppresses_explosion() -> None:
    """``new Facade().find()`` where ``Facade`` is out of scope emits no edges."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "other-a", "other-b"},
        class_by_simple={
            "Svc": ["svc"],
            "OtherA": ["other-a"],
            "OtherB": ["other-b"],
        },
        methods_by_class={
            "svc": {"a": ["m-a"]},
            "other-a": {"find": ["m-other-a-find"]},
            "other-b": {"find": ["m-other-b-find"]},
        },
        method_enclosing_class={
            "m-a": "svc",
            "m-other-a-find": "other-a",
            "m-other-b-find": "other-b",
        },
        fields_by_class={},
        vars_by_method={},
        call_receivers={"call-find": "new Facade()"},
        call_node_types={"call-find": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-find",
        callee_name="find",
        source_method_id="m-a",
        definition_index={"find": ["m-other-a-find", "m-other-b-find"]},
        type_index=type_index,
    )

    assert targets == []
    assert resolution == "typed"


def test_streaming_field_access_unanalyzed_type_suppresses_explosion() -> None:
    """``this.dao.find()`` with an out-of-scope field type emits no edges."""
    type_index = project_orchestrator._StreamingTypeIndex(
        class_ids={"svc", "other-a", "other-b"},
        class_by_simple={
            "Svc": ["svc"],
            "OtherA": ["other-a"],
            "OtherB": ["other-b"],
        },
        methods_by_class={
            "svc": {"a": ["m-a"]},
            "other-a": {"find": ["m-other-a-find"]},
            "other-b": {"find": ["m-other-b-find"]},
        },
        method_enclosing_class={
            "m-a": "svc",
            "m-other-a-find": "other-a",
            "m-other-b-find": "other-b",
        },
        fields_by_class={"svc": {"dao": "Dao"}},
        vars_by_method={},
        call_receivers={"call-find": "this.dao"},
        call_node_types={"call-find": "method_invocation"},
        return_types_by_class={},
        class_supertype_names={},
        supertypes_by_class={},
        package_by_class={},
        imports_by_file={},
    )

    targets, resolution = project_orchestrator._resolve_streaming_targets(
        call_node_id="call-find",
        callee_name="find",
        source_method_id="m-a",
        definition_index={"find": ["m-other-a-find", "m-other-b-find"]},
        type_index=type_index,
    )

    assert targets == []
    assert resolution == "typed"


def test_find_enclosing_indexed_node_walks_past_fifty_hops() -> None:
    """Deeply-nested call-sites still resolve their enclosing scope (no hop cap).

    Regression: deeply-nested ``else if`` dispatcher ladders pushed call-sites
    more than 50 parent hops from their enclosing Method, so a hardcoded
    50-hop cap silently failed to find the scope and mislabelled the edge.
    """
    # Build a linear parent chain 80 levels deep: n0 (leaf) -> ... -> n80 (method).
    child_to_parent = {f"n{i}": f"n{i + 1}" for i in range(80)}
    indexed_ids = {"n80"}

    result = project_orchestrator._find_enclosing_indexed_node("n0", child_to_parent, indexed_ids)

    assert result == "n80"


def test_find_enclosing_indexed_node_guards_against_cycles() -> None:
    """A corrupt cyclic parent chain terminates instead of looping forever."""
    child_to_parent = {"a": "b", "b": "c", "c": "a"}

    result = project_orchestrator._find_enclosing_indexed_node("a", child_to_parent, {"missing"})

    assert result is None
