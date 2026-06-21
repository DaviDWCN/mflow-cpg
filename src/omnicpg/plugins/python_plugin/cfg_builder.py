"""CFGBuilder — derives control-flow (FLOWS_TO) edges from AST nodes.

Performance
-----------
When ``PARENT_OF`` edges are supplied (the *ast_edges* parameter), the builder
constructs a ``parent_id → [child_ids]`` index so that every child-lookup is
**O(1)** instead of an **O(n)** full-list scan.
"""

from __future__ import annotations

import logging
from collections import deque
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.utils.id_gen import generate_deterministic_id_from_key

if TYPE_CHECKING:
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)

# AST node types that represent executable statements.
_STATEMENT_TYPES = frozenset(
    {
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
    }
)

# Compound statements that branch control flow.
_BRANCH_TYPES = frozenset({"if_statement"})

# Compound statements that form loops.
_LOOP_TYPES = frozenset({"for_statement", "while_statement"})

# Exception handling statements.
_TRY_TYPES = frozenset({"try_statement"})

# Union of all CFG-relevant statement types.
_ALL_CFG_TYPES = _STATEMENT_TYPES | _BRANCH_TYPES | _LOOP_TYPES | _TRY_TYPES


class CFGBuilder:
    """Build intra-procedural control-flow edges from AST nodes.

    For each function the builder creates synthetic **ENTRY** and **EXIT**
    nodes so that every CFG has a single entry and a single exit point.

    Supported constructs:

    * Sequential statement flow.
    * ``if`` / ``else`` branching (with ``condition`` property on edges).
    * ``for`` / ``while`` loops (back-edge from loop body to condition).
    """

    def __init__(self) -> None:
        """Initialise builder state."""
        self._node_map: dict[str, CPGNode] = {}
        self._parent_children: dict[str, list[str]] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def build(
        self,
        ast_nodes: list[CPGNode],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Return ``FLOWS_TO`` edges for every function body in *ast_nodes*.

        Args:
            ast_nodes: Flat list of AST :class:`CPGNode` instances.
            ast_edges: ``PARENT_OF`` edges from the AST builder.  When
                provided, an O(1) parent→children index is used instead
                of scanning the full node list for every lookup.

        Returns:
            A list of ``FLOWS_TO`` edges.
        """
        # Build O(1) lookup index from PARENT_OF edges.
        if ast_edges is not None:
            self._node_map = {n.id: n for n in ast_nodes}
            self._parent_children = {}
            for edge in ast_edges:
                self._parent_children.setdefault(edge.source_id, []).append(edge.target_id)
        else:
            self._node_map = {}
            self._parent_children = {}

        edges: list[CPGEdge] = []
        functions = [n for n in ast_nodes if n.has_label("Method")]
        for fn_node in functions:
            fn_edges = self._build_function_cfg(fn_node, ast_nodes)
            edges.extend(fn_edges)
        logger.info("CFG: generated %d FLOWS_TO edges", len(edges))
        return edges

    # ── Private helpers ───────────────────────────────────────────────────

    def _build_function_cfg(self, fn_node: CPGNode, all_nodes: list[CPGNode]) -> list[CPGEdge]:
        """Build CFG for a single function."""
        edges: list[CPGEdge] = []

        # Collect direct statement children of the function body.
        body_stmts = self._collect_body_statements(fn_node, all_nodes)
        if not body_stmts:
            return edges

        # Create synthetic ENTRY / EXIT nodes (kept as plain node IDs).
        entry_id = generate_deterministic_id_from_key(f"{fn_node.id}:entry")
        exit_id = generate_deterministic_id_from_key(f"{fn_node.id}:exit")

        # ENTRY → first statement
        edges.append(self._flow_edge(entry_id, body_stmts[0].id))

        # Walk statements
        self._connect_statements(body_stmts, edges, all_nodes, exit_id)

        # Last statement → EXIT (if not already connected via branch)
        last = body_stmts[-1]
        if last.properties.get("type") not in {"return_statement"}:
            edges.append(self._flow_edge(last.id, exit_id))

        return edges

    def _connect_statements(
        self,
        stmts: list[CPGNode],
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
    ) -> None:
        """Connect a list of sequential statements with FLOWS_TO edges."""
        if visited is None:
            visited = set()
        if id(stmts) in visited:
            return
        visited.add(id(stmts))
        for i, stmt in enumerate(stmts):
            stmt_type = stmt.properties.get("type", "")
            next_id = stmts[i + 1].id if i + 1 < len(stmts) else exit_id

            if stmt_type in _BRANCH_TYPES:
                self._handle_branch(stmt, next_id, edges, all_nodes, exit_id, visited)
            elif stmt_type in _LOOP_TYPES:
                self._handle_loop(stmt, next_id, edges, all_nodes, visited)
            elif stmt_type in _TRY_TYPES:
                self._handle_try(stmt, next_id, edges, all_nodes, exit_id, visited)
            else:
                # Sequential flow: stmt → next stmt
                if i + 1 < len(stmts):
                    edges.append(self._flow_edge(stmt.id, stmts[i + 1].id))

    def _handle_branch(
        self,
        if_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
    ) -> None:
        """Generate edges for an ``if`` / ``else`` statement."""
        if visited is None:
            visited = set()
        node_id = id(if_node)
        if node_id in visited:
            return
        visited.add(node_id)

        children = self._children_of(if_node, all_nodes)

        # Attempt to find 'block' children — then-branch and else-branch.
        blocks = [c for c in children if c.properties.get("type") == "block"]
        then_stmts = self._collect_body_statements(blocks[0], all_nodes) if blocks else []
        else_stmts = self._collect_body_statements(blocks[1], all_nodes) if len(blocks) > 1 else []

        # Also check for elif / else_clause children.
        else_clauses = [
            c for c in children if c.properties.get("type") in {"else_clause", "elif_clause"}
        ]
        if not else_stmts and else_clauses:
            for clause in else_clauses:
                clause_blocks = [
                    c2
                    for c2 in self._children_of(clause, all_nodes)
                    if c2.properties.get("type") == "block"
                ]
                if clause_blocks:
                    else_stmts = self._collect_body_statements(clause_blocks[0], all_nodes)
                    break

        # True branch
        if then_stmts:
            edges.append(self._flow_edge(if_node.id, then_stmts[0].id, condition="True"))
            self._connect_statements(then_stmts, edges, all_nodes, exit_id, visited)
            edges.append(self._flow_edge(then_stmts[-1].id, after_id))
        else:
            edges.append(self._flow_edge(if_node.id, after_id, condition="True"))

        # False branch
        if else_stmts:
            edges.append(self._flow_edge(if_node.id, else_stmts[0].id, condition="False"))
            self._connect_statements(else_stmts, edges, all_nodes, exit_id, visited)
            edges.append(self._flow_edge(else_stmts[-1].id, after_id))
        else:
            edges.append(self._flow_edge(if_node.id, after_id, condition="False"))

    def _handle_loop(
        self,
        loop_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        visited: set[int] | None = None,
    ) -> None:
        """Generate edges for ``for`` / ``while`` loops."""
        if visited is None:
            visited = set()
        node_id = id(loop_node)
        if node_id in visited:
            return
        visited.add(node_id)

        children = self._children_of(loop_node, all_nodes)
        blocks = [c for c in children if c.properties.get("type") == "block"]
        body_stmts = self._collect_body_statements(blocks[0], all_nodes) if blocks else []

        if body_stmts:
            edges.append(self._flow_edge(loop_node.id, body_stmts[0].id, condition="True"))
            for i, stmt in enumerate(body_stmts):
                stmt_type = stmt.properties.get("type", "")
                next_id = body_stmts[i + 1].id if i + 1 < len(body_stmts) else loop_node.id
                if stmt_type in _BRANCH_TYPES:
                    self._handle_branch(stmt, next_id, edges, all_nodes, after_id, visited)
                elif stmt_type in _LOOP_TYPES:
                    self._handle_loop(stmt, next_id, edges, all_nodes, visited)
                else:
                    if i + 1 < len(body_stmts):
                        edges.append(self._flow_edge(stmt.id, body_stmts[i + 1].id))
            edges.append(self._flow_edge(body_stmts[-1].id, loop_node.id))

        edges.append(self._flow_edge(loop_node.id, after_id, condition="False"))

    def _handle_try(
        self,
        try_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
    ) -> None:
        """Generate edges for a ``try`` / ``except`` statement."""
        if visited is None:
            visited = set()
        node_id = id(try_node)
        if node_id in visited:
            return
        visited.add(node_id)

        children = self._children_of(try_node, all_nodes)

        # Primary block is the direct try_block
        try_blocks = [c for c in children if c.properties.get("type") == "block"]
        try_stmts = self._collect_body_statements(try_blocks[0], all_nodes) if try_blocks else []

        except_clauses = [c for c in children if c.properties.get("type") == "except_clause"]
        else_clauses = [c for c in children if c.properties.get("type") == "else_clause"]
        finally_clauses = [c for c in children if c.properties.get("type") == "finally_clause"]

        else_stmts = []
        if else_clauses:
            eblocks = [
                c
                for c in self._children_of(else_clauses[-1], all_nodes)
                if c.properties.get("type") == "block"
            ]
            if eblocks:
                else_stmts = self._collect_body_statements(eblocks[0], all_nodes)

        finally_stmts = []
        if finally_clauses:
            fblocks = [
                c
                for c in self._children_of(finally_clauses[-1], all_nodes)
                if c.properties.get("type") == "block"
            ]
            if fblocks:
                finally_stmts = self._collect_body_statements(fblocks[0], all_nodes)

        # The node immediately after the entire try statement blocks (before finally or after_id)
        # If there's finally, we always go to finally before leaving.
        post_try_id = finally_stmts[0].id if finally_stmts else after_id

        # Edge from try_statement to first statement of try_block
        if try_stmts:
            edges.append(self._flow_edge(try_node.id, try_stmts[0].id))
            self._connect_statements(try_stmts, edges, all_nodes, exit_id, visited)

            # Normal flow (no exception) from try -> else (if exists) or post_try_id
            if else_stmts:
                edges.append(self._flow_edge(try_stmts[-1].id, else_stmts[0].id))
                self._connect_statements(else_stmts, edges, all_nodes, exit_id, visited)
                edges.append(self._flow_edge(else_stmts[-1].id, post_try_id))
            else:
                edges.append(self._flow_edge(try_stmts[-1].id, post_try_id))
        else:
            edges.append(self._flow_edge(try_node.id, post_try_id))

        # Handle except clauses
        for exc in except_clauses:
            exc_blocks = [
                c for c in self._children_of(exc, all_nodes) if c.properties.get("type") == "block"
            ]
            if exc_blocks:
                exc_stmts = self._collect_body_statements(exc_blocks[0], all_nodes)
                if exc_stmts:
                    # An exception can jump from 'try_node' abstraction to the except clause
                    edges.append(
                        self._flow_edge(try_node.id, exc_stmts[0].id, condition="Exception")
                    )
                    self._connect_statements(exc_stmts, edges, all_nodes, exit_id, visited)
                    edges.append(self._flow_edge(exc_stmts[-1].id, post_try_id))

        if finally_stmts:
            self._connect_statements(finally_stmts, edges, all_nodes, exit_id, visited)
            edges.append(self._flow_edge(finally_stmts[-1].id, after_id))

    # ── AST helpers ───────────────────────────────────────────────────────

    def _collect_body_statements(
        self, parent_node: CPGNode, all_nodes: list[CPGNode]
    ) -> list[CPGNode]:
        """Return direct statement-level children of *parent_node*.

        When the PARENT_OF index is available, performs a BFS through the
        tree and collects the first statement-type nodes found (O(subtree)).
        Falls back to line-range containment when no index exists.
        """
        if self._parent_children:
            return self._collect_body_statements_indexed(parent_node)
        return self._collect_body_statements_scan(parent_node, all_nodes)

    def _collect_body_statements_indexed(self, parent_node: CPGNode) -> list[CPGNode]:
        """Index-based O(subtree) implementation of body-statement collection.

        BFS from *parent_node* through the PARENT_OF tree, collecting the
        first statement-type nodes encountered and **not** descending into
        them (they are the "direct" statement children).
        """
        stmts: list[CPGNode] = []
        queue: deque[str] = deque(self._parent_children.get(parent_node.id, []))
        while queue:
            child_id = queue.popleft()
            child = self._node_map.get(child_id)
            if child is None:
                continue
            child_type = child.properties.get("type", "")
            if child_type in _ALL_CFG_TYPES:
                stmts.append(child)
            else:
                queue.extend(self._parent_children.get(child_id, []))
        return sorted(stmts, key=lambda n: int(n.properties.get("line_start", 0)))

    @staticmethod
    def _collect_body_statements_scan(
        parent_node: CPGNode, all_nodes: list[CPGNode]
    ) -> list[CPGNode]:
        """Fallback O(n) line-range scan for body-statement collection."""
        parent_start = int(parent_node.properties.get("line_start", 0))
        parent_end = int(parent_node.properties.get("line_end", 0))

        candidates: list[CPGNode] = []
        for node in all_nodes:
            if node.id == parent_node.id:
                continue
            node_type = node.properties.get("type", "")
            node_start = int(node.properties.get("line_start", 0))
            node_end = int(node.properties.get("line_end", 0))
            if node_type not in _ALL_CFG_TYPES:
                continue
            if node_start >= parent_start and node_end <= parent_end:
                candidates.append(node)

        return _filter_direct_children(candidates)

    def _children_of(self, parent: CPGNode, all_nodes: list[CPGNode]) -> list[CPGNode]:
        """Return AST children of *parent*.

        When the PARENT_OF index is available, returns direct tree children
        in O(1).  Falls back to line-range containment scan otherwise.
        """
        if self._parent_children:
            child_ids = self._parent_children.get(parent.id, [])
            return [self._node_map[cid] for cid in child_ids if cid in self._node_map]
        # Fallback: line-range scan.
        p_start = int(parent.properties.get("line_start", 0))
        p_end = int(parent.properties.get("line_end", 0))
        result: list[CPGNode] = []
        for n in all_nodes:
            if n.id == parent.id:
                continue
            n_start = int(n.properties.get("line_start", 0))
            n_end = int(n.properties.get("line_end", 0))
            if n_start >= p_start and n_end <= p_end:
                result.append(n)
        return result

    @staticmethod
    def _flow_edge(source_id: str, target_id: str, **extra: Any) -> CPGEdge:
        """Create a ``FLOWS_TO`` edge with optional extra properties."""
        props = MappingProxyType(extra) if extra else MappingProxyType({})
        return CPGEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=EdgeType.FLOWS_TO,
            properties=props,
        )


def _filter_direct_children(candidates: list[CPGNode]) -> list[CPGNode]:
    """Keep only nodes that are not nested inside another candidate.

    Two nodes A and B where A's line range strictly contains B's means B is
    nested.  We keep only the outermost ones and sort by ``line_start``.

    Uses an O(n log n) sweep-line approach: sort by start ascending, end
    descending, then track the current enclosing interval.  A node whose
    range fits inside the current enclosing interval is nested and skipped.
    """
    if not candidates:
        return []

    # Sort by start line (ascending), then by span size (descending) so that
    # outer statements come first.
    sorted_cands = sorted(
        candidates,
        key=lambda n: (
            int(n.properties.get("line_start", 0)),
            -int(n.properties.get("line_end", 0)),
        ),
    )

    direct: list[CPGNode] = []
    # Track the maximum *end* line of all accepted nodes so far.  Because
    # we sorted by (start ASC, end DESC), any node whose end is within the
    # current max_end is nested inside a previously accepted node.
    max_end = -1
    for node in sorted_cands:
        ne = int(node.properties.get("line_end", 0))
        if ne <= max_end:
            # Fully contained within a previously accepted (wider) node.
            continue
        direct.append(node)
        max_end = ne

    # Already sorted by line_start (maintained by the sweep).
    return direct
