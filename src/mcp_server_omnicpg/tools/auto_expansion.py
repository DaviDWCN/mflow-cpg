"""Auto-expansion MCP tools with JIT method expansion.

This module provides enhanced MCP tools that automatically expand methods
when needed, implementing the hybrid ARCHITECTURAL + CodeSlicer approach.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp_server_omnicpg.neo4j_adapter import get_adapter
from mcp_server_omnicpg.tools.path_queries import (
    find_control_flow as base_find_control_flow,
)
from mcp_server_omnicpg.tools.path_queries import (
    find_data_flow as base_find_data_flow,
)

if TYPE_CHECKING:
    from omnicpg.cache.expansion_cache import ExpansionCache

logger = logging.getLogger(__name__)

# Global expansion cache instance
_expansion_cache: ExpansionCache | None = None


def get_expansion_cache() -> ExpansionCache:
    """获取或创建全局 ExpansionCache 实例."""
    global _expansion_cache
    if _expansion_cache is None:
        from omnicpg.cache.expansion_cache import ExpansionCache

        adapter = get_adapter()
        adapter.ensure_connected()
        _expansion_cache = ExpansionCache(adapter)
        logger.info("Initialized expansion cache")
    return _expansion_cache


def find_data_flow_with_auto_expand(
    source_node_id: str,
    target_node_id: str,
    max_depth: int = 5,
    auto_expand: bool = True,
) -> list[dict[str, Any]]:
    """查找数据流路径，支持自动展开方法。

    当在 ARCHITECTURAL 模式下查询数据流时，如果发现方法未展开，
    会自动触发展开并存储到 Neo4j。

    Args:
        source_node_id: 源节点 ID。
        target_node_id: 目标节点 ID。
        max_depth: 最大路径深度。
        auto_expand: 是否自动展开未展开的方法。

    Returns:
        数据流路径列表。
    """
    # 首先尝试基础查询
    try:
        paths = base_find_data_flow(source_node_id, target_node_id, max_depth)
        if paths or not auto_expand:
            return paths
    except Exception as e:
        logger.warning("Base data flow query failed: %s", e)

    # 如果没有找到路径且启用了自动展开，检查是否需要展开方法
    if auto_expand:
        return _auto_expand_and_retry_data_flow(source_node_id, target_node_id, max_depth)

    return []


def find_control_flow_with_auto_expand(
    start_node_id: str,
    end_node_id: str,
    max_depth: int = 5,
    auto_expand: bool = True,
) -> list[dict[str, Any]]:
    """查找控制流路径，支持自动展开方法。

    当在 ARCHITECTURAL 模式下查询控制流时，如果发现方法未展开，
    会自动触发展开并存储到 Neo4j。

    Args:
        start_node_id: 起始节点 ID。
        end_node_id: 结束节点 ID。
        max_depth: 最大路径深度。
        auto_expand: 是否自动展开未展开的方法。

    Returns:
        控制流路径列表。
    """
    # 首先尝试基础查询
    try:
        paths = base_find_control_flow(start_node_id, end_node_id, max_depth)
        if paths or not auto_expand:
            return paths
    except Exception as e:
        logger.warning("Base control flow query failed: %s", e)

    # 如果没有找到路径且启用了自动展开，检查是否需要展开方法
    if auto_expand:
        return _auto_expand_and_retry_control_flow(start_node_id, end_node_id, max_depth)

    return []


def _auto_expand_and_retry_data_flow(
    source_node_id: str,
    target_node_id: str,
    max_depth: int,
) -> list[dict[str, Any]]:
    """自动展开相关方法并重试数据流查询."""
    logger.info("Auto-expanding methods for data flow query")

    # 查找需要展开的方法
    methods_to_expand = _find_methods_on_path(source_node_id, target_node_id)

    if not methods_to_expand:
        logger.info("No methods found to expand")
        return []

    # 批量展开方法
    logger.info("Expanding %d methods", len(methods_to_expand))
    expanded_count = 0

    for method_id in methods_to_expand:
        try:
            result = expand_method_on_demand(method_id)
            if result.get("status") == "success":
                expanded_count += 1
                logger.info("Successfully expanded method %s", method_id[:8])
            elif result.get("status") == "already_expanded":
                logger.debug("Method %s already expanded", method_id[:8])
            else:
                logger.warning(
                    "Failed to expand method %s: %s", method_id[:8], result.get("message")
                )
        except Exception as e:
            logger.error("Error expanding method %s: %s", method_id[:8], e)

    logger.info("Expanded %d/%d methods", expanded_count, len(methods_to_expand))

    # 重试查询
    return base_find_data_flow(source_node_id, target_node_id, max_depth)


def _auto_expand_and_retry_control_flow(
    start_node_id: str,
    end_node_id: str,
    max_depth: int,
) -> list[dict[str, Any]]:
    """自动展开相关方法并重试控制流查询."""
    logger.info("Auto-expanding methods for control flow query")

    # 查找需要展开的方法
    methods_to_expand = _find_methods_on_path(start_node_id, end_node_id)

    if not methods_to_expand:
        logger.info("No methods found to expand")
        return []

    # 批量展开方法
    logger.info("Expanding %d methods", len(methods_to_expand))
    expanded_count = 0

    for method_id in methods_to_expand:
        try:
            result = expand_method_on_demand(method_id)
            if result.get("status") == "success":
                expanded_count += 1
                logger.info("Successfully expanded method %s", method_id[:8])
            elif result.get("status") == "already_expanded":
                logger.debug("Method %s already expanded", method_id[:8])
            else:
                logger.warning(
                    "Failed to expand method %s: %s", method_id[:8], result.get("message")
                )
        except Exception as e:
            logger.error("Error expanding method %s: %s", method_id[:8], e)

    logger.info("Expanded %d/%d methods", expanded_count, len(methods_to_expand))

    # 重试查询
    return base_find_control_flow(start_node_id, end_node_id, max_depth)


def _find_methods_on_path(start_node_id: str, end_node_id: str) -> list[str]:
    """查找路径上的方法节点.

    Args:
        start_node_id: 起始节点 ID。
        end_node_id: 结束节点 ID。

    Returns:
        需要展开的方法 ID 列表。
    """
    adapter = get_adapter()
    adapter.ensure_connected()

    # 查找路径上未展开的方法
    query = """
        MATCH (start:Node {id: $start_node_id})
        MATCH (end:Node {id: $end_node_id})
        MATCH path = (start)-[*1..5]-(end)
        UNWIND nodes(path) AS node
        MATCH (node:Node)
        WHERE node.type = 'function_definition'
          AND (node.expanded IS NULL OR node.expanded = false)
          AND node.source_code IS NOT NULL
        RETURN DISTINCT node.id AS method_id
        LIMIT 10
    """

    results = adapter.query(query, start_node_id=start_node_id, end_node_id=end_node_id)
    method_ids = [record["method_id"] for record in results]

    logger.info("Found %d methods to expand on path", len(method_ids))
    return method_ids


def _get_plugin_for_file(file_path: str) -> Any:
    """Return the appropriate language plugin based on file extension.

    XML files use JavaPlugin because the Java plugin handles framework
    configuration (Spring, Hibernate, web.xml, etc.) with deep parsing.
    """
    if file_path.endswith((".java", ".jsp", ".xml")):
        from omnicpg.plugins.java_plugin.plugin import JavaPlugin

        return JavaPlugin()
    # Default to Python
    from omnicpg.plugins.python_plugin.plugin import PythonPlugin

    return PythonPlugin()


def expand_method_on_demand(method_id: str) -> dict[str, Any]:
    """按需展开指定方法.

    这个工具允许显式地展开一个方法，并将其存储到 Neo4j。
    自动检测方法所属语言并选择对应的语言插件。

    Args:
        method_id: 要展开的方法节点 ID。

    Returns:
        展开结果，包含节点数、边数等信息。
    """
    adapter = get_adapter()
    adapter.ensure_connected()

    # 检查是否已展开
    if adapter.check_method_expanded(method_id):
        return {
            "status": "already_expanded",
            "method_id": method_id,
            "message": "Method already expanded in database",
        }

    # 查询方法的源代码
    query = """
        MATCH (m:Method {id: $method_id})
        RETURN m.source_code AS source_code,
               m.file_path AS file_path,
               m.name AS name
    """
    results = adapter.query(query, method_id=method_id)

    if not results:
        return {
            "status": "not_found",
            "method_id": method_id,
            "message": "Method not found",
        }

    method_info = results[0]
    source_code = method_info.get("source_code")

    if not source_code:
        return {
            "status": "no_source_code",
            "method_id": method_id,
            "message": "Method has no source_code property (may not be in ARCHITECTURAL mode)",
        }

    # 执行展开 — 根据文件扩展名自动选择语言插件
    from omnicpg.models.analysis_level import AnalysisLevel

    file_path = method_info.get("file_path", "<expanded>")
    plugin = _get_plugin_for_file(file_path)

    ast_nodes, ast_edges = plugin.parse_to_ast(
        file_path,
        source_code,
        analysis_level=AnalysisLevel.FULL,
    )
    cfg_edges = plugin.build_cfg(ast_nodes, ast_edges)
    dfg_edges = plugin.build_dfg(ast_nodes, cfg_edges, ast_edges)

    all_edges = []
    all_edges.extend(ast_edges)
    all_edges.extend(cfg_edges)
    all_edges.extend(dfg_edges)

    # 存储到 Neo4j
    try:
        adapter.insert_cpg_nodes(ast_nodes)
        adapter.insert_cpg_edges(all_edges)
        adapter.mark_method_expanded(method_id)

        logger.info(
            "Successfully expanded method %s: %d nodes, %d edges",
            method_id[:8],
            len(ast_nodes),
            len(all_edges),
        )

        return {
            "status": "success",
            "method_id": method_id,
            "method_name": method_info.get("name"),
            "nodes_count": len(ast_nodes),
            "edges_count": len(all_edges),
            "message": "Method successfully expanded and stored",
        }
    except Exception as e:
        logger.error("Failed to store expanded method: %s", e)
        return {
            "status": "error",
            "method_id": method_id,
            "message": f"Failed to store expanded method: {e}",
        }


def get_expansion_stats() -> dict[str, Any]:
    """获取展开缓存统计信息.

    Returns:
        包含缓存统计信息的字典。
    """
    cache = get_expansion_cache()
    stats: dict[str, Any] = cache.get_cache_stats()
    return stats
