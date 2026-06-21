"""ExpansionCache — manages method expansion state and caching for ARCHITECTURAL mode.

This module provides a cache layer for the hybrid analysis approach:
- ARCHITECTURAL-level scan of the entire project
- On-demand FULL-level expansion of specific methods
- Incremental storage to Neo4j for fast retrieval

The cache ensures that methods are only expanded once and reused across queries.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from omnicpg.interfaces.language_plugin import LanguagePlugin
    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode
    from omnicpg.slicer.code_slicer import CodeSlicer

logger = logging.getLogger(__name__)


@runtime_checkable
class SupportsMethodExpansion(Protocol):
    """Structural protocol for adapters that support method-expansion tracking."""

    def check_method_expanded(self, method_id: str) -> bool:
        """Return True if the method has already been expanded into the database."""
        ...

    def query(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a Cypher read query and return the result rows."""
        ...


class ExpansionCache:
    """管理方法展开状态和缓存的缓存管理器。

    这个类提供了一个简单的内存缓存，用于跟踪哪些方法已被展开。
    它与 Neo4j 协作，确保展开状态持久化。

    Args:
        neo4j_adapter: 已连接的、支持方法展开追踪的 adapter 实例。
    """

    def __init__(self, neo4j_adapter: SupportsMethodExpansion) -> None:
        """初始化展开缓存管理器."""
        self._neo4j_adapter = neo4j_adapter
        # 内存缓存：method_id -> (nodes, edges)
        self._cache: dict[str, tuple[list[CPGNode], list[CPGEdge]]] = {}
        # 批量展开队列：待展开的方法 ID 列表
        self._pending_expansions: set[str] = set()

    def get_expanded_method(
        self,
        method_id: str,
        slicer: CodeSlicer,
        plugin: LanguagePlugin,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """获取展开的方法，如果未展开则触发展开。

        这个方法实现了缓存的 get-or-compute 逻辑：
        1. 检查内存缓存
        2. 检查 Neo4j 数据库
        3. 如果都不存在，触发展开并存储

        Args:
            method_id: 方法节点 ID。
            slicer: CodeSlicer 实例。
            plugin: 语言插件实例。

        Returns:
            展开的节点和边。
        """
        # 步骤 1: 检查内存缓存
        if method_id in self._cache:
            logger.debug("Method %s found in memory cache", method_id[:8])
            return self._cache[method_id]

        # 步骤 2: 检查 Neo4j 并触发展开（如果需要）
        nodes, edges = slicer.expand_method_to_neo4j(method_id, plugin, self._neo4j_adapter)

        # 步骤 3: 存入内存缓存
        self._cache[method_id] = (nodes, edges)

        return nodes, edges

    def batch_expand_methods(
        self,
        method_ids: list[str],
        slicer: CodeSlicer,
        plugin: LanguagePlugin,
        max_concurrent: int = 5,
    ) -> dict[str, tuple[list[CPGNode], list[CPGEdge]]]:
        """批量展开多个方法。

        这个方法可以预先展开多个方法，适用于预热缓存或批量分析场景。

        Args:
            method_ids: 要展开的方法 ID 列表。
            slicer: CodeSlicer 实例。
            plugin: 语言插件实例。
            max_concurrent: 最大并发展开数（预留参数，当前为串行）。

        Returns:
            字典，键为方法 ID，值为对应的 (nodes, edges) 元组。
        """
        results: dict[str, tuple[list[CPGNode], list[CPGEdge]]] = {}
        expanded_count = 0
        skipped_count = 0

        for method_id in method_ids:
            # 检查是否已展开（内存缓存 + Neo4j）
            if method_id in self._cache or self._neo4j_adapter.check_method_expanded(method_id):
                skipped_count += 1
                continue

            # 执行展开
            nodes, edges = self.get_expanded_method(method_id, slicer, plugin)
            results[method_id] = (nodes, edges)
            expanded_count += 1

            logger.info(
                "Batch expansion: %d/%d methods expanded, %d skipped",
                expanded_count,
                len(method_ids),
                skipped_count,
            )

        logger.info("Batch expansion completed: %d methods expanded", expanded_count)
        return results

    def preload_hot_methods(
        self,
        slicer: CodeSlicer,
        plugin: LanguagePlugin,
        criteria: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> None:
        """预加载热点方法到缓存。

        这个方法根据指定的标准从 Neo4j 查找方法并预加载到缓存中。

        Args:
            slicer: CodeSlicer 实例。
            plugin: 语言插件实例。
            criteria: 查询条件，例如 {"name_pattern": "process_*"}。
                如果为 None，使用默认标准（最近调用的方法）。
            limit: 预加载的方法数量上限。
        """
        # 查询要预加载的方法
        if criteria is None:
            # 默认：查找最近的方法（可以根据实际需求调整）
            query = (
                "MATCH (m:Method) "
                "WHERE m.expanded IS NULL OR m.expanded = false "
                "RETURN m.id AS id "
                "LIMIT $limit"
            )
            method_records = self._neo4j_adapter.query(query, limit=limit)
            method_ids = [record["id"] for record in method_records]
        else:
            # 自定义查询条件
            if "name_pattern" in criteria:
                pattern = criteria["name_pattern"]
                query = (
                    "MATCH (m:Method) "
                    "WHERE m.name STARTS WITH $pattern "
                    "AND (m.expanded IS NULL OR m.expanded = false) "
                    "RETURN m.id AS id "
                    "LIMIT $limit"
                )
                method_records = self._neo4j_adapter.query(query, pattern=pattern, limit=limit)
                method_ids = [record["id"] for record in method_records]
            else:
                logger.warning("Unknown preload criteria: %s", criteria)
                return

        # 批量展开
        if method_ids:
            logger.info("Preloading %d hot methods...", len(method_ids))
            self.batch_expand_methods(method_ids, slicer, plugin)
            logger.info("Preload completed")

    def clear_cache(self) -> None:
        """清空内存缓存。

        注意：这不会清空 Neo4j 中的展开状态，只清空内存缓存。
        """
        self._cache.clear()
        logger.info("Memory cache cleared")

    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计信息。

        Returns:
            包含缓存统计信息的字典。
        """
        # 统计 Neo4j 中已展开的方法数量
        query = "MATCH (m:Method) WHERE m.expanded = true RETURN count(m) AS expanded_count"
        result = self._neo4j_adapter.query(query)
        expanded_in_db = result[0]["expanded_count"] if result else 0

        return {
            "memory_cache_size": len(self._cache),
            "expanded_in_db": expanded_in_db,
            "pending_expansions": len(self._pending_expansions),
        }

    def invalidate_method(self, method_id: str) -> None:
        """使指定方法的缓存失效。

        当方法源代码发生变化时，可以调用此方法清除缓存。

        Args:
            method_id: 要失效的方法节点 ID。
        """
        if method_id in self._cache:
            del self._cache[method_id]
            logger.info("Invalidated cache for method %s", method_id[:8])

        # 也可以考虑从 Neo4j 中移除展开标记
        # （根据实际需求决定是否需要）
