"""CallGraphBuilder — derives cross-file call-graph (CALLS) edges from AST nodes.

This builder resolves function invocations to their definitions across files,
enabling whole-project analysis at the microservice level.  It scans the
aggregated AST nodes for:

1. **Definitions** — nodes with ``Method`` label and a ``name`` property.
2. **Call sites** — ``call`` nodes whose function part matches a known name.

For each resolved call it creates a ``CALLS`` edge from the *call site* node
to the *definition* node, regardless of which file each resides in.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING

from omnicpg.models.edge import CPGEdge, EdgeType

if TYPE_CHECKING:
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)


class CallGraphBuilder:
    """Build inter-procedural call-graph edges across files.

    Given the *full* set of AST nodes from *all* analysed files, the builder:

    1. Indexes every function/method definition by its qualified name.
    2. Walks every ``call`` node and resolves the callee name.
    3. Emits a ``CALLS`` edge from the call-site to each resolved definition.

    Limitations (current scope):

    * Name resolution is purely syntactic (no type inference).
    * Does not handle aliased imports (``from foo import bar as baz``).
    * Method calls on objects (``obj.method()``) resolve only by method name.
    """

    # ── Public API ────────────────────────────────────────────────────────

    def build(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Return ``CALLS`` edges linking **Method → Method**.

        Each call-site (``call`` node) is resolved to its enclosing
        ``Method`` via the AST ``PARENT_OF`` edges so that the resulting
        ``CALLS`` edges connect the *caller method* to the *callee
        method* — not the raw call-site node.

        Args:
            all_nodes: Flat list of **all** AST nodes from every file.
            all_edges: Optional list of existing edges (used to build the
                parent index from ``PARENT_OF`` edges).  When *None* the
                builder falls back to using the call-site node directly.

        Returns:
            A list of ``CALLS`` edges.
        """
        # Single-pass: build definition index, node index, and call list.
        def_index: dict[str, list[str]] = {}
        node_index: dict[str, CPGNode] = {}
        call_nodes: list[CPGNode] = []
        # Track which names are imported directly (from x import y) per file.
        # These are "known" top-level names — method calls using these names as
        # plain identifiers are high-confidence CALLS.
        imported_names_by_file: dict[str, set[str]] = {}

        for node in all_nodes:
            node_index[node.id] = node
            if node.has_label("Method"):
                name = node.properties.get("name")
                if name is not None:
                    def_index.setdefault(str(name), []).append(node.id)
            if node.properties.get("type") == "call":
                call_nodes.append(node)
            # Collect imported names from import_from_statement nodes
            if node.properties.get("type") == "import_from_statement":
                fp = str(node.properties.get("file_path", ""))
                code = str(node.properties.get("code", ""))
                # Parse ``from X import a, b as c`` — capture the imported identifiers
                for imported in _parse_imported_names(code):
                    imported_names_by_file.setdefault(fp, set()).add(imported)

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

        for call_node in call_nodes:
            callee_name, is_attribute_call = self._extract_callee_name_and_kind(
                call_node, all_nodes
            )
            if callee_name is None:
                continue

            # For attribute calls (``obj.method()``), only emit a CALLS edge if
            # the method name is unambiguous (exactly one definition found).
            # This dramatically reduces false positives from common method names
            # like ``get``, ``set``, ``update`` that exist in many classes.
            if is_attribute_call and len(def_index.get(callee_name, [])) > 1:
                logger.debug(
                    "Skipping ambiguous attribute call '%s' (%d definitions)",
                    callee_name,
                    len(def_index.get(callee_name, [])),
                )
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

            # Use the enclosing method as source; fall back to call-site.
            source_node = caller_method if caller_method is not None else call_node
            source_id = source_node.id

            targets = def_index.get(callee_name, [])
            for target_id in targets:
                if source_id == target_id:
                    continue  # skip self-recursion at definition level
                pair = (source_id, target_id)
                if pair in seen:
                    continue  # deduplicate
                seen.add(pair)

                caller_file = str(source_node.properties.get("file_path", ""))
                target_node = node_index.get(target_id)
                target_file = (
                    str(target_node.properties.get("file_path", "")) if target_node else ""
                )

                props: dict[str, str] = {
                    "callee": callee_name,
                    "caller_file": caller_file,
                    "target_file": target_file,
                }
                if caller_method is not None:
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
        """Map function/method name → list of definition node IDs.

        Multiple definitions with the same name can exist across files.
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
    def _extract_callee_name(
        call_node: CPGNode,
        all_nodes: list[CPGNode],
    ) -> str | None:
        """Extract the function name from a ``call`` node.

        Handles two patterns:

        * **Simple call**: ``greet(...)`` → extract the identifier before ``(``.
        * **Attribute call**: ``obj.method(...)`` → extract the method name
          after the last dot.

        The extraction is purely code-based to avoid ambiguity from line-range
        containment heuristics in a flat node list.
        """
        _ = all_nodes  # reserved for future tree-based resolution
        code = str(call_node.properties.get("code", ""))
        if "(" not in code:
            return None

        prefix = code.split("(", maxsplit=1)[0].strip()
        if not prefix:
            return None

        # Attribute call: ``obj.method()`` → extract ``method``
        if "." in prefix:
            name = prefix.rsplit(".", maxsplit=1)[-1]
            if name.isidentifier():
                return name
            return None

        # Simple call: ``greet()`` → extract ``greet``
        if prefix.isidentifier():
            return prefix

        return None

    @staticmethod
    def _extract_callee_name_and_kind(
        call_node: CPGNode,
        all_nodes: list[CPGNode],
    ) -> tuple[str | None, bool]:
        """Extract callee name and whether this is an attribute call.

        Returns a ``(callee_name, is_attribute_call)`` pair.

        * ``is_attribute_call=True`` when the call is of the form ``obj.method()``.
          These are lower-confidence because we cannot resolve the receiver type.
        * ``is_attribute_call=False`` for plain ``func()`` calls, which are
          high-confidence when the name matches a known definition.

        Returns ``(None, False)`` when the name cannot be extracted.
        """
        _ = all_nodes  # reserved for future tree-based resolution
        code = str(call_node.properties.get("code", ""))
        if "(" not in code:
            return None, False

        prefix = code.split("(", maxsplit=1)[0].strip()
        if not prefix:
            return None, False

        if "." in prefix:
            name = prefix.rsplit(".", maxsplit=1)[-1]
            if name.isidentifier():
                return name, True  # attribute call
            return None, False

        if prefix.isidentifier():
            return prefix, False  # direct call

        return None, False


def _parse_imported_names(import_stmt_code: str) -> list[str]:
    """Extract directly imported names from a ``from X import a, b as c`` statement.

    Returns the *local* names (after ``as`` if present) that can be used
    directly in the file's scope.

    Examples::

        >>> _parse_imported_names("from utils import greet, farewell")
        ['greet', 'farewell']
        >>> _parse_imported_names("from os import path as ospath")
        ['ospath']
    """
    # Strip "from X import " prefix
    code = import_stmt_code.strip()
    marker = "import "
    idx = code.find(marker)
    if idx == -1:
        return []
    names_part = code[idx + len(marker) :].strip()
    # Handle parenthesised imports
    names_part = names_part.strip("()")
    result: list[str] = []
    for item in names_part.split(","):
        item = item.strip()
        if " as " in item:
            # Take the alias
            alias = item.split(" as ", maxsplit=1)[-1].strip()
            if alias.isidentifier():
                result.append(alias)
        else:
            if item.isidentifier():
                result.append(item)
    return result
