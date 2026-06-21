"""DFGBuilder — derives data-flow (REACHES) edges from the Java control-flow graph.

Algorithm
---------
The builder performs a classic **reaching-definitions** dataflow analysis
(gen/kill on the CFG built from ``FLOWS_TO`` edges):

1. Build CFG successor / predecessor maps from the supplied ``FLOWS_TO`` edges.
2. For each method, run a worklist-based reaching-definitions pass:
   - Each CFG node has an *in-set* / *out-set* of ``(var_name, def_node_id)``.
   - Gen/Kill: a definition **kills** prior definitions of the same variable
     and **generates** a new one.
3. After convergence, for every ``identifier`` use inside the method, emit a
   ``REACHES`` edge from each reaching definition to the use.

Compared to the previous "last-def-before-use" MVP this correctly handles:

* **Branch-join merge** — both ``if`` / ``else`` definitions reach later uses.
* **Loop-carried definitions** — a definition inside a loop body reaches uses
  on subsequent iterations.
* **try/catch merges**.

Java-specific behaviour:

* Definitions: ``local_variable_declaration`` (incl. multi-declaration
  ``int a, b;``), ``assignment_expression`` (simple identifier or field LHS),
  and ``formal_parameter`` (method-entry definitions).
* **Field-level flow**: ``this.field = ...`` / ``field = ...`` are tracked
  under a ``field:<name>`` namespace.  A bare identifier use resolves to a
  local definition first (locals shadow fields, matching Java semantics) and
  falls back to a field definition.

A line-ordered "last-def-before-use" fallback is retained for when no CFG /
PARENT_OF edges are available, preserving backward compatibility.
"""

from __future__ import annotations

import logging
from collections import deque
from types import MappingProxyType
from typing import TYPE_CHECKING

from omnicpg.models.edge import CPGEdge, EdgeType

if TYPE_CHECKING:
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)

# AST node types treated as variable *definitions* (left-hand side).
_DEF_TYPES = frozenset(
    {
        "local_variable_declaration",
        "assignment_expression",
        "formal_parameter",
        "spread_parameter",
    }
)

# Assignment-like nodes that may appear as direct children of a CFG statement.
_ASSIGN_TYPES = frozenset({"assignment_expression", "local_variable_declaration"})


