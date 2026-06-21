"""ASTBuilder — converts Python source to CPG AST nodes and edges via Tree-sitter.

In addition to the full AST sub-graph (``PARENT_OF`` edges), the builder
emits a **macro-skeleton** layer:

* A ``:File`` node for each parsed file.
* ``CONTAINS`` edges forming the ``File → Class → Method`` hierarchy.
* ``DEPENDS_ON`` edges for ``import`` / ``from … import`` statements.
* Enriched properties on ``:Method`` and ``:Class`` nodes:
  ``signature``, ``docstring``, and ``source_code``.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode
from omnicpg.utils.id_gen import generate_deterministic_id

logger = logging.getLogger(__name__)

# Tree-sitter node types that carry a meaningful ``name`` child.
_NAMED_NODE_TYPES = frozenset(
    {
        "function_definition",
        "class_definition",
        "decorated_definition",
    }
)

# Tree-sitter node types considered *skeleton* nodes in ARCHITECTURAL mode.
_SKELETON_TYPES = frozenset(
    {
        "module",
        "class_definition",
        "function_definition",
        "decorated_definition",
        "block",
    }
)

# Statement-level node types retained in STRUCTURAL mode.
_STRUCTURAL_TYPES = frozenset(
    {
        "module",
        "class_definition",
        "function_definition",
        "decorated_definition",
        "expression_statement",
        "return_statement",
        "assignment",
        "augmented_assignment",
        "print_statement",
        "assert_statement",
        "raise_statement",
        "pass_statement",
        "break_statement",
        "continue_statement",
        "delete_statement",
        "import_statement",
        "import_from_statement",
        "global_statement",
        "nonlocal_statement",
        "if_statement",
        "for_statement",
        "while_statement",
        "try_statement",
        "with_statement",
        "block",
        "elif_clause",
        "else_clause",
        "except_clause",
        "finally_clause",
        "parameters",
        "typed_parameter",
        "default_parameter",
    }
)


class ASTBuilder:
    """Build CPG AST sub-graph from Python source using Tree-sitter.

    Usage::

        builder = ASTBuilder()
        nodes, edges = builder.build("example.py", source_text)
    """

    def __init__(self) -> None:
        """Initialise tree-sitter parser with the Python grammar."""
        self._language = Language(tspython.language())
        self._parser = Parser(self._language)

    # ── Public API ────────────────────────────────────────────────────────

    def build(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel = AnalysisLevel.FULL,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse *source_code* and return ``(nodes, edges)``.

        The result includes the full AST (``PARENT_OF`` edges) **and** a
        macro-skeleton overlay: a ``:File`` node with ``CONTAINS`` edges
        to every top-level ``:Class`` and ``:Method``, plus ``DEPENDS_ON``
        edges for each import statement.

        Args:
            file_path: Used to annotate nodes with file-level metadata.
            source_code: The full Python source text.
            analysis_level: Desired analysis granularity.

        Returns:
            A tuple of all AST nodes and edges (``PARENT_OF``,
            ``CONTAINS``, ``DEPENDS_ON``).
        """
        tree = self._parser.parse(source_code.encode("utf-8"))
        nodes: list[CPGNode] = []
        edges: list[CPGEdge] = []
        self._walk(
            tree.root_node,
            file_path,
            nodes,
            edges,
            parent_id=None,
            analysis_level=analysis_level,
        )

        # ── Macro-skeleton: File node + CONTAINS / DEPENDS_ON ─────────
        skeleton_nodes, skeleton_edges = self._build_skeleton(nodes, file_path)
        nodes.extend(skeleton_nodes)
        edges.extend(skeleton_edges)

        logger.info("AST for %s: %d nodes, %d edges", file_path, len(nodes), len(edges))
        return nodes, edges

    # ── Private helpers ───────────────────────────────────────────────────

    def _walk(
        self,
        ts_node: Node,
        file_path: str,
        nodes: list[CPGNode],
        edges: list[CPGEdge],
        parent_id: str | None,
        analysis_level: AnalysisLevel = AnalysisLevel.FULL,
    ) -> str:
        """Recursively convert a tree-sitter node into CPGNode + edges.

        The recursion depth is controlled by *analysis_level*:

        * ``FULL`` — emit every named child (original behaviour).
        * ``ARCHITECTURAL`` — stop at Method level; store body as
          ``source_code`` property.
        * ``STRUCTURAL`` — keep statement-level nodes but prune
          expression and literal children.

        Returns:
            The CPGNode ``id`` created for *ts_node*.
        """
        props = self._extract_properties(ts_node, file_path)
        labels = self._compute_labels(ts_node)

        # Build a deterministic ID from stable attributes.
        # For named entities (class/method) use the name; for unnamed nodes
        # fall back to the code snippet for extra disambiguation.
        node_name = str(props.get("name") or props.get("code", "")[:80])
        line_start = int(props.get("line_start", 0))
        col_start = ts_node.start_point[1]
        node_id = generate_deterministic_id(
            file_path,
            ts_node.type,
            node_name,
            line_start,
            col_start,
        )

        # In ARCHITECTURAL mode, attach source_code to Method nodes.
        if analysis_level == AnalysisLevel.ARCHITECTURAL and ts_node.type == "function_definition":
            body_code = ts_node.text.decode("utf-8") if ts_node.text is not None else ""
            props["source_code"] = body_code

        cpg_node = CPGNode(
            id=node_id,
            labels=labels,
            properties=MappingProxyType(props),
        )
        nodes.append(cpg_node)

        # Choose edge type based on analysis level.
        edge_type = (
            EdgeType.CONTAINS
            if analysis_level == AnalysisLevel.ARCHITECTURAL
            else EdgeType.PARENT_OF
        )

        if parent_id is not None:
            edges.append(
                CPGEdge(
                    source_id=parent_id,
                    target_id=node_id,
                    edge_type=edge_type,
                )
            )

        # Decide whether and how to recurse into children.
        if analysis_level == AnalysisLevel.ARCHITECTURAL:
            # Only recurse into skeleton-level children; stop at methods.
            if ts_node.type != "function_definition":
                for child in ts_node.children:
                    if child.is_named and child.type in _SKELETON_TYPES:
                        self._walk(
                            child,
                            file_path,
                            nodes,
                            edges,
                            parent_id=node_id,
                            analysis_level=analysis_level,
                        )
        elif analysis_level == AnalysisLevel.STRUCTURAL:
            # Recurse into structural-level children only.
            for child in ts_node.children:
                if child.is_named and child.type in _STRUCTURAL_TYPES:
                    self._walk(
                        child,
                        file_path,
                        nodes,
                        edges,
                        parent_id=node_id,
                        analysis_level=analysis_level,
                    )
        else:
            # FULL mode — recurse into all named children.
            for child in ts_node.children:
                if child.is_named:
                    self._walk(
                        child,
                        file_path,
                        nodes,
                        edges,
                        parent_id=node_id,
                        analysis_level=analysis_level,
                    )

        return node_id

    @staticmethod
    def _extract_properties(ts_node: Node, file_path: str) -> dict[str, Any]:
        """Build the ``properties`` dict for a single tree-sitter node."""
        props: dict[str, Any] = {
            "type": ts_node.type,
            "code": (ts_node.text.decode("utf-8") if ts_node.text is not None else ""),
            "line_start": ts_node.start_point[0] + 1,  # 1-indexed
            "line_end": ts_node.end_point[0] + 1,
            "file_path": file_path,
        }

        # Extract identifier name for well-known compound nodes.
        if ts_node.type in _NAMED_NODE_TYPES:
            name_child = ts_node.child_by_field_name("name")
            if name_child is not None and name_child.text is not None:
                props["name"] = name_child.text.decode("utf-8")

        # ── Enriched properties for Method / Class / Module nodes ─────
        if ts_node.type == "function_definition":
            _enrich_function(ts_node, props)
        elif ts_node.type == "class_definition":
            _enrich_class(ts_node, props)
        elif ts_node.type == "module":
            props["layer"] = _detect_layer(file_path)

        return props

    @staticmethod
    def _compute_labels(ts_node: Node) -> tuple[str, ...]:
        """Derive node labels from the tree-sitter node type."""
        base = ("Node",)
        ts_type = ts_node.type
        if ts_type == "function_definition":
            return (*base, "Method")
        if ts_type == "class_definition":
            return (*base, "Class")
        if ts_type in {"identifier", "attribute"}:
            return (*base, "Variable")
        if ts_type == "module":
            return (*base, "Module")
        if ts_type in {"import_statement", "import_from_statement"}:
            return (*base, "Import")
        return base

    # ── Skeleton builder ──────────────────────────────────────────────────

    @staticmethod
    def _build_skeleton(
        ast_nodes: list[CPGNode],
        file_path: str,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Build the macro-skeleton overlay for one file.

        Creates:

        * A ``:File`` node.
        * ``CONTAINS`` edges from File → top-level Class / Method and
          from Class → its contained Methods.
        * ``DEPENDS_ON`` edges from the File node to itself (carrying the
          imported module name as an edge property) for every import
          statement found in the AST.

        Returns:
            ``(extra_nodes, extra_edges)``
        """
        extra_nodes: list[CPGNode] = []
        extra_edges: list[CPGEdge] = []

        # File node — includes ``layer`` derived from path segments.
        file_name = file_path.rsplit("/", maxsplit=1)[-1]
        file_node_id = generate_deterministic_id(file_path, "file", file_name, 1)
        file_node = CPGNode(
            id=file_node_id,
            labels=("Node", "File"),
            properties=MappingProxyType(
                {
                    "type": "file",
                    "name": file_path.rsplit("/", maxsplit=1)[-1],
                    "file_path": file_path,
                    "line_start": 1,
                    "line_end": 0,
                    "code": "",
                    "layer": _detect_layer(file_path),
                }
            ),
        )
        extra_nodes.append(file_node)

        # Index: parent-node-id → children nodes, for the module root.
        class_ids: set[str] = set()
        method_ids: set[str] = set()

        for node in ast_nodes:
            if node.has_label("Class"):
                class_ids.add(node.id)
            elif node.has_label("Method"):
                method_ids.add(node.id)

        # We need to know which class each method belongs to, and which
        # top-level entities the module directly owns.  We use
        # file_path + line ranges as a lightweight containment check
        # (avoids requiring the PARENT_OF index).
        class_nodes = [n for n in ast_nodes if n.id in class_ids]
        method_nodes = [n for n in ast_nodes if n.id in method_ids]

        # File → top-level Classes
        for cls in class_nodes:
            extra_edges.append(
                CPGEdge(
                    source_id=file_node_id,
                    target_id=cls.id,
                    edge_type=EdgeType.CONTAINS,
                )
            )

        # Assign methods to their enclosing class (by line range), or
        # directly to File if top-level.
        claimed_methods: set[str] = set()
        for cls in class_nodes:
            cls_start = int(cls.properties.get("line_start", 0))
            cls_end = int(cls.properties.get("line_end", 0))
            for method in method_nodes:
                m_start = int(method.properties.get("line_start", 0))
                m_end = int(method.properties.get("line_end", 0))
                if cls_start <= m_start and m_end <= cls_end:
                    extra_edges.append(
                        CPGEdge(
                            source_id=cls.id,
                            target_id=method.id,
                            edge_type=EdgeType.CONTAINS,
                        )
                    )
                    claimed_methods.add(method.id)

        # Remaining (top-level) methods → File
        for method in method_nodes:
            if method.id not in claimed_methods:
                extra_edges.append(
                    CPGEdge(
                        source_id=file_node_id,
                        target_id=method.id,
                        edge_type=EdgeType.CONTAINS,
                    )
                )

        # DEPENDS_ON edges for imports
        import_nodes = [n for n in ast_nodes if n.has_label("Import")]
        for imp in import_nodes:
            code = str(imp.properties.get("code", ""))
            module_name = _extract_import_module(code)
            if module_name:
                extra_edges.append(
                    CPGEdge(
                        source_id=file_node_id,
                        target_id=file_node_id,
                        edge_type=EdgeType.DEPENDS_ON,
                        properties=MappingProxyType({"module": module_name}),
                    )
                )

        # ── IMPLEMENTS edges: Class → base Class ─────────────────────
        # For each class with ``base_classes``, emit an IMPLEMENTS edge
        # to every base class node that exists in the same file's AST.
        # NOTE: duplicate class names in the same file will map to the
        # last occurrence; this is acceptable for typical Python code.
        class_name_index: dict[str, str] = {}
        for cls in class_nodes:
            cname = str(cls.properties.get("name", ""))
            if cname:
                class_name_index[cname] = cls.id

        for cls in class_nodes:
            base_classes = cls.properties.get("base_classes", ())
            if not base_classes:
                continue
            for base_name in base_classes:
                base_id = class_name_index.get(str(base_name))
                if base_id is not None and base_id != cls.id:
                    extra_edges.append(
                        CPGEdge(
                            source_id=cls.id,
                            target_id=base_id,
                            edge_type=EdgeType.IMPLEMENTS,
                            properties=MappingProxyType({"base_class": str(base_name)}),
                        )
                    )

        # ── TESTS edges: test_xxx() → xxx() ─────────────────────────
        # Match test functions (``test_*``) to tested functions by
        # stripping the ``test_`` prefix and looking up the name in the
        # method-name index.
        # NOTE: duplicate method names map to the last occurrence.
        method_name_index: dict[str, str] = {}
        for method in method_nodes:
            mname = str(method.properties.get("name", ""))
            if mname:
                method_name_index[mname] = method.id

        for method in method_nodes:
            mname = str(method.properties.get("name", ""))
            if mname.startswith("test_"):
                tested_name = mname[5:]  # strip "test_" prefix
                tested_id = method_name_index.get(tested_name)
                if tested_id is not None and tested_id != method.id:
                    extra_edges.append(
                        CPGEdge(
                            source_id=method.id,
                            target_id=tested_id,
                            edge_type=EdgeType.TESTS,
                            properties=MappingProxyType({"tested_function": tested_name}),
                        )
                    )

        return extra_nodes, extra_edges


# ── Module-level helpers ─────────────────────────────────────────────────


def _enrich_function(ts_node: Node, props: dict[str, Any]) -> None:
    """Add structured metadata to a function node.

    Properties added:

    * ``signature`` — first line of the definition up to the colon.
    * ``docstring`` — extracted from the first string literal in the body.
    * ``source_code`` — full function text.
    * ``param_names`` — tuple of parameter names.
    * ``return_type`` — return-type annotation text (if present).
    * ``is_async`` — ``True`` when decorated with ``async``.
    * ``decorators`` — tuple of decorator names (e.g. ``("classmethod",)``).
    * ``complexity`` — McCabe cyclomatic complexity (branch count + 1).
    """
    # signature: first line of the function definition up to the colon.
    if ts_node.text is not None:
        full_text = ts_node.text.decode("utf-8")
        props["source_code"] = full_text
        first_line_end = full_text.find(":")
        if first_line_end != -1:
            props["signature"] = full_text[: first_line_end + 1].strip()

    # docstring: first child that is an expression_statement containing a string.
    body = ts_node.child_by_field_name("body")
    if body is not None and body.child_count > 0:
        first_stmt = body.children[0]
        if first_stmt.type == "expression_statement" and first_stmt.child_count > 0:
            string_node = first_stmt.children[0]
            if string_node.type == "string" and string_node.text is not None:
                raw = string_node.text.decode("utf-8")
                # Strip triple-quote delimiters.
                for delim in ('"""', "'''"):
                    if raw.startswith(delim) and raw.endswith(delim):
                        raw = raw[3:-3]
                        break
                props["docstring"] = raw.strip()

    # ── Structured properties ────────────────────────────────────────
    props["param_names"] = _extract_param_names(ts_node)
    props["return_type"] = _extract_return_type(ts_node)
    props["is_async"] = _is_async_function(ts_node)
    props["decorators"] = _extract_decorators(ts_node)
    props["complexity"] = _compute_mccabe_complexity(ts_node)


def _enrich_class(ts_node: Node, props: dict[str, Any]) -> None:
    """Add ``docstring``, ``source_code``, and ``base_classes`` to a class node."""
    if ts_node.text is not None:
        props["source_code"] = ts_node.text.decode("utf-8")

    body = ts_node.child_by_field_name("body")
    if body is not None and body.child_count > 0:
        first_stmt = body.children[0]
        if first_stmt.type == "expression_statement" and first_stmt.child_count > 0:
            string_node = first_stmt.children[0]
            if string_node.type == "string" and string_node.text is not None:
                raw = string_node.text.decode("utf-8")
                for delim in ('"""', "'''"):
                    if raw.startswith(delim) and raw.endswith(delim):
                        raw = raw[3:-3]
                        break
                props["docstring"] = raw.strip()

    # ── Base classes ─────────────────────────────────────────────────
    props["base_classes"] = _extract_base_classes(ts_node)


def _extract_import_module(code: str) -> str | None:
    """Extract the module name from an import statement's code text.

    Handles:

    * ``import foo.bar``  → ``foo.bar``
    * ``from foo import bar`` → ``foo``
    """
    code = code.strip()
    if code.startswith("from "):
        parts = code.split()
        if len(parts) >= 2:
            return parts[1]
        return None
    if code.startswith("import "):
        parts = code.split()
        if len(parts) >= 2:
            # Handle ``import a, b`` — return first module.
            return parts[1].rstrip(",")
        return None
    return None


# ── Structured-property helpers ──────────────────────────────────────────


# Tree-sitter node types that increment McCabe cyclomatic complexity.
_BRANCH_TYPES = frozenset(
    {
        "if_statement",
        "elif_clause",
        "for_statement",
        "while_statement",
        "except_clause",
        "with_statement",
        "assert_statement",
        "boolean_operator",  # ``and`` / ``or`` short-circuit branches
        "conditional_expression",  # ternary ``x if cond else y``
    }
)


def _extract_param_names(ts_node: Node) -> tuple[str, ...]:
    """Extract parameter names from a ``function_definition`` node.

    Walks the ``parameters`` field and collects identifier names,
    skipping ``self`` and ``cls`` for methods.
    """
    params_node = ts_node.child_by_field_name("parameters")
    if params_node is None:
        return ()
    names: list[str] = []
    for child in params_node.children:
        # Plain identifier: ``def foo(a, b):``
        if child.type == "identifier" and child.text is not None:
            name = child.text.decode("utf-8")
            if name not in ("self", "cls"):
                names.append(name)
        # Typed parameter: ``def foo(a: int):``
        elif child.type in ("typed_parameter", "typed_default_parameter"):
            name_child = child.child_by_field_name("name")
            if name_child is None:
                # Fall back to first identifier child
                for sub in child.children:
                    if sub.type == "identifier" and sub.text is not None:
                        name_child = sub
                        break
            if name_child is not None and name_child.text is not None:
                name = name_child.text.decode("utf-8")
                if name not in ("self", "cls"):
                    names.append(name)
        # Default parameter: ``def foo(a=1):``
        elif child.type == "default_parameter":
            name_child = child.child_by_field_name("name")
            if name_child is not None and name_child.text is not None:
                name = name_child.text.decode("utf-8")
                if name not in ("self", "cls"):
                    names.append(name)
        # *args / **kwargs
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            for sub in child.children:
                if sub.type == "identifier" and sub.text is not None:
                    names.append(sub.text.decode("utf-8"))
    return tuple(names)


def _extract_return_type(ts_node: Node) -> str | None:
    """Extract return-type annotation from a ``function_definition`` node.

    Returns ``None`` if no return-type annotation is present.
    """
    return_type = ts_node.child_by_field_name("return_type")
    if return_type is not None and return_type.text is not None:
        return return_type.text.decode("utf-8")
    return None


def _is_async_function(ts_node: Node) -> bool:
    """Return ``True`` if the function is ``async def``."""
    # In tree-sitter-python, ``async def`` is a regular
    # ``function_definition`` whose parent is a module/class/block
    # but the text starts with ``async``.
    if ts_node.text is not None:
        text = ts_node.text.decode("utf-8")
        return text.lstrip().startswith("async ")
    return False


def _extract_decorators(ts_node: Node) -> tuple[str, ...]:
    """Extract decorator names from a ``function_definition`` or ``class_definition``.

    Checks whether the node is wrapped in a ``decorated_definition``
    parent, then iterates over ``decorator`` children.
    """
    parent = ts_node.parent
    if parent is None or parent.type != "decorated_definition":
        return ()
    names: list[str] = []
    for child in parent.children:
        if child.type == "decorator" and child.text is not None:
            text = child.text.decode("utf-8").lstrip("@").strip()
            # ``@decorator(args)`` → just the name
            paren_idx = text.find("(")
            if paren_idx != -1:
                text = text[:paren_idx].strip()
            if text:
                names.append(text)
    return tuple(names)


def _compute_mccabe_complexity(ts_node: Node) -> int:
    """Compute McCabe cyclomatic complexity for a function node.

    Counts branch nodes (``if``, ``for``, ``while``, ``except``,
    ``with``, ``assert``, ``boolean_operator``, ``conditional_expression``)
    within the entire function sub-tree.  The result is
    ``branch_count + 1`` (the base path).
    """
    count = 0
    stack = [ts_node]
    while stack:
        node = stack.pop()
        if node.type in _BRANCH_TYPES:
            count += 1
        for child in node.children:
            if child.is_named:
                stack.append(child)
    return count + 1


def _extract_base_classes(ts_node: Node) -> tuple[str, ...]:
    """Extract base class names from a ``class_definition`` node.

    In tree-sitter-python the superclass list is in the
    ``superclasses`` field (an ``argument_list`` node).
    """
    superclasses = ts_node.child_by_field_name("superclasses")
    if superclasses is None:
        return ()
    names: list[str] = []
    for child in superclasses.children:
        if child.type == "identifier" and child.text is not None:
            names.append(child.text.decode("utf-8"))
        elif child.type == "attribute" and child.text is not None:
            # e.g. ``abc.ABC`` → take the whole dotted name
            names.append(child.text.decode("utf-8"))
    return tuple(names)


# ── Layer detection ──────────────────────────────────────────────────────

# Maps path segments to architecture layer names.
_LAYER_PATH_MAP: dict[str, str] = {
    "interfaces": "interface",
    "adapters": "adapter",
    "plugins": "plugin",
    "orchestrator": "engine",
    "models": "model",
    "slicer": "slicer",
    "tests": "test",
    "test": "test",
    "mcp_server": "mcp",
}


def _detect_layer(file_path: str) -> str:
    """Infer the architecture layer from the file path.

    Scans path segments (case-insensitive) against :data:`_LAYER_PATH_MAP`
    and returns the first match, or ``"other"`` if none matches.
    """
    if not file_path:
        return "other"
    # Normalise Windows back-slashes.
    normalised = file_path.replace("\\", "/")
    parts = normalised.split("/")
    for part in parts:
        layer = _LAYER_PATH_MAP.get(part.lower())
        if layer is not None:
            return layer
    return "other"
