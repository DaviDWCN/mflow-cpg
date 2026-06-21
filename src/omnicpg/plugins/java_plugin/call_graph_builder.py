"""CallGraphBuilder — derives cross-file call-graph (CALLS) edges from Java AST nodes.

This builder resolves method invocations to their definitions across files,
enabling whole-project analysis.  It scans the aggregated AST nodes for:

1. **Definitions** — nodes with ``Method`` label and a ``name`` property.
2. **Call sites** — ``method_invocation`` nodes whose method part matches a
   known name.

For Java frameworks (Spring, Struts, Hibernate) the builder also recognises:

* **Spring**: ``@Autowired`` injection targets linked to ``@Service`` /
  ``@Repository`` / ``@Component`` definitions.
* **Struts 1.x**: ``ActionForm`` and ``Action`` class relationships.
* **Hibernate**: DAO / repository method calls linked to entity definitions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from omnicpg.models.edge import CPGEdge, EdgeType

if TYPE_CHECKING:
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)

# Upper bound on receiver-expression recursion (method-call / field-access
# chains).  Real-world fluent chains are short; this guards runaway recursion
# on malformed input.
_MAX_CHAIN_DEPTH = 8


def _looks_like_type_name(text: str) -> bool:
    """True when *text* is a plain (possibly qualified/generic/array) type name."""
    base = text.strip()
    while base.endswith("[]"):
        base = base[:-2].strip()
    if "<" in base:
        base = base[: base.index("<")].strip()
    if not base:
        return False
    return all(part.isidentifier() for part in base.split("."))


def _extract_cast_target_type(receiver: str) -> str | None:
    """Return the cast target type of a leading cast expression, else ``None``.

    Legacy generics-free Java casts collection elements before invoking
    methods, e.g. ``((FzItemDto) coll.get(0)).getVal()`` whose receiver text is
    ``((FzItemDto) coll.get(0))``.  The cast target type is the receiver's
    static type and can drive type-aware resolution.  Redundant wrapping
    parentheses are unwrapped first.
    """
    text = receiver.strip()
    if not text.startswith("("):
        return None
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                inner = text[1:i].strip()
                rest = text[i + 1 :].strip()
                if not rest:
                    return _extract_cast_target_type(inner)
                return inner if _looks_like_type_name(inner) else None
    return None


def _extract_import_fqn(code: str) -> str | None:
    """Return the imported fully-qualified name from a Java import statement.

    Handles normal and ``static`` imports by returning the last token; the
    caller is responsible for splitting the simple name.  Wildcard imports
    (``import a.b.*;``) return the ``a.b.*`` form unchanged.
    """
    text = code.strip().rstrip(";")
    if not text.startswith("import "):
        return None
    parts = text.split()
    return parts[-1] if len(parts) >= 2 else None


@dataclass(frozen=True)
class _TypeContext:
    """Lightweight per-project type indexes used for type-aware call resolution."""

    class_by_simple: dict[str, list[str]]
    methods_by_class: dict[str, dict[str, list[str]]]
    method_enclosing_class: dict[str, str]
    fields_by_class: dict[str, dict[str, str]]
    vars_by_method: dict[str, dict[str, str]]
    node_index: dict[str, CPGNode]
    # Per-class method return types (simple names): class id → method name →
    # simple return-type name.  Powers receiver-type inference for *chained*
    # calls (e.g. ``a.getB().getC()`` resolves ``getC`` against ``getB``'s
    # declared return type).  Overloads collapse to the last-seen return type.
    return_types_by_class: dict[str, dict[str, str]]
    # Method simple name → set of captured return-type simple names across all
    # classes; lets a chain whose root is unresolvable still infer its result
    # type when the tail getter has a single project-wide return type.
    return_types_by_method: dict[str, set[str]]
    # Class hierarchy: class node id → resolved supertype/subtype class node ids.
    # Populated from each class node's ``superclass`` (simple name) and
    # ``base_classes`` (implemented interfaces), resolved through
    # ``class_by_simple`` when the name is unambiguous within the project.
    supertypes_by_class: dict[str, list[str]]
    subtypes_by_class: dict[str, list[str]]
    # Disambiguation context for simple names shared by several classes:
    #   * ``package_by_class`` — class node id → declaring package.
    #   * ``imports_by_file``  — file path → {simple name → imported FQN}.
    # When a simple type name is ambiguous, resolution prefers an explicitly
    # imported class, then a same-package class, before giving up.
    package_by_class: dict[str, str]
    imports_by_file: dict[str, dict[str, str]]


class CallGraphBuilder:
    """Build inter-procedural call-graph edges across Java files.

    Given the *full* set of AST nodes from *all* analysed files, the builder:

    1. Indexes every method/constructor definition by its qualified name.
    2. Walks every ``method_invocation`` node and resolves the callee name.
    3. Emits a ``CALLS`` edge from the call-site to each resolved definition.

    Limitations (current scope):

    * Name resolution is purely syntactic (no type inference).
    * Overloaded methods with the same name are all linked.
    * Does not handle reflection-based invocations.
    """

    # ── Public API ────────────────────────────────────────────────────────

    def build(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Return ``CALLS`` edges linking **Method → Method**.

        Each call-site (``method_invocation`` node) is resolved to its
        enclosing ``Method`` via the AST ``PARENT_OF`` edges so that the
        resulting ``CALLS`` edges connect the *caller method* to the
        *callee method* — not the raw call-site node.

        Args:
            all_nodes: Flat list of **all** AST nodes from every file.
            all_edges: Optional list of existing edges (used to build the
                parent index from ``PARENT_OF`` edges).  When *None* the
                builder falls back to a file-path + line-range heuristic.

        Returns:
            A list of ``CALLS`` edges.
        """
        # Single-pass: build definition index, node index, and call list.
        def_index: dict[str, list[str]] = {}
        node_index: dict[str, CPGNode] = {}
        call_nodes: list[CPGNode] = []

        for node in all_nodes:
            node_index[node.id] = node
            if node.has_label("Method"):
                name = node.properties.get("name")
                if name is not None:
                    def_index.setdefault(str(name), []).append(node.id)
            if node.properties.get("type") in {"method_invocation", "method_reference", "object_creation_expression"}:
                call_nodes.append(node)

        # Build child→parent map from PARENT_OF / CONTAINS edges for ancestor lookup.
        # ``setdefault`` keeps the first parent seen for each child.  Because
        # the AST builder emits PARENT_OF edges *before* skeleton CONTAINS
        # edges, PARENT_OF (fine-grained) takes precedence when both exist.
        child_to_parent: dict[str, str] = {}
        if all_edges:
            for edge in all_edges:
                if edge.edge_type in (EdgeType.PARENT_OF, EdgeType.CONTAINS):
                    child_to_parent.setdefault(edge.target_id, edge.source_id)

        edges: list[CPGEdge] = []
        seen: set[tuple[str, str]] = set()

        # Pre-index Method nodes by (file_path, line_start, line_end) for
        # the line-range containment fallback.
        method_nodes_by_file: dict[str, list[CPGNode]] = {}
        for _nid, node in node_index.items():
            if node.has_label("Method") and node.properties.get("name"):
                fp = str(node.properties.get("file_path", ""))
                method_nodes_by_file.setdefault(fp, []).append(node)

        # ── Phase 3: lightweight type context for type-aware resolution ──
        type_ctx = self._build_type_context(all_nodes, node_index, child_to_parent)

        for call_node in call_nodes:
            callee_name = self._extract_callee_name(call_node)
            if callee_name is None:
                continue

            # Resolve caller: walk up PARENT_OF/CONTAINS to find enclosing Method.
            caller_method = self._find_enclosing_method(
                call_node.id,
                child_to_parent,
                node_index,
            )
            # Fallback: use line-range containment when edge-walking fails.
            if caller_method is None:
                caller_method = self._find_enclosing_method_by_line_range(
                    call_node,
                    method_nodes_by_file,
                )

            # Only create CALLS edge if we found both caller and callee methods
            if caller_method is None:
                # Skip this call-site - can't resolve caller method
                logger.warning(
                    "Skipping call-site at %s: unable to find enclosing Method",
                    call_node.properties.get("file_path", "unknown"),
                )
                continue

            # Use the caller method as source instead of call-site
            source_id = caller_method.id

            # Type-aware resolution narrows targets to the receiver's class and
            # tags the edge with a confidence level.  Falls back to name-based
            # matching (resolution=heuristic) when the type cannot be inferred.
            targets, resolution = self._resolve_targets(
                call_node,
                callee_name,
                caller_method,
                def_index,
                type_ctx,
            )
            for target_id in targets:
                if source_id == target_id:
                    continue  # skip self-recursion at definition level
                pair = (source_id, target_id)
                if pair in seen:
                    continue  # deduplicate
                seen.add(pair)

                caller_file = str(caller_method.properties.get("file_path", ""))
                target_node = node_index.get(target_id)
                target_file = (
                    str(target_node.properties.get("file_path", "")) if target_node else ""
                )

                props: dict[str, str] = {
                    "callee": callee_name,
                    "caller_file": caller_file,
                    "target_file": target_file,
                    "callsite_id": call_node.id,
                    "resolution": resolution,
                }
                if call_node.properties.get("type") == "method_reference":
                    props["call_kind"] = "method_reference"
                caller_name = str(caller_method.properties.get("name", ""))
                if caller_name:
                    props["caller"] = caller_name

                edges.append(
                    CPGEdge(
                        source_id=source_id,
                        target_id=target_id,
                        edge_type=EdgeType.CALLS,
                        properties=MappingProxyType(props),
                    )
                )

        logger.info("Call graph: generated %d CALLS edges", len(edges))
        return edges

    # ── Phase 3: type-aware resolution ────────────────────────────────────

    @staticmethod
    def _simple_type_name(type_str: str) -> str:
        """Normalize a declared type to its simple class name.

        Strips generics (``List<Foo>`` → ``List``), array markers
        (``Foo[]`` → ``Foo``) and package qualifiers (``a.b.Foo`` → ``Foo``).
        """
        text = type_str.strip()
        if "<" in text:
            text = text[: text.index("<")]
        text = text.replace("[]", "").strip()
        if "." in text:
            text = text.rsplit(".", maxsplit=1)[-1]
        return text

    def _build_type_context(
        self,
        all_nodes: list[CPGNode],
        node_index: dict[str, CPGNode],
        child_to_parent: dict[str, str],
    ) -> _TypeContext:
        """Build class / method / variable type indexes for type-aware resolution."""
        class_types = {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }
        class_by_simple: dict[str, list[str]] = {}
        methods_by_class: dict[str, dict[str, list[str]]] = {}
        method_enclosing_class: dict[str, str] = {}
        fields_by_class: dict[str, dict[str, str]] = {}
        vars_by_method: dict[str, dict[str, str]] = {}
        return_types_by_class: dict[str, dict[str, str]] = {}
        return_types_by_method: dict[str, set[str]] = {}
        package_by_class: dict[str, str] = {}
        imports_by_file: dict[str, dict[str, str]] = {}

        # Index class declarations by simple name (+ declaring package).
        for node in all_nodes:
            if node.properties.get("type") in class_types:
                name = node.properties.get("name")
                if name is not None:
                    class_by_simple.setdefault(str(name), []).append(node.id)
                    package_by_class[node.id] = str(node.properties.get("package", ""))
            elif node.has_label("Import"):
                fqn = _extract_import_fqn(str(node.properties.get("code", "")))
                if fqn and "." in fqn:
                    simple = fqn.rsplit(".", 1)[1]
                    if simple != "*":
                        file_path = str(node.properties.get("file_path", ""))
                        imports_by_file.setdefault(file_path, {})[simple] = fqn

        def enclosing_of(node_id: str, predicate: Any) -> str | None:
            current = node_id
            seen: set[str] = {current}
            while True:
                parent_id = child_to_parent.get(current)
                if parent_id is None or parent_id in seen:
                    return None
                parent = node_index.get(parent_id)
                if parent is None:
                    return None
                if predicate(parent):
                    return parent_id
                seen.add(parent_id)
                current = parent_id

        is_class = lambda n: n.properties.get("type") in class_types  # noqa: E731
        is_method = lambda n: n.has_label("Method")  # noqa: E731

        for node in all_nodes:
            ntype = node.properties.get("type")
            if node.has_label("Method"):
                name = node.properties.get("name")
                cls = enclosing_of(node.id, is_class)
                if cls is not None:
                    method_enclosing_class[node.id] = cls
                    if name is not None:
                        methods_by_class.setdefault(cls, {}).setdefault(str(name), []).append(
                            node.id
                        )
                        return_type = node.properties.get("return_type")
                        if return_type:
                            simple_rt = self._simple_type_name(str(return_type))
                            return_types_by_class.setdefault(cls, {})[str(name)] = simple_rt
                            return_types_by_method.setdefault(str(name), set()).add(simple_rt)
            elif ntype == "field_declaration":
                cls = enclosing_of(node.id, is_class)
                declared = node.properties.get("declared_type")
                if cls is not None and declared:
                    simple = self._simple_type_name(str(declared))
                    for var in node.properties.get("var_names", ()) or ():
                        fields_by_class.setdefault(cls, {})[str(var)] = simple
            elif ntype in (
                "local_variable_declaration",
                "enhanced_for_statement",
                "catch_formal_parameter",
            ):
                method = enclosing_of(node.id, is_method)
                declared = node.properties.get("declared_type")
                if method is not None and declared:
                    simple = self._simple_type_name(str(declared))
                    for var in node.properties.get("var_names", ()) or ():
                        vars_by_method.setdefault(method, {})[str(var)] = simple
            elif ntype == "formal_parameter":
                method = enclosing_of(node.id, is_method)
                if method is not None:
                    name, type_str = self._parse_formal_parameter(node)
                    if name and type_str:
                        vars_by_method.setdefault(method, {})[name] = self._simple_type_name(
                            type_str
                        )

        # ── Class hierarchy (supertype / subtype) for virtual dispatch ──
        # Resolve each class' declared ``superclass`` + ``base_classes`` (interfaces)
        # to concrete class node ids when the simple name is unambiguous in the
        # project.  Ambiguous names (multiple classes sharing a simple name) are
        # skipped to avoid spurious edges.
        supertypes_by_class: dict[str, list[str]] = {}
        subtypes_by_class: dict[str, list[str]] = {}
        for node in all_nodes:
            if node.properties.get("type") not in class_types:
                continue
            supers: list[str] = []
            superclass = node.properties.get("superclass")
            base_names: list[str] = []
            if superclass:
                base_names.append(str(superclass))
            for base in node.properties.get("base_classes", ()) or ():
                base_names.append(str(base))
            for base_name in base_names:
                simple = self._simple_type_name(base_name)
                candidates = class_by_simple.get(simple, [])
                if len(candidates) == 1 and candidates[0] != node.id:
                    super_id = candidates[0]
                    supers.append(super_id)
                    subtypes_by_class.setdefault(super_id, []).append(node.id)
            if supers:
                supertypes_by_class[node.id] = supers

        return _TypeContext(
            class_by_simple=class_by_simple,
            methods_by_class=methods_by_class,
            method_enclosing_class=method_enclosing_class,
            fields_by_class=fields_by_class,
            vars_by_method=vars_by_method,
            node_index=node_index,
            return_types_by_class=return_types_by_class,
            return_types_by_method=return_types_by_method,
            supertypes_by_class=supertypes_by_class,
            subtypes_by_class=subtypes_by_class,
            package_by_class=package_by_class,
            imports_by_file=imports_by_file,
        )

    @staticmethod
    def _parse_formal_parameter(node: CPGNode) -> tuple[str, str]:
        """Return ``(name, type)`` parsed from a ``formal_parameter`` node's code."""
        code = str(node.properties.get("code", "")).strip()
        if not code:
            return "", ""
        # Drop annotations like ``@PathVariable Long id``.
        tokens = [t for t in code.split() if not t.startswith("@")]
        if len(tokens) < 2:
            return "", ""
        return tokens[-1], " ".join(tokens[:-1])

    def _resolve_targets(
        self,
        call_node: CPGNode,
        callee_name: str,
        caller_method: CPGNode,
        def_index: dict[str, list[str]],
        ctx: _TypeContext,
    ) -> tuple[list[str], str]:
        """Resolve a call to target method IDs, returning ``(targets, resolution)``.

        ``resolution`` is ``"typed"`` when the receiver type could be inferred
        and the method found in that class; otherwise ``"heuristic"`` (name-based
        matching across all definitions, preserving the legacy behaviour).
        """
        receiver = call_node.properties.get("receiver")
        caller_class = ctx.method_enclosing_class.get(caller_method.id)
        target_class: str | None = None

        # ``Foo.class.getName()`` / ``x.getClass().getName()`` and friends invoke
        # ``java.lang.Class`` reflection methods, which are always external.
        # Never link them to same-named project methods (this would explode to
        # every same-named method in the codebase).
        if receiver is not None:
            recv_text = str(receiver).rstrip()
            if recv_text.endswith(".class") or recv_text.endswith("getClass()"):
                return [], "typed"

        if call_node.properties.get("type") == "object_creation_expression":
            target_class = self._resolve_class_of_type(callee_name, ctx, caller_method, caller_class)
        elif receiver is None or str(receiver) in {"this", "super"}:
            # Unqualified / this call → resolve within the caller's class.
            target_class = caller_class
        else:
            target_class = self._infer_receiver_class(
                str(receiver), caller_method, caller_class, ctx
            )

        if target_class is not None:
            candidates = self._resolve_in_hierarchy(target_class, callee_name, ctx)
            if candidates:
                narrowed = self._disambiguate_overloads(call_node, candidates, caller_method, ctx)
                return narrowed, "typed"
            # Receiver type is known but the method isn't in the captured
            # hierarchy (e.g. a base class outside the analysis scope). Constrain
            # candidates to the receiver type's own name-based hierarchy instead
            # of exploding to a global name match (which emits many false edges).
            allowed = self._name_ancestor_classes(target_class, ctx)
            constrained = [
                target
                for target in def_index.get(callee_name, [])
                if ctx.method_enclosing_class.get(target) in allowed
            ]
            return constrained, "typed"

        # Receiver's declared type name is known but its class could not be
        # pinned down above. Either the type was never analysed (suppress the
        # call as out-of-scope) or its simple name is ambiguous (several
        # same-named classes in scope) — in the latter case constrain candidates
        # to the union of those classes' name-based hierarchies rather than
        # exploding to every same-named method in the project.
        if receiver is not None and str(receiver) not in {"this", "super"}:
            type_name = self._receiver_type_name(str(receiver), caller_method, caller_class, ctx)
            if type_name:
                candidate_classes = ctx.class_by_simple.get(type_name)
                if not candidate_classes:
                    return [], "typed"
                allowed = set()
                for cid in candidate_classes:
                    allowed |= self._name_ancestor_classes(cid, ctx)
                constrained = [
                    target
                    for target in def_index.get(callee_name, [])
                    if ctx.method_enclosing_class.get(target) in allowed
                ]
                return constrained, "typed"

        # Fallback: name-based matching across all definitions.
        return def_index.get(callee_name, []), "heuristic"

    @classmethod
    def _receiver_type_name(
        cls,
        receiver: str,
        caller_method: CPGNode,
        caller_class: str | None,
        ctx: _TypeContext,
    ) -> str | None:
        """Return the declared simple type name of a *receiver* expression.

        Resolves the three common shapes whose type is statically knowable even
        when the type's class was not analysed: ``new Foo(...)`` → ``Foo``, a
        bare identifier → its local / parameter / field type, a single field
        access ``obj.field`` → the field's declared type, and a method-call
        chain ``a.getB()`` → ``getB``'s declared return type (or its
        project-wide return type when the root cannot be resolved).  Returns
        ``None`` when the type cannot be inferred (e.g. an uncaptured return
        type).
        """
        text = receiver.strip()
        if text.startswith("new "):
            head = text[4:].split("(", 1)[0].split("[", 1)[0].strip()
            return cls._simple_type_name(head) if head else None
        cast_type = _extract_cast_target_type(text)
        if cast_type is not None:
            return cls._simple_type_name(cast_type)
        if text.isidentifier():
            declared = ctx.vars_by_method.get(caller_method.id, {}).get(text)
            if declared is None and caller_class is not None:
                declared = ctx.fields_by_class.get(caller_class, {}).get(text)
            return declared
        if text.endswith(")"):
            call_head = cls._strip_trailing_call_args(text)
            if call_head is None:
                return None
            if "." in call_head:
                obj, method = call_head.rsplit(".", 1)
                obj_class = cls._infer_receiver_class(obj, caller_method, caller_class, ctx)
            else:
                method, obj_class = call_head, caller_class
            if not method.isidentifier():
                return None
            if obj_class is not None:
                resolved = cls._lookup_return_type_in_hierarchy(obj_class, method, ctx)
                if resolved is not None:
                    return resolved
            return cls._global_return_type(method, ctx)
        if "." in text:
            obj, member = text.rsplit(".", 1)
            if member.isidentifier():
                obj_class = cls._infer_receiver_class(obj, caller_method, caller_class, ctx)
                if obj_class is not None:
                    return cls._lookup_field_in_hierarchy(obj_class, member, ctx)
        return None

    @classmethod
    def _name_ancestor_classes(cls, target_class: str, ctx: _TypeContext) -> set[str]:
        """Return *target*'s name-based super-type closure (existing classes).

        Reads the ``superclass``/``base_classes`` properties off each class node
        so the closure covers bases that were dropped from ``supertypes_by_class``
        due to ambiguity, while excluding bases that were never analysed.
        """
        result = {target_class}
        stack = [target_class]
        while stack:
            cid = stack.pop()
            node = ctx.node_index.get(cid)
            if node is None:
                continue
            names: list[str] = []
            superclass = node.properties.get("superclass")
            if superclass:
                names.append(cls._simple_type_name(str(superclass)))
            for base in node.properties.get("base_classes", ()) or ():
                names.append(cls._simple_type_name(str(base)))
            for name in names:
                for candidate in ctx.class_by_simple.get(name, []):
                    if candidate not in result:
                        result.add(candidate)
                        stack.append(candidate)
        return result

    @classmethod
    def _infer_receiver_class(
        cls,
        expr: str,
        caller_method: CPGNode,
        caller_class: str | None,
        ctx: _TypeContext,
        depth: int = 0,
    ) -> str | None:
        """Infer the class node id a receiver *expression* statically refers to.

        Handles the common Java receiver shapes, recursing through method-call
        chains (``a.getB().getC()``) and field access (``a.b.c``):

        * ``this`` / ``super`` → the caller's enclosing class.
        * ``new Foo(...)`` → ``Foo`` (when unambiguous in the project).
        * a local / parameter / field identifier → its declared type's class.
        * a bare class name → that class (static call, e.g. ``Foo.bar()``).
        * ``<expr>.method(...)`` → the method's declared return-type class.
        * ``<expr>.field`` → the field's declared-type class.

        Returns ``None`` when the type cannot be inferred unambiguously.
        """
        if depth > _MAX_CHAIN_DEPTH:
            return None
        text = expr.strip()
        if not text:
            return None
        if text in {"this", "super"}:
            return caller_class
        # ``((Foo) expr).m()`` — cast target type is the receiver's static type.
        cast_type = _extract_cast_target_type(text)
        if cast_type is not None:
            return cls._resolve_class_of_type(cast_type, ctx, caller_method, caller_class)
        if text.startswith("new "):
            head = text[4:].split("(", 1)[0].split("[", 1)[0].strip()
            if not head:
                return None
            return cls._resolve_class_of_type(head, ctx, caller_method, caller_class)

        if text.isidentifier():
            declared = ctx.vars_by_method.get(caller_method.id, {}).get(text)
            if declared is None and caller_class is not None:
                declared = ctx.fields_by_class.get(caller_class, {}).get(text)
            if declared is not None:
                return cls._resolve_class_of_type(declared, ctx, caller_method, caller_class)
            # Bare class name → static method call.
            return cls._resolve_class_of_type(text, ctx, caller_method, caller_class)

        # Method-call chain: strip the trailing ``(...)`` argument list.
        if text.endswith(")"):
            call_head = cls._strip_trailing_call_args(text)
            if call_head is None:
                return None
            if "." in call_head:
                obj, method = call_head.rsplit(".", 1)
                obj_class = cls._infer_receiver_class(
                    obj, caller_method, caller_class, ctx, depth + 1
                )
            else:
                method, obj_class = call_head, caller_class
            if obj_class is None or not method.isidentifier():
                return None
            return_type = cls._lookup_return_type_in_hierarchy(obj_class, method, ctx)
            if not return_type:
                return None
            return cls._resolve_class_of_type(return_type, ctx, caller_method, caller_class)

        # Field access: ``<expr>.field``.
        if "." in text:
            obj, member = text.rsplit(".", 1)
            if not member.isidentifier():
                return None
            obj_class = cls._infer_receiver_class(obj, caller_method, caller_class, ctx, depth + 1)
            if obj_class is None:
                return None
            field_type = cls._lookup_field_in_hierarchy(obj_class, member, ctx)
            if not field_type:
                return None
            return cls._resolve_class_of_type(field_type, ctx, caller_method, caller_class)

        return None

    @classmethod
    def _resolve_class_of_type(
        cls,
        type_name: str | None,
        ctx: _TypeContext,
        caller_method: CPGNode | None = None,
        caller_class: str | None = None,
    ) -> str | None:
        """Resolve a (possibly qualified/generic) type name to a unique class id.

        When the simple name is shared by several classes, disambiguates using
        the caller's file imports and then its package, mirroring Java name
        resolution.  Returns ``None`` when still ambiguous.
        """
        if not type_name:
            return None
        simple = cls._simple_type_name(str(type_name))
        classes = ctx.class_by_simple.get(simple, [])
        if not classes:
            return None
        if len(classes) == 1:
            return classes[0]
        return cls._disambiguate_classes(simple, classes, ctx, caller_method, caller_class)

    @staticmethod
    def _disambiguate_classes(
        simple: str,
        classes: list[str],
        ctx: _TypeContext,
        caller_method: CPGNode | None,
        caller_class: str | None,
    ) -> str | None:
        """Pick one class among same-simple-name candidates via imports / package."""
        # 1. Explicit import in the caller's file wins.
        if caller_method is not None:
            file_path = str(caller_method.properties.get("file_path", ""))
            imported_fqn = ctx.imports_by_file.get(file_path, {}).get(simple)
            if imported_fqn:
                imported_pkg = imported_fqn.rsplit(".", 1)[0]
                matches = [
                    cid for cid in classes if ctx.package_by_class.get(cid, "") == imported_pkg
                ]
                if len(matches) == 1:
                    return matches[0]
        # 2. Same-package candidate (unqualified types resolve within the package).
        if caller_class is not None:
            caller_pkg = ctx.package_by_class.get(caller_class)
            if caller_pkg is not None:
                matches = [
                    cid for cid in classes if ctx.package_by_class.get(cid, "") == caller_pkg
                ]
                if len(matches) == 1:
                    return matches[0]
        return None

    @staticmethod
    def _strip_trailing_call_args(text: str) -> str | None:
        """Strip the final balanced ``(...)`` group, returning the callee head.

        ``a.getB().getC(x, y)`` → ``a.getB().getC``.  Returns ``None`` when the
        text does not end in a balanced call.
        """
        text = text.rstrip()
        if not text.endswith(")"):
            return None
        depth = 0
        for i in range(len(text) - 1, -1, -1):
            char = text[i]
            if char == ")":
                depth += 1
            elif char == "(":
                depth -= 1
                if depth == 0:
                    return text[:i].rstrip()
        return None

    @staticmethod
    def _lookup_return_type_in_hierarchy(
        class_id: str,
        method: str,
        ctx: _TypeContext,
    ) -> str | None:
        """Return *method*'s declared return type, walking up the supertype chain."""
        seen: set[str] = set()
        stack = [class_id]
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            found = ctx.return_types_by_class.get(cid, {}).get(method)
            if found is not None:
                return found
            stack.extend(ctx.supertypes_by_class.get(cid, []))
        return None

    @staticmethod
    def _global_return_type(method: str, ctx: _TypeContext) -> str | None:
        """Return *method*'s project-wide return type when it is unambiguous.

        Last resort for chains whose root cannot be resolved (e.g. the declared
        type name is shared by several classes): when ``method`` has a single
        captured return type across the project, that type is a safe basis for
        suppression / constraint; otherwise returns ``None``.
        """
        returns = ctx.return_types_by_method.get(method)
        if returns is not None and len(returns) == 1:
            return next(iter(returns))
        return None

    @staticmethod
    def _lookup_field_in_hierarchy(
        class_id: str,
        field: str,
        ctx: _TypeContext,
    ) -> str | None:
        """Return *field*'s declared type, walking up the supertype chain."""
        seen: set[str] = set()
        stack = [class_id]
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            found = ctx.fields_by_class.get(cid, {}).get(field)
            if found is not None:
                return found
            stack.extend(ctx.supertypes_by_class.get(cid, []))
        return None

    @staticmethod
    def _resolve_in_hierarchy(
        target_class: str,
        callee_name: str,
        ctx: _TypeContext,
    ) -> list[str]:
        """Resolve *callee_name* against *target_class* using the class hierarchy.

        Mirrors Java virtual dispatch / inheritance:

        1. **Inherited methods** — walk *up* the supertype chain so a call on a
           subclass resolves to a method defined in a superclass.
        2. **Virtual dispatch** — walk *down* into subtypes so a call on an
           interface / abstract receiver resolves to overriding implementations
           (Class-Hierarchy-Analysis style).

        The receiver's own class is checked first; if it declares the method we
        still include subtype overrides (the static receiver type may be a base
        type whose runtime type is a subclass).
        """
        targets: list[str] = []

        # 1 + receiver class: climb supertypes until a declaration is found per branch.
        seen_up: set[str] = set()
        stack = [target_class]
        while stack:
            cid = stack.pop()
            if cid in seen_up:
                continue
            seen_up.add(cid)
            found = ctx.methods_by_class.get(cid, {}).get(callee_name)
            if found:
                targets.extend(found)
                continue  # nearest definition on this branch wins; stop climbing
            stack.extend(ctx.supertypes_by_class.get(cid, []))

        # 2. descend into subtypes for overriding implementations.
        seen_down: set[str] = set()
        stack = list(ctx.subtypes_by_class.get(target_class, []))
        while stack:
            cid = stack.pop()
            if cid in seen_down:
                continue
            seen_down.add(cid)
            found = ctx.methods_by_class.get(cid, {}).get(callee_name)
            if found:
                targets.extend(found)
            stack.extend(ctx.subtypes_by_class.get(cid, []))

        # Preserve order, deduplicate.
        return list(dict.fromkeys(targets))

    @classmethod
    def _disambiguate_overloads(
        cls,
        call_node: CPGNode,
        candidates: list[str],
        caller_method: CPGNode,
        ctx: _TypeContext,
    ) -> list[str]:
        """Narrow overloaded candidates by argument count, then argument types.

        First filters by arity (parameter count vs. ``arg_count``).  When the
        argument *expressions* are available (``arg_exprs``) and their static
        types can be inferred, candidates whose declared ``param_types`` do not
        match the inferred argument types (by simple name) are dropped.
        """
        if len(candidates) <= 1:
            return candidates

        arg_count = call_node.properties.get("arg_count")
        if arg_count is not None:
            by_arity = [
                cid
                for cid in candidates
                if len(ctx.node_index[cid].properties.get("param_types", []) or [])
                == int(arg_count)
            ]
            if by_arity:
                candidates = by_arity

        if len(candidates) <= 1:
            return candidates

        # Type-based narrowing using inferred argument types.
        arg_types = cls._infer_arg_types(call_node, caller_method, ctx)
        if not arg_types:
            return candidates

        typed = []
        for cid in candidates:
            param_types = ctx.node_index[cid].properties.get("param_types", []) or []
            if len(param_types) != len(arg_types):
                continue
            if cls._param_types_match(list(param_types), arg_types):
                typed.append(cid)
        return typed or candidates

    @classmethod
    def _infer_arg_types(
        cls,
        call_node: CPGNode,
        caller_method: CPGNode,
        ctx: _TypeContext,
    ) -> list[str | None]:
        """Best-effort static type inference for each call argument.

        Returns a list aligned with the argument positions; entries are simple
        type names (e.g. ``"String"``) or ``None`` when the type is unknown.
        """
        exprs = call_node.properties.get("arg_exprs")
        if not exprs:
            return []
        caller_class = ctx.method_enclosing_class.get(caller_method.id)
        result: list[str | None] = []
        for expr in exprs:
            result.append(cls._infer_expr_type(str(expr), caller_method, caller_class, ctx))
        return result

    @classmethod
    def _infer_expr_type(
        cls,
        expr: str,
        caller_method: CPGNode,
        caller_class: str | None,
        ctx: _TypeContext,
    ) -> str | None:
        """Infer the simple static type of a single argument expression."""
        text = expr.strip()
        if not text:
            return None
        # String / char / numeric / boolean / null literals.
        if text.startswith('"'):
            return "String"
        if text.startswith("'"):
            return "char"
        if text in {"true", "false"}:
            return "boolean"
        if text == "null":
            return None
        if text.endswith(("L", "l")) and text[:-1].isdigit():
            return "long"
        if text.endswith(("f", "F")) and cls._is_decimal(text[:-1]):
            return "float"
        if text.endswith(("d", "D")) and cls._is_decimal(text[:-1]):
            return "double"
        if text.isdigit():
            return "int"
        if cls._is_decimal(text):
            return "double"
        # ``new Foo(...)`` → Foo
        if text.startswith("new "):
            rest = text[4:].strip()
            head = rest.split("(", 1)[0].split("[", 1)[0].strip()
            return cls._simple_type_name(head) if head else None
        # Identifier → look up in caller's locals/params, then fields.
        if text.isidentifier():
            local = ctx.vars_by_method.get(caller_method.id, {}).get(text)
            if local is not None:
                return local
            if caller_class is not None:
                return ctx.fields_by_class.get(caller_class, {}).get(text)
        return None

    @staticmethod
    def _is_decimal(text: str) -> bool:
        """Return True when *text* looks like a decimal literal (e.g. ``1.5``)."""
        try:
            float(text)
        except ValueError:
            return False
        return "." in text or "e" in text or "E" in text

    @classmethod
    def _param_types_match(cls, param_types: list[str], arg_types: list[str | None]) -> bool:
        """Return True when each known arg type matches the param's simple type.

        Unknown argument types (``None``) match any parameter (conservative).
        Numeric widening is allowed (e.g. ``int`` arg matches ``long``/``double``
        parameter) to avoid dropping legitimate overloads.
        """
        numeric_widen = {
            "int": {"int", "long", "float", "double", "Integer", "Object"},
            "long": {"long", "float", "double", "Long", "Object"},
            "float": {"float", "double", "Float", "Object"},
            "double": {"double", "Double", "Object"},
            "char": {"char", "int", "long", "float", "double", "Character", "Object"},
        }
        for declared, inferred in zip(param_types, arg_types, strict=False):
            if inferred is None:
                continue
            simple = cls._simple_type_name(str(declared))
            if simple == inferred or simple == "Object":
                continue
            if inferred in numeric_widen and simple in numeric_widen[inferred]:
                continue
            return False
        return True

    # ── Private helpers ───────────────────────────────────────────────────

    @staticmethod
    def _find_enclosing_method(
        node_id: str,
        child_to_parent: dict[str, str],
        node_index: dict[str, CPGNode],
    ) -> CPGNode | None:
        """Walk up the AST via ``PARENT_OF`` / ``CONTAINS`` edges to find the nearest Method.

        Walks the full parent chain (a tree, so it terminates naturally),
        returning ``None`` when no ``Method``-labelled ancestor is reachable.
        A ``seen`` set guards against any corrupt cyclic parent chain.
        """
        current = node_id
        seen: set[str] = {current}
        while True:
            parent_id = child_to_parent.get(current)
            if parent_id is None or parent_id in seen:
                return None
            parent_node = node_index.get(parent_id)
            if parent_node is None:
                return None
            if parent_node.has_label("Method"):
                return parent_node
            seen.add(parent_id)
            current = parent_id

    @staticmethod
    def _find_enclosing_method_by_line_range(
        call_node: CPGNode,
        method_nodes_by_file: dict[str, list[CPGNode]],
    ) -> CPGNode | None:
        """Find the enclosing Method using file-path + line-range containment.

        This is the fallback when ``PARENT_OF`` / ``CONTAINS`` edge-walking
        fails (e.g. edges were not passed to :meth:`build`).

        Among all Method nodes in the same file whose line range encloses the
        call-site, the **innermost** (smallest span) method is returned.
        """
        call_file = str(call_node.properties.get("file_path", ""))
        call_line = int(call_node.properties.get("line_start", 0))

        if not call_file or call_line == 0:
            logger.debug(
                "Line-range fallback skipped: missing file_path/line_start on node %s",
                call_node.id,
            )
            return None

        candidates = method_nodes_by_file.get(call_file, [])
        best: CPGNode | None = None
        best_span = float("inf")
        for method in candidates:
            m_start = int(method.properties.get("line_start", 0))
            m_end = int(method.properties.get("line_end", 0))
            if m_start <= call_line <= m_end:
                span = m_end - m_start
                if span < best_span:
                    best = method
                    best_span = span
        return best

    @staticmethod
    def _build_definition_index(
        all_nodes: list[CPGNode],
    ) -> dict[str, list[str]]:
        """Map method/constructor name → list of definition node IDs.

        Multiple definitions with the same name can exist across files
        (overloading, multiple implementations of an interface, etc.).
        """
        index: dict[str, list[str]] = {}
        for node in all_nodes:
            if not node.has_label("Method"):
                continue
            name = node.properties.get("name")
            if name is None:
                continue
            name_str = str(name)
            index.setdefault(name_str, []).append(node.id)
        return index

    @staticmethod
    def _extract_callee_name(call_node: CPGNode) -> str | None:
        """Extract the method name from a ``method_invocation`` node.

        Prefers the ``name`` property extracted at parse time (via tree-sitter
        fields, robust to chained / generic / nested calls).  Falls back to
        string parsing only for nodes lacking the property.
        """
        name = call_node.properties.get("name")
        if name is not None:
            name_str = str(name)
            if call_node.properties.get("type") == "object_creation_expression":
                if "<" in name_str:
                    name_str = name_str.split("<", maxsplit=1)[0].strip()
                if "." in name_str:
                    name_str = name_str.rsplit(".", maxsplit=1)[-1]
            if name_str.isidentifier():
                return name_str

        code = str(call_node.properties.get("code", ""))
        if "(" not in code:
            return None

        prefix = code.split("(", maxsplit=1)[0].strip()
        if not prefix:
            return None

        # Qualified call: ``obj.method()`` -> extract ``method``
        if "." in prefix:
            resolved = prefix.rsplit(".", maxsplit=1)[-1]
            if resolved.isidentifier():
                return resolved
            return None

        # Simple call: ``doSomething()``
        if prefix.isidentifier():
            return prefix

        return None