class DFGBuilder:
    """Build intra-procedural data-flow edges via reaching-definitions for Java."""

    def __init__(self) -> None:
        """Initialise builder state."""
        self._node_map: dict[str, CPGNode] = {}
        self._parent_children: dict[str, list[str]] = {}
        self._child_to_parent: dict[str, str] = {}

    # -- Public API --------------------------------------------------------

    def build(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Return ``REACHES`` edges for each def->use pair.

        Args:
            cfg_nodes: All AST/CFG nodes.
            cfg_edges: ``FLOWS_TO`` edges from :class:`CFGBuilder`.
            ast_edges: ``PARENT_OF`` edges for O(1) subtree traversal.

        Returns:
            A deduplicated list of ``REACHES`` edges.
        """
        self._node_map = {n.id: n for n in cfg_nodes}
        self._parent_children = {}
        self._child_to_parent = {}
        if ast_edges:
            for e in ast_edges:
                self._parent_children.setdefault(e.source_id, []).append(e.target_id)
                self._child_to_parent[e.target_id] = e.source_id

        # Build CFG successor / predecessor maps.
        cfg_successors: dict[str, list[str]] = {}
        cfg_predecessors: dict[str, list[str]] = {}
        for e in cfg_edges:
            if e.edge_type == EdgeType.FLOWS_TO:
                cfg_successors.setdefault(e.source_id, []).append(e.target_id)
                cfg_predecessors.setdefault(e.target_id, []).append(e.source_id)

        # Fallback to the legacy heuristic when control/parent edges are absent.
        if not cfg_successors or not self._parent_children:
            return self._build_fallback(cfg_nodes)

        edges: list[CPGEdge] = []
        method_nodes = [n for n in cfg_nodes if n.has_label("Method")]
        for fn in method_nodes:
            scope_children = self._scope_children_indexed(fn)
            edges.extend(
                self._reaching_definitions(
                    fn_node=fn,
                    scope_nodes=scope_children,
                    cfg_successors=cfg_successors,
                    cfg_predecessors=cfg_predecessors,
                )
            )

        logger.info("DFG: generated %d REACHES edges", len(edges))
        return edges

    # -- Reaching-definitions core -----------------------------------------

    def _reaching_definitions(
        self,
        fn_node: CPGNode,
        scope_nodes: list[CPGNode],
        cfg_successors: dict[str, list[str]],
        cfg_predecessors: dict[str, list[str]],
    ) -> list[CPGEdge]:
        """Run reaching-definitions analysis for a single method."""
        if not scope_nodes:
            return []

        node_ids_in_scope = {n.id for n in scope_nodes}
        node_ids_in_scope.add(fn_node.id)

        local_succs: dict[str, list[str]] = {
            nid: [s for s in succs if s in node_ids_in_scope]
            for nid, succs in cfg_successors.items()
            if nid in node_ids_in_scope
        }
        local_preds: dict[str, list[str]] = {
            nid: [p for p in preds if p in node_ids_in_scope]
            for nid, preds in cfg_predecessors.items()
            if nid in node_ids_in_scope
        }

        cfg_node_ids = set(local_succs.keys()) | set(local_preds.keys())
        if not cfg_node_ids:
            return []

        gen: dict[str, set[tuple[str, str]]] = {}
        kill: dict[str, set[str]] = {}
        all_defs: dict[str, set[tuple[str, str]]] = {}

        def _register(var: str, def_id: str, cfg_owner: str) -> None:
            all_defs.setdefault(var, set()).add((var, def_id))
            gen.setdefault(cfg_owner, set()).add((var, def_id))
            kill.setdefault(cfg_owner, set()).add(var)

        # Method parameters are definitions live at the entry (fn_node) point.
        for var, def_id in self._collect_parameters(fn_node):
            _register(var, def_id, fn_node.id)

        for node in scope_nodes:
            ntype = node.properties.get("type", "")
            assign_nodes: list[CPGNode] = []
            if ntype in _ASSIGN_TYPES:
                assign_nodes = [node]
            elif node.id in cfg_node_ids:
                assign_nodes = self._direct_assignment_children(node.id)

            for anode in assign_nodes:
                cfg_owner = node.id if node.id in cfg_node_ids else anode.id
                for var in _extract_definition_targets(anode):
                    _register(var, anode.id, cfg_owner)

        # Worklist: in[n] = union(out[pred]); out[n] = gen[n] | (in[n] - kill[n]).
        in_sets: dict[str, set[tuple[str, str]]] = {n.id: set() for n in scope_nodes}
        out_sets: dict[str, set[tuple[str, str]]] = {n.id: set() for n in scope_nodes}
        in_sets.setdefault(fn_node.id, set())
        out_sets[fn_node.id] = gen.get(fn_node.id, set()).copy()

        initial = [nid for nid in (n.id for n in scope_nodes) if nid in cfg_node_ids]
        worklist: deque[str] = deque(initial)
        queued: set[str] = set(initial)

        max_iterations = len(scope_nodes) * 4 + 10
        iteration = 0
        while worklist and iteration < max_iterations:
            iteration += 1
            nid = worklist.popleft()
            queued.discard(nid)

            new_in: set[tuple[str, str]] = set()
            for pred_id in local_preds.get(nid, []):
                new_in |= out_sets.get(pred_id, set())
            in_sets[nid] = new_in

            node_kill = kill.get(nid, set())
            surviving = {pair for pair in new_in if pair[0] not in node_kill}
            new_out = surviving | gen.get(nid, set())

            if new_out != out_sets.get(nid, set()):
                out_sets[nid] = new_out
                for succ_id in local_succs.get(nid, []):
                    if succ_id not in queued:
                        worklist.append(succ_id)
                        queued.add(succ_id)

        return self._emit_use_edges(scope_nodes, in_sets, all_defs, cfg_node_ids)

    def _emit_use_edges(
        self,
        scope_nodes: list[CPGNode],
        in_sets: dict[str, set[tuple[str, str]]],
        all_defs: dict[str, set[tuple[str, str]]],
        cfg_node_ids: set[str],
    ) -> list[CPGEdge]:
        """Emit ``REACHES`` edges from reaching definitions to identifier uses."""
        edges: list[CPGEdge] = []
        seen: set[tuple[str, str]] = set()

        for node in scope_nodes:
            if node.properties.get("type") != "identifier":
                continue
            name = str(node.properties.get("code", ""))
            if not name.isidentifier():
                continue

            # Locals shadow fields (matching Java scoping).
            if name in all_defs:
                var_key = name
            elif f"field:{name}" in all_defs:
                var_key = f"field:{name}"
            else:
                continue

            stmt_id = self._find_cfg_ancestor(node.id, cfg_node_ids)
            reaching: set[str] = set()
            if stmt_id is not None:
                for v, def_id in in_sets.get(stmt_id, set()):
                    if v == var_key:
                        reaching.add(def_id)
            else:
                reaching = {def_id for _v, def_id in all_defs.get(var_key, set())}

            for def_id in reaching:
                if def_id == node.id:
                    continue
                pair = (def_id, node.id)
                if pair in seen:
                    continue
                seen.add(pair)
                edges.append(
                    CPGEdge(
                        source_id=def_id,
                        target_id=node.id,
                        edge_type=EdgeType.REACHES,
                        properties=MappingProxyType({"variable": name}),
                    )
                )
        return edges

    # -- Helpers -----------------------------------------------------------

    def _collect_parameters(self, fn_node: CPGNode) -> list[tuple[str, str]]:
        """Return ``(name, node_id)`` for each formal parameter of *fn_node*."""
        result: list[tuple[str, str]] = []
        for cid in self._parent_children.get(fn_node.id, []):
            child = self._node_map.get(cid)
            if child is None or child.properties.get("type") != "formal_parameters":
                continue
            for pid in self._parent_children.get(child.id, []):
                pnode = self._node_map.get(pid)
                if pnode is None:
                    continue
                if pnode.properties.get("type") not in {"formal_parameter", "spread_parameter"}:
                    continue
                name = _param_name(pnode, self._parent_children, self._node_map)
                if name:
                    result.append((name, pnode.id))
        return result

    def _direct_assignment_children(self, nid: str) -> list[CPGNode]:
        """Return immediate assignment-like children of CFG node *nid*."""
        result: list[CPGNode] = []
        for child_id in self._parent_children.get(nid, []):
            child = self._node_map.get(child_id)
            if child is not None and child.properties.get("type") in _ASSIGN_TYPES:
                result.append(child)
        return result

    def _find_cfg_ancestor(self, node_id: str, cfg_node_ids: set[str]) -> str | None:
        """Walk up PARENT_OF to find the nearest CFG-connected ancestor."""
        current = node_id
        for _ in range(40):
            parent_id = self._child_to_parent.get(current)
            if parent_id is None:
                return None
            if parent_id in cfg_node_ids:
                return parent_id
            current = parent_id
        return None

    def _scope_children_indexed(self, fn_node: CPGNode) -> list[CPGNode]:
        """Index-based O(subtree) scope-children lookup."""
        result: list[CPGNode] = []
        visited: set[str] = set()
        stack = list(self._parent_children.get(fn_node.id, []))
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            node = self._node_map.get(node_id)
            if node is None:
                continue
            result.append(node)
            stack.extend(self._parent_children.get(node_id, []))
        return sorted(result, key=lambda n: int(n.properties.get("line_start", 0)))

    # -- Legacy fallback ---------------------------------------------------

    def _build_fallback(self, cfg_nodes: list[CPGNode]) -> list[CPGEdge]:
        """Line-ordered last-def-before-use heuristic (no CFG/PARENT_OF edges)."""
        edges: list[CPGEdge] = []
        for fn in (n for n in cfg_nodes if n.has_label("Method")):
            fn_children = self._scope_children_scan(fn, cfg_nodes)
            edges.extend(_build_def_use(fn_children))
        logger.info("DFG (fallback): generated %d REACHES edges", len(edges))
        return edges

    @staticmethod
    def _scope_children_scan(fn_node: CPGNode, all_nodes: list[CPGNode]) -> list[CPGNode]:
        """Fallback O(n) line-range scan for scope-children lookup."""
        fn_start = int(fn_node.properties.get("line_start", 0))
        fn_end = int(fn_node.properties.get("line_end", 0))
        children: list[CPGNode] = []
        for n in all_nodes:
            if n.id == fn_node.id:
                continue
            n_start = int(n.properties.get("line_start", 0))
            n_end = int(n.properties.get("line_end", 0))
            if n_start >= fn_start and n_end <= fn_end:
                children.append(n)
        return sorted(children, key=lambda n: int(n.properties.get("line_start", 0)))


def _param_name(
    pnode: CPGNode,
    parent_children: dict[str, list[str]],
    node_map: dict[str, CPGNode],
) -> str | None:
    """Resolve a formal-parameter name via its identifier child."""
    name = pnode.properties.get("name")
    if name:
        return str(name)
    for cid in parent_children.get(pnode.id, []):
        child = node_map.get(cid)
        if child is not None and child.properties.get("type") == "identifier":
            code = child.properties.get("code")
            if code:
                return str(code)
    return None


def _extract_definition_targets(node: CPGNode) -> list[str]:
    """Return all variable names defined by *node* (possibly several)."""
    node_type = node.properties.get("type", "")

    if node_type in {"formal_parameter", "spread_parameter"}:
        name = node.properties.get("name")
        return [str(name)] if name else []

    if node_type == "local_variable_declaration":
        var_names = node.properties.get("var_names")
        if var_names:
            return [str(v) for v in var_names]
        code = str(node.properties.get("code", ""))
        lhs = code.split("=", maxsplit=1)[0].strip() if "=" in code else code.rstrip(";").strip()
        parts = lhs.split()
        if len(parts) >= 2 and parts[-1].isidentifier():
            return [parts[-1]]
        return []

    if node_type == "assignment_expression":
        target = node.properties.get("assign_target")
        if target:
            if node.properties.get("assign_kind") == "field":
                return [f"field:{target}"]
            return [str(target)]
        code = str(node.properties.get("code", ""))
        if "=" in code:
            lhs = code.split("=", maxsplit=1)[0].strip()
            if lhs.isidentifier():
                return [lhs]
        return []

    return []


def _extract_definition_target(node: CPGNode) -> str | None:
    """Return the first variable name defined by *node* (legacy helper)."""
    targets = _extract_definition_targets(node)
    return targets[0] if targets else None


def _build_def_use(nodes: list[CPGNode]) -> list[CPGEdge]:
    """Legacy last-def-before-use analysis on a line-ordered node list."""
    edges: list[CPGEdge] = []

    defs: dict[str, str] = {}
    for node in nodes:
        node_type = node.properties.get("type", "")
        if node_type in _DEF_TYPES:
            for name in _extract_definition_targets(node):
                defs[name] = node.id

    for node in nodes:
        if node.properties.get("type") != "identifier":
            continue
        var_name = node.properties.get("code", "")
        if var_name in defs:
            def_id = defs[var_name]
            if def_id != node.id:
                edges.append(
                    CPGEdge(
                        source_id=def_id,
                        target_id=node.id,
                        edge_type=EdgeType.REACHES,
                        properties=MappingProxyType({"variable": var_name}),
                    )
                )

    return edges
