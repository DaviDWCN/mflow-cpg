"""DFGBuilder — derives data-flow (REACHES) edges using reaching-definitions analysis.

Algorithm
---------
The builder performs a classic **dataflow analysis** (gen/kill on a CFG):

1. Build a CFG successor map from the supplied ``FLOWS_TO`` edges.
2. For each function, run a worklist-based **reaching-definitions** pass:
   - Each CFG node has an *in-set* (set of ``(var_name, def_node_id)`` pairs
     that reach its entry) and an *out-set* (set that leave its exit).
   - Gen/Kill semantics: an assignment node **kills** all prior definitions of
     that variable and **generates** a new one.
3. After convergence, for every ``identifier`` node inside the function, if
   its name is in the in-set of its CFG-predecessor graph position, emit a
   ``REACHES`` edge from the definition node to the identifier node.

Compared to the previous MVP this correctly handles:

* **Branch-join merge**: both the if-branch and else-branch definitions reach
  use-sites after the join point.
* **Loop-carried definitions**: a definition inside a loop body can reach uses
  on subsequent loop iterations.
* **try/except merges**: definitions in try or except branches both reach uses
  after the block.

Limitations (still intentional for this version):

* Intra-procedural only (no inter-procedural / closure / global analysis).
* No type inference — uses plain identifier text matching.
* Augmented-assignment (``x += 1``) treated as both use and def.
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
_DEF_TYPES = frozenset({"assignment", "augmented_assignment"})

# A definition is a (variable_name, def_node_id) pair.
# DefSet = frozenset[tuple[str, str]]


class DFGBuilder:
    """Build intra-procedural data-flow edges via reaching-definitions analysis.

    The builder uses the ``FLOWS_TO`` edges emitted by :class:`CFGBuilder` to
    traverse the control-flow graph and compute, for each program point, the
    set of variable definitions that *reach* that point through all possible
    execution paths.

    This replaces the previous "last-def-before-use" heuristic with a proper
    worklist-based fixed-point computation so that branches and loops are
    handled correctly.
    """

    def __init__(self) -> None:
        """Initialise builder state."""
        self._node_map: dict[str, CPGNode] = {}
        self._parent_children: dict[str, list[str]] = {}
        self._child_to_parent: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def build(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Return ``REACHES`` edges for each def→use pair.

        Args:
            cfg_nodes: All AST/CFG nodes (from the combined AST + CFG output).
            cfg_edges:  ``FLOWS_TO`` edges from :class:`CFGBuilder`.  These are
                used to build the CFG successor/predecessor maps for the
                reaching-definitions analysis.
            ast_edges: ``PARENT_OF`` edges for O(1) subtree traversal.
                When *None* a O(n) line-range scan is used as fallback.

        Returns:
            A deduplicated list of ``REACHES`` edges.
        """
        # Build index structures.
        self._node_map = {n.id: n for n in cfg_nodes}
        self._parent_children = {}
        self._child_to_parent = {}
        if ast_edges:
            for e in ast_edges:
                self._parent_children.setdefault(e.source_id, []).append(e.target_id)
                self._child_to_parent[e.target_id] = e.source_id

        # Build CFG successor map from FLOWS_TO edges.
        cfg_successors: dict[str, list[str]] = {}
        cfg_predecessors: dict[str, list[str]] = {}
        for e in cfg_edges:
            if e.edge_type == EdgeType.FLOWS_TO:
                cfg_successors.setdefault(e.source_id, []).append(e.target_id)
                cfg_predecessors.setdefault(e.target_id, []).append(e.source_id)

        edges: list[CPGEdge] = []
        # Analyze both Method nodes and the Module node (for global scope).
        scopes = [n for n in cfg_nodes if n.has_label("Method") or n.has_label("Module")]

        total_scopes = len(scopes)
        for idx, scope in enumerate(scopes, start=1):
            t_scope = __import__("time").monotonic()
            scope_name = scope.properties.get("name") or scope.properties.get("type") or scope.id
            logger.info("DFG scope %d/%d start: %s", idx, total_scopes, scope_name)
            scope_children = self._scope_children(scope, cfg_nodes)
            scope_edges = self._reaching_definitions(
                fn_node=scope,
                scope_nodes=scope_children,
                cfg_successors=cfg_successors,
                cfg_predecessors=cfg_predecessors,
            )
            edges.extend(scope_edges)
            logger.info(
                "DFG scope %d/%d done: %s -> %d REACHES edges (%.2fs)",
                idx,
                total_scopes,
                scope_name,
                len(scope_edges),
                __import__("time").monotonic() - t_scope,
            )

        logger.info("DFG: generated %d REACHES edges", len(edges))
        return edges

    # ── Reaching-Definitions Core ─────────────────────────────────────────

    def _reaching_definitions(
        self,
        fn_node: CPGNode,
        scope_nodes: list[CPGNode],
        cfg_successors: dict[str, list[str]],
        cfg_predecessors: dict[str, list[str]],
    ) -> list[CPGEdge]:
        """Run reaching-definitions analysis for a single function.

        Returns ``REACHES`` edges from definition nodes to identifier-use nodes.
        """
        if not scope_nodes:
            return []

        # Only consider nodes that are actually in the CFG graph (connected to
        # FLOWS_TO edges).  Statement-level nodes are the interesting ones.
        node_ids_in_scope = {n.id for n in scope_nodes}
        node_ids_in_scope.add(fn_node.id)

        # Filter CFG maps to only nodes in this function's scope.
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

        # Nodes that actually participate in the CFG (have FLOWS_TO edges).
        cfg_node_ids = set(local_succs.keys()) | set(local_preds.keys())
        if not cfg_node_ids:
            # No control-flow for this scope (e.g. module-level script code when
            # CFG is method-only), so we skip DFG to avoid dense fallback edges.
            return []

        # Compute gen/kill for each CFG-relevant node.
        # Gen and kill must operate on the CFG-level nodes (expression_statement,
        # return_statement, etc.), so we look at both the node itself *and* its
        # immediate children (e.g. the 'assignment' child of 'expression_statement').
        gen: dict[str, set[tuple[str, str]]] = {}
        kill: dict[str, set[str]] = {}
        all_defs: dict[str, list[tuple[str, str]]] = {}  # var_name -> [(var,def_id)]

        # Build a map: cfg_node_id -> [direct assignment children]
        def _direct_assignment_children(nid: str) -> list[CPGNode]:
            """Return immediate children of *nid* that are assignment nodes."""
            result = []
            for child_id in self._parent_children.get(nid, []):
                child = self._node_map.get(child_id)
                if child and child.properties.get("type") in _DEF_TYPES:
                    result.append(child)
            return result

        # Treat parameters as initial definitions for Method scopes.
        if fn_node.has_label("Method"):
            # Find identifiers in the parameters subtree.
            params_node_id = next(
                (
                    cid
                    for cid in self._parent_children.get(fn_node.id, [])
                    if (n := self._node_map.get(cid)) is not None
                    and n.properties.get("type") == "parameters"
                ),
                None,
            )
            if params_node_id:
                # We reuse the logic for finding identifier nodes in parameters
                # (simplifying here for brevity, but logically reaching the identifiers)
                for pid in self._parent_children.get(params_node_id, []):
                    pnode = self._node_map.get(pid)
                    if not pnode:
                        continue
                    # Handle identifier directly or inside typed/default_parameter
                    if pnode.properties.get("type") == "identifier":
                        ids = [pnode]
                    else:
                        ids = [
                            child
                            for cid in self._parent_children.get(pnode.id, [])
                            if (child := self._node_map.get(cid)) is not None
                            and child.properties.get("type") == "identifier"
                        ]
                    for p_id_node in ids:
                        var_name = p_id_node.properties.get("code")
                        if var_name:
                            all_defs.setdefault(var_name, []).append((var_name, p_id_node.id))
                            gen.setdefault(fn_node.id, set()).add((var_name, p_id_node.id))
                            kill.setdefault(fn_node.id, set()).add(var_name)

        for node in scope_nodes:
            ntype = node.properties.get("type", "")
            # Direct assignment
            assign_nodes: list[CPGNode] = []
            if ntype in _DEF_TYPES:
                assign_nodes = [node]
            elif node.id in cfg_node_ids:
                # expression_statement wrapping an assignment, etc.
                assign_nodes = _direct_assignment_children(node.id)

            for anode in assign_nodes:
                var_name = _extract_assignment_target(anode)
                if var_name:
                    # We register the assignment node id as the def, but the
                    # gen belongs to the CFG-level parent (so worklist propagates it).
                    cfg_owner = node.id if node.id in cfg_node_ids else anode.id
                    all_defs.setdefault(var_name, []).append((var_name, anode.id))
                    gen.setdefault(cfg_owner, set()).add((var_name, anode.id))
                    kill.setdefault(cfg_owner, set()).add(var_name)

        # Build all_defs_set: all (var, def_id) pairs in this function.
        all_defs_flat: dict[str, set[tuple[str, str]]] = {
            v: set(pairs) for v, pairs in all_defs.items()
        }

        # Worklist algorithm: in[n] = Union(out[p] for each predecessor p)
        # out[n] = gen[n] | (in[n] - kill[n])
        in_sets: dict[str, set[tuple[str, str]]] = {n.id: set() for n in scope_nodes}
        out_sets: dict[str, set[tuple[str, str]]] = {n.id: set() for n in scope_nodes}

        initial_nodes = [nid for nid in (n.id for n in scope_nodes) if nid in cfg_node_ids]
        worklist: deque[str] = deque(initial_nodes)
        # Use a parallel set for O(1) membership checks (deque.__contains__ is O(n)).
        in_worklist: set[str] = set(initial_nodes)

        max_iterations = len(scope_nodes) * 4 + 10  # safety bound
        iteration = 0
        while worklist and iteration < max_iterations:
            iteration += 1
            nid = worklist.popleft()
            in_worklist.discard(nid)

            # in[n] = union of out[pred] for all predecessors
            new_in: set[tuple[str, str]] = set()
            for pred_id in local_preds.get(nid, []):
                new_in |= out_sets.get(pred_id, set())
            in_sets[nid] = new_in

            # out[n] = gen[n] | (in[n] - {(v,d) | v in kill[n]})
            node_kill = kill.get(nid, set())
            surviving = {pair for pair in new_in if pair[0] not in node_kill}
            new_out = surviving | gen.get(nid, set())

            if new_out != out_sets[nid]:
                out_sets[nid] = new_out
                # Re-enqueue successors that are not already queued.
                for succ_id in local_succs.get(nid, []):
                    if succ_id not in in_worklist:
                        worklist.append(succ_id)
                        in_worklist.add(succ_id)

        # Now emit REACHES edges: for each identifier node, look up what defs
        # reach it from its predecessors in the CFG.
        edges: list[CPGEdge] = []
        seen_pairs: set[tuple[str, str]] = set()

        for node in scope_nodes:
            if node.properties.get("type") != "identifier":
                continue
            var_name = str(node.properties.get("code", ""))
            if not var_name or not var_name.isidentifier():
                continue
            if var_name not in all_defs_flat:
                continue

            # Collect reaching defs from predecessors (or from in-set of the
            # containing statement).  Since identifiers themselves are not
            # statement-level CFG nodes, we use the in-set of the statement
            # that is the nearest CFG-connected ancestor.
            reaching: set[str] = set()
            containing_stmt_id = self._find_cfg_ancestor(node.id, cfg_node_ids)
            if containing_stmt_id:
                for v, def_id in in_sets.get(containing_stmt_id, set()):
                    if v == var_name:
                        reaching.add(def_id)
            else:
                # If no CFG ancestor found, fall back: use all defs of this var.
                for _v, def_id in all_defs_flat.get(var_name, set()):
                    reaching.add(def_id)

            for def_id in reaching:
                if def_id == node.id:
                    continue  # skip self-edge
                pair = (def_id, node.id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                edges.append(
                    CPGEdge(
                        source_id=def_id,
                        target_id=node.id,
                        edge_type=EdgeType.REACHES,
                        properties=MappingProxyType({"variable": var_name}),
                    )
                )

        # New: Emit intra-statement value flow (RHS -> LHS)
        for node in scope_nodes:
            ntype = node.properties.get("type", "")
            if ntype in _DEF_TYPES:
                # Find LHS identifier and RHS expression children
                raw_children = [
                    self._node_map.get(cid) for cid in self._parent_children.get(node.id, [])
                ]
                children = [c for c in raw_children if c is not None]
                lhs = None
                rhs = None
                # Tree-sitter python assignment with named children: [lhs, rhs]
                # (The operator '=' is not named)
                if len(children) >= 2:
                    lhs = children[0]
                    rhs = children[1]

                if lhs and rhs:
                    # Link RHS value flow to the assignment node itself.
                    # This allows a continuous path: RHS -> Assignment -> UseSite
                    edges.append(
                        CPGEdge(
                            source_id=rhs.id,
                            target_id=node.id,
                            edge_type=EdgeType.REACHES,
                            properties=MappingProxyType({"assignment": "value_flow"}),
                        )
                    )

            elif ntype == "return_statement":
                # Connect the expression child to the return statement itself
                children = [
                    c
                    for cid in self._parent_children.get(node.id, [])
                    if (c := self._node_map.get(cid)) is not None
                ]
                if children:
                    # Usually 'return' token followed by expression
                    val_node = children[-1]
                    if val_node and val_node.id != node.id:
                        edges.append(
                            CPGEdge(
                                source_id=val_node.id,
                                target_id=node.id,
                                edge_type=EdgeType.REACHES,
                                properties=MappingProxyType({"return": "value_flow"}),
                            )
                        )

        return edges

    def _find_cfg_ancestor(self, node_id: str, cfg_node_ids: set[str]) -> str | None:
        """Walk up the PARENT_OF tree to find the nearest ancestor that is a CFG node.

        This bridges the gap between fine-grained ``identifier`` AST nodes and
        the statement-level nodes that appear in the CFG graph.
        """
        if not self._child_to_parent:
            return None

        current = node_id
        for _ in range(30):  # depth guard
            parent_id = self._child_to_parent.get(current)
            if parent_id is None:
                return None
            if parent_id in cfg_node_ids:
                return parent_id
            current = parent_id
        return None

    # ── Scope-collection helpers ──────────────────────────────────────────

    def _scope_children(self, fn_node: CPGNode, all_nodes: list[CPGNode]) -> list[CPGNode]:
        """Return nodes contained within *fn_node*'s scope, sorted by line start."""
        is_method = fn_node.has_label("Method")
        if self._parent_children:
            return self._scope_children_indexed(fn_node, stop_at_nested_methods=not is_method)
        return self._scope_children_scan(fn_node, all_nodes)

    def _scope_children_indexed(
        self, fn_node: CPGNode, stop_at_nested_methods: bool = False
    ) -> list[CPGNode]:
        """Index-based O(subtree) scope-children lookup.

        When *stop_at_nested_methods* is True (i.e. the scope is a Module or
        other non-Method node), traversal stops at Method boundaries so that
        function body nodes are not included in the Module scope.  This
        prevents the worklist from re-processing every function body under the
        Module scope — the primary cause of the DFG analysis deadlock.
        """
        result: list[CPGNode] = []
        stack = list(self._parent_children.get(fn_node.id, []))
        visited: set[str] = set()
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            node = self._node_map.get(node_id)
            if node is not None:
                if stop_at_nested_methods and node.has_label("Method"):
                    # Do not recurse into nested Method subtrees; those are
                    # analysed independently in their own scope iteration.
                    continue
                result.append(node)
                stack.extend(self._parent_children.get(node_id, []))
        return sorted(result, key=lambda n: int(n.properties.get("line_start", 0)))

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


def _extract_assignment_target(node: CPGNode) -> str | None:
    """Extract the target variable name from an assignment/augmented-assignment node.

    For simple assignments like ``x = 1`` the code property is ``'x = 1'``.
    For augmented assignments like ``x += 1`` the code is ``'x += 1'``.
    Returns the LHS identifier for simple left-hand sides, or ``None`` for
    complex targets (tuple unpacking, attribute assignment, subscript, etc.).
    """
    code = str(node.properties.get("code", ""))
    # Split on the first '=' (handles both '=' and '+=' / '-=' etc.)
    if "=" not in code:
        return None
    # Strip off operator prefix (e.g. '+=') before splitting.
    lhs = code.split("=", maxsplit=1)[0].rstrip("+-*/%&|^~<>")
    lhs = lhs.strip()
    if lhs.isidentifier():
        return lhs
    return None
