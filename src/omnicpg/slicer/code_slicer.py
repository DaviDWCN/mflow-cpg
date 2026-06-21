"""CodeSlicer — extract task-relevant code slices from a CPG for LLM consumption.

Modern AI agents are limited by context windows and can't ingest an entire
codebase at once.  The :class:`CodeSlicer` queries the CPG to extract a
*minimal, connected* sub-graph around a user-specified point of interest, then
renders it as a compact code listing that fits within a token budget.

Supported slicing strategies:

* **Backward slice** — Given a variable use, trace ``REACHES`` and
  ``FLOWS_TO`` edges *backwards* to find all definitions and statements
  that influence the value at that point.
* **Forward slice** — Trace edges *forward* to find everything affected
  by a given definition.
* **Call-context slice** — Follow ``CALLS`` edges to include callee
  definitions (or caller sites) from other files.
* **Neighbourhood slice** — Retrieve all nodes within *N* hops of a
  starting node, regardless of edge direction or type.

All slicers respect an optional ``max_nodes`` budget to keep the result
within the LLM's context window.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from omnicpg.models.edge import EdgeType

if TYPE_CHECKING:
    from omnicpg.interfaces.language_plugin import LanguagePlugin
    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)

# Default maximum number of nodes in a slice.
_DEFAULT_MAX_NODES = 200


class CodeSlicer:
    """Extract sub-graphs and render code snippets from a CPG.

    The slicer operates over in-memory node and edge lists produced by the
    analysis pipeline.  It does *not* require a running graph database.

    Args:
        nodes: All CPG nodes.
        edges: All CPG edges.
    """

    def __init__(
        self,
        nodes: list[CPGNode],
        edges: list[CPGEdge],
    ) -> None:
        """Initialise the slicer with the full CPG."""
        self._nodes = nodes
        self._edges = edges
        # Build lookup tables for fast traversal.
        self._node_map: dict[str, CPGNode] = {n.id: n for n in nodes}
        self._outgoing: dict[str, list[CPGEdge]] = {}
        self._incoming: dict[str, list[CPGEdge]] = {}
        for edge in edges:
            self._outgoing.setdefault(edge.source_id, []).append(edge)
            self._incoming.setdefault(edge.target_id, []).append(edge)

    # ── Public API ────────────────────────────────────────────────────────

    def backward_slice(
        self,
        node_id: str,
        max_nodes: int = _DEFAULT_MAX_NODES,
        edge_types: frozenset[EdgeType] | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Return the backward slice from *node_id*.

        Traces ``REACHES`` and ``FLOWS_TO`` edges backwards (incoming) to
        collect all nodes that influence the given node.

        Args:
            node_id: Starting node ID.
            max_nodes: Maximum number of nodes to include.
            edge_types: Edge types to follow. Defaults to
                ``{REACHES, FLOWS_TO, CALLS}``.

        Returns:
            A tuple of ``(nodes, edges)`` forming the backward slice.
        """
        if edge_types is None:
            edge_types = frozenset(
                {EdgeType.REACHES, EdgeType.FLOWS_TO, EdgeType.CALLS, EdgeType.CONTAINS}
            )
        return self._traverse(
            start_id=node_id,
            direction="backward",
            edge_types=edge_types,
            max_nodes=max_nodes,
        )

    def forward_slice(
        self,
        node_id: str,
        max_nodes: int = _DEFAULT_MAX_NODES,
        edge_types: frozenset[EdgeType] | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Return the forward slice from *node_id*.

        Traces ``REACHES`` and ``FLOWS_TO`` edges forwards (outgoing) to
        collect all nodes affected by the given definition.

        Args:
            node_id: Starting node ID.
            max_nodes: Maximum number of nodes to include.
            edge_types: Edge types to follow.

        Returns:
            A tuple of ``(nodes, edges)`` forming the forward slice.
        """
        if edge_types is None:
            edge_types = frozenset(
                {EdgeType.REACHES, EdgeType.FLOWS_TO, EdgeType.CALLS, EdgeType.CONTAINS}
            )
        return self._traverse(
            start_id=node_id,
            direction="forward",
            edge_types=edge_types,
            max_nodes=max_nodes,
        )

    def neighbourhood(
        self,
        node_id: str,
        max_hops: int = 2,
        max_nodes: int = _DEFAULT_MAX_NODES,
        edge_types: frozenset[EdgeType] | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Return all nodes within *max_hops* of *node_id*.

        Follows edges in both directions (undirected BFS).

        Args:
            node_id: Starting node ID.
            max_hops: Maximum graph distance.
            max_nodes: Maximum number of nodes to include.
            edge_types: Edge types to follow.  ``None`` means all types.

        Returns:
            A tuple of ``(nodes, edges)`` within the neighbourhood.
        """
        visited_ids: set[str] = set()
        collected_edges: list[CPGEdge] = []
        frontier: set[str] = {node_id}

        for _hop in range(max_hops):
            next_frontier: set[str] = set()
            for nid in frontier:
                if nid in visited_ids:
                    continue
                if len(visited_ids) >= max_nodes:
                    break
                visited_ids.add(nid)

                # Outgoing
                for edge in self._outgoing.get(nid, []):
                    if edge_types is not None and edge.edge_type not in edge_types:
                        continue
                    collected_edges.append(edge)
                    if edge.target_id not in visited_ids:
                        next_frontier.add(edge.target_id)

                # Incoming
                for edge in self._incoming.get(nid, []):
                    if edge_types is not None and edge.edge_type not in edge_types:
                        continue
                    collected_edges.append(edge)
                    if edge.source_id not in visited_ids:
                        next_frontier.add(edge.source_id)

            if len(visited_ids) >= max_nodes:
                break
            frontier = next_frontier

        # Add remaining frontier nodes if they haven't been visited and budget allows
        for nid in frontier:
            if len(visited_ids) >= max_nodes:
                break
            visited_ids.add(nid)

        nodes = [self._node_map[nid] for nid in visited_ids if nid in self._node_map]
        # Deduplicate edges
        seen_edges: set[tuple[str, str, str]] = set()
        unique_edges: list[CPGEdge] = []
        for edge in collected_edges:
            key = (edge.source_id, edge.target_id, str(edge.edge_type))
            if key not in seen_edges:
                seen_edges.add(key)
                unique_edges.append(edge)

        logger.info(
            "Neighbourhood slice: %d nodes, %d edges (hops=%d)",
            len(nodes),
            len(unique_edges),
            max_hops,
        )
        return nodes, unique_edges

    def render_slice(
        self,
        nodes: list[CPGNode],
        include_metadata: bool = True,
    ) -> str:
        """Render a slice as a compact, LLM-friendly text listing.

        Groups nodes by file, sorts by line number, and produces a minimal
        code representation suitable for inclusion in an AI prompt.

        Args:
            nodes: Nodes to render (output of a slicing method).
            include_metadata: Whether to include file/line annotations.

        Returns:
            A formatted string ready for LLM consumption.
        """
        # Group by file path.
        by_file: dict[str, list[CPGNode]] = {}
        for node in nodes:
            fp = str(node.properties.get("file_path", "<unknown>"))
            by_file.setdefault(fp, []).append(node)

        parts: list[str] = []
        for file_path in sorted(by_file):
            file_nodes = sorted(
                by_file[file_path],
                key=lambda n: int(n.properties.get("line_start", 0)),
            )
            if include_metadata:
                parts.append(f"# --- {file_path} ---")

            seen_lines: set[int] = set()
            for node in file_nodes:
                code = str(node.properties.get("code", ""))
                line_start = int(node.properties.get("line_start", 0))
                if line_start in seen_lines or not code.strip():
                    continue
                seen_lines.add(line_start)

                if include_metadata:
                    labels_str = ", ".join(node.labels)
                    parts.append(f"L{line_start} [{labels_str}] {code}")
                else:
                    parts.append(code)

        return "\n".join(parts)

    def find_node_by_name(self, name: str) -> list[CPGNode]:
        """Find nodes by their ``name`` property (e.g. function name).

        Args:
            name: The name to search for.

        Returns:
            Matching nodes.
        """
        return [n for n in self._nodes if n.properties.get("name") == name]

    def find_node_by_property(self, key: str, value: Any) -> list[CPGNode]:
        """Find nodes where ``properties[key] == value``.

        Args:
            key: Property name.
            value: Expected value.

        Returns:
            Matching nodes.
        """
        return [n for n in self._nodes if n.properties.get(key) == value]

    # ── JIT expansion helpers (ARCHITECTURAL mode) ───────────────────────

    def get_method_source(self, node_id: str) -> str | None:
        """Return the ``source_code`` property of a Method node.

        In ``ARCHITECTURAL`` analysis mode, method bodies are not expanded
        into individual AST nodes.  Instead, the full source text is stored
        as a ``source_code`` property on the Method node.  This helper
        provides convenient access to that property.

        Args:
            node_id: ID of the Method node.

        Returns:
            The source code string, or ``None`` if the node does not exist
            or does not carry a ``source_code`` property.
        """
        node = self._node_map.get(node_id)
        if node is None:
            return None
        src: str | None = node.properties.get("source_code")
        return src

    def expand_method(
        self,
        node_id: str,
        plugin: LanguagePlugin,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Perform a full (FULL-level) analysis of a single method on-the-fly.

        This enables the "JIT drill-down" workflow: an AI agent first
        navigates the architectural skeleton graph, then selectively
        expands specific methods when deeper analysis is needed.

        The method re-parses the source code stored on the Method node
        using the given *plugin* with ``AnalysisLevel.FULL`` and also
        derives CFG and DFG edges for the resulting sub-graph.

        Args:
            node_id: ID of the Method node to expand.
            plugin: A :class:`LanguagePlugin` instance capable of
                parsing the method's language.

        Returns:
            A tuple of ``(nodes, edges)`` forming the local CPG for
            the method body.  Returns ``([], [])`` if the node does not
            exist or has no ``source_code`` property.
        """
        from omnicpg.models.analysis_level import AnalysisLevel

        source = self.get_method_source(node_id)
        if source is None:
            return [], []

        node = self._node_map[node_id]
        file_path = str(node.properties.get("file_path", "<expanded>"))

        # Re-parse the method body at full granularity.
        ast_nodes, ast_edges = plugin.parse_to_ast(
            file_path,
            source,
            analysis_level=AnalysisLevel.FULL,
        )
        cfg_edges = plugin.build_cfg(ast_nodes, ast_edges)
        dfg_edges = plugin.build_dfg(ast_nodes, cfg_edges, ast_edges)

        all_edges: list[CPGEdge] = []
        all_edges.extend(ast_edges)
        all_edges.extend(cfg_edges)
        all_edges.extend(dfg_edges)

        logger.info(
            "JIT expansion of %s: %d nodes, %d edges",
            node_id[:8],
            len(ast_nodes),
            len(all_edges),
        )
        return ast_nodes, all_edges

    def expand_method_to_neo4j(
        self,
        node_id: str,
        plugin: LanguagePlugin,
        neo4j_adapter: Any,  # Neo4jAdapter 类型，使用 Any 避免循环导入
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """展开方法并增量存储到 Neo4j 数据库。

        这个方法实现了混合方案：
        1. 检查方法是否已在 Neo4j 中展开
        2. 如果已展开，从 Neo4j 查询并返回展开的节点和边
        3. 如果未展开，展开方法并增量存储到 Neo4j

        这样下次查询时可以直接从数据库检索，无需重复展开。

        Args:
            node_id: 要展开的 Method 节点 ID。
            plugin: 能够解析该方法语言的语言插件实例。
            neo4j_adapter: 已连接的 Neo4jAdapter 实例。

        Returns:
            展开后的节点和边。如果方法不存在或展开失败，返回 ([], [])。
        """
        # 步骤 1: 检查方法是否已在 Neo4j 中展开
        if neo4j_adapter.check_method_expanded(node_id):
            logger.info("Method %s already expanded in Neo4j, fetching from database", node_id[:8])
            # 从 Neo4j 查询展开的节点和边
            return self._fetch_expanded_method_from_neo4j(node_id, neo4j_adapter)

        # 步骤 2: 方法未展开，执行展开
        logger.info("Method %s not expanded, performing JIT expansion", node_id[:8])
        expanded_nodes, expanded_edges = self.expand_method(node_id, plugin)

        if not expanded_nodes:
            return [], []

        # 步骤 3: 增量存储到 Neo4j
        try:
            neo4j_adapter.insert_nodes_incremental(expanded_nodes)
            neo4j_adapter.insert_edges_incremental(expanded_edges)
            # 标记方法为已展开
            neo4j_adapter.mark_method_expanded(node_id)
            logger.info(
                "Incrementally stored %d nodes and %d edges to Neo4j for method %s",
                len(expanded_nodes),
                len(expanded_edges),
                node_id[:8],
            )
        except Exception as e:
            logger.error("Failed to store expanded method to Neo4j: %s", e)
            # 即使存储失败，仍然返回展开结果
            pass

        return expanded_nodes, expanded_edges

    def _fetch_expanded_method_from_neo4j(
        self,
        node_id: str,
        neo4j_adapter: Any,  # Neo4jAdapter 类型
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """从 Neo4j 数据库查询已展开的方法节点和边。

        Args:
            node_id: Method 节点 ID。
            neo4j_adapter: 已连接的 Neo4jAdapter 实例。

        Returns:
            从 Neo4j 查询的节点和边。
        """
        from omnicpg.models.edge import CPGEdge, EdgeType
        from omnicpg.models.node import CPGNode

        # 查询方法节点及其所有子节点（使用 CONTAINS 边向下遍历）
        nodes_query = (
            "MATCH (m:Method {id: $method_id})-[:CONTAINS*0..]->(n:Node) RETURN DISTINCT n"
        )
        nodes_records = neo4j_adapter.query(nodes_query, method_id=node_id)

        # 查询相关边
        edges_query = (
            "MATCH (m:Method {id: $method_id})-[:CONTAINS*0..]->(n1:Node) "
            "MATCH (n1)-[r]->(n2:Node) "
            "WHERE (n2:Node)-[:CONTAINS*0..]->(m:Method {id: $method_id}) "
            "RETURN DISTINCT r"
        )
        edges_records = neo4j_adapter.query(edges_query, method_id=node_id)

        # 转换为 CPGNode 和 CPGEdge 对象
        nodes = []
        for record in nodes_records:
            n = record["n"]
            node = CPGNode(
                id=n["id"],
                labels=tuple(n.labels),
                properties=MappingProxyType(dict(n)),
            )
            nodes.append(node)

        edges = []
        for record in edges_records:
            r = record["r"]
            # 获取边类型（从 relationship 类型名）
            edge_type_str = type(r).__name__.upper()
            try:
                edge_type = EdgeType(edge_type_str)
            except ValueError:
                # 如果边类型不在枚举中，使用字符串值
                edge_type = EdgeType.PARENT_OF  # 默认值

            edge = CPGEdge(
                source_id=r.start_node["id"],
                target_id=r.end_node["id"],
                edge_type=edge_type,
                properties=MappingProxyType(dict(r)),
            )
            edges.append(edge)

        logger.info(
            "Fetched %d nodes and %d edges from Neo4j for method %s",
            len(nodes),
            len(edges),
            node_id[:8],
        )
        return nodes, edges

    # ── Private helpers ───────────────────────────────────────────────────

    def _traverse(
        self,
        start_id: str,
        direction: str,
        edge_types: frozenset[EdgeType],
        max_nodes: int,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """BFS traversal in the given *direction*.

        Args:
            start_id: Starting node.
            direction: ``"forward"`` (follow outgoing) or ``"backward"``
                (follow incoming).
            edge_types: Which edge types to traverse.
            max_nodes: Budget cap on collected nodes.

        Returns:
            ``(nodes, edges)`` in the slice.
        """
        visited: set[str] = set()
        collected_edges: list[CPGEdge] = []
        queue: list[str] = [start_id]

        while queue and len(visited) < max_nodes:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if direction == "forward":
                neighbours = self._outgoing.get(current, [])
            else:
                neighbours = self._incoming.get(current, [])

            for edge in neighbours:
                if edge.edge_type not in edge_types:
                    continue
                collected_edges.append(edge)
                next_id = edge.target_id if direction == "forward" else edge.source_id
                if next_id not in visited:
                    queue.append(next_id)

        nodes = [self._node_map[nid] for nid in visited if nid in self._node_map]
        logger.info(
            "%s slice from %s: %d nodes, %d edges",
            direction.capitalize(),
            start_id[:8],
            len(nodes),
            len(collected_edges),
        )
        return nodes, collected_edges
