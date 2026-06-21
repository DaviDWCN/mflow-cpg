"""CFGBuilder — derives control-flow (FLOWS_TO) edges from Java AST nodes.

Supports Java-specific constructs:

* Sequential statement flow.
* ``if`` / ``else`` branching.
* ``for`` / ``while`` / ``do-while`` / enhanced-for loops.
* ``try`` / ``catch`` / ``finally`` exception handling.
* ``switch`` statements (case / default).
* ``synchronized`` blocks.

Performance
-----------
When ``PARENT_OF`` edges are supplied (the *ast_edges* parameter), the builder
constructs a ``parent_id → [child_ids]`` index so that every child-lookup is
**O(1)** instead of an **O(n)** full-list scan.  For large Java files (100 k+
AST nodes) this reduces CFG construction from minutes to milliseconds.
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

# Java AST node types that represent executable statements.
_STATEMENT_TYPES = frozenset(
    {
        "expression_statement",
        "return_statement",
        "local_variable_declaration",
        "throw_statement",
        "assert_statement",
        "break_statement",
        "continue_statement",
    }
)

# Compound statements that branch control flow.
_BRANCH_TYPES = frozenset({"if_statement"})

# Loop statement types.
_LOOP_TYPES = frozenset(
    {
        "for_statement",
        "while_statement",
        "do_statement",
        "enhanced_for_statement",
    }
)

# Exception-handling statement.
_TRY_TYPES = frozenset({"try_statement", "try_with_resources_statement"})

# Switch statement.  Modern tree-sitter-java unifies old- and new-style
# switches under ``switch_expression``; ``switch_statement`` is kept for
# compatibility with older grammar versions.
_SWITCH_TYPES = frozenset({"switch_expression", "switch_statement"})

# Synchronized block.
_SYNCHRONIZED_TYPES = frozenset({"synchronized_statement"})

# Union of all CFG-relevant statement types.
_ALL_CFG_TYPES = (
    _STATEMENT_TYPES
    | _BRANCH_TYPES
    | _LOOP_TYPES
    | _TRY_TYPES
    | _SWITCH_TYPES
    | _SYNCHRONIZED_TYPES
)


class CFGBuilder:
    """Build intra-procedural control-flow edges from Java AST nodes.

    For each method/constructor the builder creates synthetic **ENTRY** and
    **EXIT** nodes so that every CFG has a single entry and a single exit point.

    Supported constructs:

    * Sequential statement flow.
    * ``if`` / ``else`` branching (with ``condition`` property on edges).
    * ``for`` / ``while`` / ``do`` / enhanced-for loops (back-edge from body
      to condition).
    * ``try`` / ``catch`` / ``finally`` exception handling.
    * ``switch`` statements.
    * ``synchronized`` blocks.
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
        """Return ``FLOWS_TO`` edges for every method body in *ast_nodes*.

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
        methods = [n for n in ast_nodes if n.has_label("Method")]
        for fn_node in methods:
            fn_edges = self._build_method_cfg(fn_node, ast_nodes)
            edges.extend(fn_edges)
        logger.info("CFG: generated %d FLOWS_TO edges", len(edges))
        return edges

    # ── Private helpers ───────────────────────────────────────────────────

    def _build_method_cfg(self, fn_node: CPGNode, all_nodes: list[CPGNode]) -> list[CPGEdge]:
        """Build CFG for a single method or constructor."""
        edges: list[CPGEdge] = []

        body_stmts = self._collect_body_statements(fn_node, all_nodes)
        if not body_stmts:
            return edges

        entry_id = generate_deterministic_id_from_key(f"{fn_node.id}:entry")
        exit_id = generate_deterministic_id_from_key(f"{fn_node.id}:exit")

        # ENTRY → first statement
        edges.append(self._flow_edge(entry_id, body_stmts[0].id))

        # Walk statements
        self._connect_statements(body_stmts, edges, all_nodes, exit_id)

        # Last statement → EXIT (unless it's a return/throw)
        last = body_stmts[-1]
        if last.properties.get("type") not in {"return_statement", "throw_statement"}:
            edges.append(self._flow_edge(last.id, exit_id))

        return edges

    def _connect_statements(
        self,
        stmts: list[CPGNode],
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
        loop_ctx: tuple[str, str] | None = None,
        tail_id: str | None = None,
    ) -> None:
        """Connect a list of sequential statements with FLOWS_TO edges.

        ``loop_ctx``, when provided, is a ``(break_target, continue_target)``
        pair describing the nearest enclosing loop (or ``switch`` for breaks).
        ``break``/``continue`` statements are then routed to the loop exit /
        loop header respectively instead of falling through to the next
        statement, matching Java control-flow semantics.

        ``tail_id``, when provided, is the target the **last** sequential
        statement flows to (e.g. a loop back-edge); callers that add the final
        edge themselves leave it ``None``.
        """
        if visited is None:
            visited = set()
        if id(stmts) in visited:
            return
        visited.add(id(stmts))

        for i, stmt in enumerate(stmts):
            stmt_type = stmt.properties.get("type", "")
            is_last = i + 1 == len(stmts)
            next_id = (
                stmts[i + 1].id if not is_last else (tail_id if tail_id is not None else exit_id)
            )

            # ``break`` / ``continue`` jump to the enclosing loop targets.
            if stmt_type == "break_statement" and loop_ctx is not None:
                edges.append(self._flow_edge(stmt.id, loop_ctx[0]))
                continue
            if stmt_type == "continue_statement" and loop_ctx is not None:
                edges.append(self._flow_edge(stmt.id, loop_ctx[1]))
                continue

            if stmt_type in _BRANCH_TYPES:
                self._handle_branch(stmt, next_id, edges, all_nodes, exit_id, visited, loop_ctx)
            elif stmt_type in _LOOP_TYPES:
                # A nested loop establishes its own break/continue targets.
                self._handle_loop(stmt, next_id, edges, all_nodes, visited)
            elif stmt_type in _TRY_TYPES:
                self._handle_try(stmt, next_id, edges, all_nodes, exit_id, visited, loop_ctx)
            elif stmt_type in _SWITCH_TYPES:
                self._handle_switch(stmt, next_id, edges, all_nodes, exit_id, visited, loop_ctx)
            elif stmt_type in _SYNCHRONIZED_TYPES:
                self._handle_synchronized(
                    stmt, next_id, edges, all_nodes, exit_id, visited, loop_ctx
                )
            else:
                # Sequential flow: stmt → next stmt (or tail target when last).
                if not is_last:
                    edges.append(self._flow_edge(stmt.id, stmts[i + 1].id))
                elif tail_id is not None:
                    edges.append(self._flow_edge(stmt.id, tail_id))

    # ── Branch handling ──────────────────────────────────────────────────

    def _handle_branch(
        self,
        if_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
        loop_ctx: tuple[str, str] | None = None,
    ) -> None:
        """Generate edges for an ``if`` / ``else`` statement."""
        if visited is None:
            visited = set()
        node_id = id(if_node)
        if node_id in visited:
            return
        visited.add(node_id)

        children = self._children_of(if_node, all_nodes)

        # Java if_statement has block children for then/else branches.
        blocks = [c for c in children if c.properties.get("type") == "block"]
        then_stmts = self._collect_body_statements(blocks[0], all_nodes) if blocks else []
        else_stmts = self._collect_body_statements(blocks[1], all_nodes) if len(blocks) > 1 else []

        # True branch
        if then_stmts:
            edges.append(self._flow_edge(if_node.id, then_stmts[0].id, condition="True"))
            self._connect_statements(then_stmts, edges, all_nodes, exit_id, visited, loop_ctx)
            self._link_branch_tail(then_stmts[-1], after_id, edges, loop_ctx)
        else:
            edges.append(self._flow_edge(if_node.id, after_id, condition="True"))

        # False branch
        if else_stmts:
            edges.append(self._flow_edge(if_node.id, else_stmts[0].id, condition="False"))
            self._connect_statements(else_stmts, edges, all_nodes, exit_id, visited, loop_ctx)
            self._link_branch_tail(else_stmts[-1], after_id, edges, loop_ctx)
        else:
            edges.append(self._flow_edge(if_node.id, after_id, condition="False"))

    @staticmethod
    def _is_jump(stmt: CPGNode) -> bool:
        """Return True when *stmt* unconditionally transfers control elsewhere."""
        return stmt.properties.get("type") in {
            "return_statement",
            "throw_statement",
            "break_statement",
            "continue_statement",
        }

    def _link_branch_tail(
        self,
        last: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        loop_ctx: tuple[str, str] | None,
    ) -> None:
        """Flow a branch's last statement to *after_id*.

        When the last statement is a ``break`` / ``continue`` (already routed to
        the loop targets by :meth:`_connect_statements`) or a ``return`` /
        ``throw`` (terminates the path), no fall-through edge is added.
        """
        if loop_ctx is not None and self._is_jump(last):
            return
        edges.append(self._flow_edge(last.id, after_id))

    # ── Loop handling ────────────────────────────────────────────────────

    def _handle_loop(
        self,
        loop_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        visited: set[int] | None = None,
    ) -> None:
        """Generate edges for ``for`` / ``while`` / ``do`` / enhanced-for loops."""
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
            # ``break`` exits to *after_id*; ``continue`` returns to the loop
            # header (``loop_node.id``).  The last sequential body statement
            # also flows back to the header (the loop back-edge).
            self._connect_statements(
                body_stmts,
                edges,
                all_nodes,
                after_id,
                visited,
                loop_ctx=(after_id, loop_node.id),
                tail_id=loop_node.id,
            )

        # Exit edge: loop condition → after
        edges.append(self._flow_edge(loop_node.id, after_id, condition="False"))

    # ── Try/catch/finally handling ───────────────────────────────────────

    def _handle_try(
        self,
        try_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
        loop_ctx: tuple[str, str] | None = None,
    ) -> None:
        """Generate edges for ``try`` / ``catch`` / ``finally``."""
        if visited is None:
            visited = set()
        node_id = id(try_node)
        if node_id in visited:
            return
        visited.add(node_id)

        children = self._children_of(try_node, all_nodes)

        # Separate try body, catch clauses, and finally clause.
        try_blocks = [c for c in children if c.properties.get("type") == "block"]
        catch_clauses = [c for c in children if c.properties.get("type") == "catch_clause"]
        finally_clauses = [c for c in children if c.properties.get("type") == "finally_clause"]

        # Determine the target after try/catch (finally or after_id).
        finally_stmts: list[CPGNode] = []
        if finally_clauses:
            finally_blocks = [
                c
                for c in self._children_of(finally_clauses[0], all_nodes)
                if c.properties.get("type") == "block"
            ]
            if finally_blocks:
                finally_stmts = self._collect_body_statements(finally_blocks[0], all_nodes)

        post_target = finally_stmts[0].id if finally_stmts else after_id

        # Try body
        try_body: list[CPGNode] = []
        if try_blocks:
            try_body = self._collect_body_statements(try_blocks[0], all_nodes)
        if try_body:
            edges.append(self._flow_edge(try_node.id, try_body[0].id))
            self._connect_statements(try_body, edges, all_nodes, exit_id, visited, loop_ctx)
            self._link_branch_tail(try_body[-1], post_target, edges, loop_ctx)

        # Catch clauses — each catch block is reachable from the try node.
        for catch_node in catch_clauses:
            catch_blocks = [
                c
                for c in self._children_of(catch_node, all_nodes)
                if c.properties.get("type") == "block"
            ]
            catch_body: list[CPGNode] = []
            if catch_blocks:
                catch_body = self._collect_body_statements(catch_blocks[0], all_nodes)
            if catch_body:
                edges.append(self._flow_edge(try_node.id, catch_body[0].id, condition="exception"))
                self._connect_statements(catch_body, edges, all_nodes, exit_id, visited, loop_ctx)
                self._link_branch_tail(catch_body[-1], post_target, edges, loop_ctx)

        # Finally clause
        if finally_stmts:
            self._connect_statements(finally_stmts, edges, all_nodes, exit_id, visited, loop_ctx)
            self._link_branch_tail(finally_stmts[-1], after_id, edges, loop_ctx)

    # ── Switch handling ──────────────────────────────────────────────────

    def _handle_switch(
        self,
        switch_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
        loop_ctx: tuple[str, str] | None = None,
    ) -> None:
        """Generate edges for ``switch`` statements."""
        if visited is None:
            visited = set()
        node_id = id(switch_node)
        if node_id in visited:
            return
        visited.add(node_id)

        children = self._children_of(switch_node, all_nodes)

        # Within a switch, ``break`` exits the switch (→ after_id); ``continue``
        # still targets the enclosing loop (if any).
        continue_target = loop_ctx[1] if loop_ctx is not None else after_id
        switch_ctx = (after_id, continue_target)

        # Find switch_block_statement_group nodes.  With direct PARENT_OF
        # children these sit under a ``switch_block`` intermediate node.
        case_groups = [
            c for c in children if c.properties.get("type") == "switch_block_statement_group"
        ]
        if not case_groups:
            switch_blocks = [c for c in children if c.properties.get("type") == "switch_block"]
            for sb in switch_blocks:
                sb_children = self._children_of(sb, all_nodes)
                case_groups.extend(
                    c
                    for c in sb_children
                    if c.properties.get("type") == "switch_block_statement_group"
                )

        for case_group in case_groups:
            case_stmts = self._collect_body_statements(case_group, all_nodes)
            if case_stmts:
                edges.append(self._flow_edge(switch_node.id, case_stmts[0].id, condition="case"))
                # ``break`` within the case is routed to after_id by switch_ctx;
                # a case without a terminal break falls through (no extra edge).
                self._connect_statements(
                    case_stmts, edges, all_nodes, exit_id, visited, switch_ctx
                )

        # Default edge: if no cases match, go to after_id.
        if not case_groups:
            edges.append(self._flow_edge(switch_node.id, after_id, condition="default"))

    # ── Synchronized handling ────────────────────────────────────────────

    def _handle_synchronized(
        self,
        sync_node: CPGNode,
        after_id: str,
        edges: list[CPGEdge],
        all_nodes: list[CPGNode],
        exit_id: str,
        visited: set[int] | None = None,
        loop_ctx: tuple[str, str] | None = None,
    ) -> None:
        """Generate edges for ``synchronized`` blocks (treated as sequential)."""
        if visited is None:
            visited = set()
        node_id = id(sync_node)
        if node_id in visited:
            return
        visited.add(node_id)

        children = self._children_of(sync_node, all_nodes)
        blocks = [c for c in children if c.properties.get("type") == "block"]
        body_stmts = self._collect_body_statements(blocks[0], all_nodes) if blocks else []

        if body_stmts:
            edges.append(self._flow_edge(sync_node.id, body_stmts[0].id))
            self._connect_statements(body_stmts, edges, all_nodes, exit_id, visited, loop_ctx)
            self._link_branch_tail(body_stmts[-1], after_id, edges, loop_ctx)
        else:
            edges.append(self._flow_edge(sync_node.id, after_id))

    # ── AST helpers ──────────────────────────────────────────────────────

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
        visited: set[str] = set()
        queue: deque[str] = deque(self._parent_children.get(parent_node.id, []))
        while queue:
            child_id = queue.popleft()
            if child_id in visited:
                continue
            visited.add(child_id)
            child = self._node_map.get(child_id)
            if child is None:
                continue
            child_type = child.properties.get("type", "")
            if child_type in _ALL_CFG_TYPES:
                stmts.append(child)
                # Don't descend — this is a "direct" statement child.
            else:
                # Not a statement node; continue searching its children.
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

    # Sort by start ascending, then by span size descending (widest first).
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
