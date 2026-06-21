"""测试混合方案：ARCHITECTURAL + CodeSlicer + 增量存储.

这个测试模块验证完整的混合分析流程：
1. 使用 ARCHITECTURAL 级别扫描项目
2. 使用 CodeSlicer 按需展开方法
3. 增量存储到 Neo4j
4. 从 Neo4j 查询已展开的方法
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omnicpg.adapters.neo4j_adapter import Neo4jAdapter
from omnicpg.cache.expansion_cache import ExpansionCache
from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator
from omnicpg.plugins.python_plugin.plugin import PythonPlugin
from omnicpg.slicer.code_slicer import CodeSlicer

# ── 测试代码样本 ─────────────────────────────────────────────────────────────


SAMPLE_CODE = """\
class Calculator:
    def add(self, a: int, b: int) -> int:
        result = a + b
        return result

    def subtract(self, a: int, b: int) -> int:
        if a > b:
            return a - b
        return 0

def process_data(data: list) -> int:
    total = 0
    for item in data:
        total += item
    return total
"""

# ── 测试类 ─────────────────────────────────────────────────────────────────────


class TestHybridAnalysis:
    """测试混合分析方案的完整流程."""

    @pytest.fixture()
    def temp_project(self) -> tempfile.TemporaryDirectory:
        """创建临时项目目录."""
        tmpdir = tempfile.TemporaryDirectory()
        project_path = Path(tmpdir.name)
        (project_path / "calculator.py").write_text(SAMPLE_CODE)
        yield tmpdir
        tmpdir.cleanup()

    @pytest.fixture()
    def neo4j_adapter(self) -> Neo4jAdapter:
        """创建 Neo4j 适配器（需要 Neo4j 运行）."""
        adapter = Neo4jAdapter()
        # 注意：实际测试需要连接到真实的 Neo4j 实例
        # adapter.connect("bolt://localhost:7687", ("neo4j", "password"))
        # adapter.clear()  # 清空测试数据库
        return adapter

    def test_architectural_scan(self, temp_project: tempfile.TemporaryDirectory) -> None:
        """测试 ARCHITECTURAL 级别扫描."""
        orch = ProjectOrchestrator(
            plugins=[PythonPlugin()],
            analysis_level=AnalysisLevel.ARCHITECTURAL,
        )
        nodes, edges = orch.analyze(temp_project.name)

        # 验证节点类型
        node_types = {n.labels for n in nodes}
        assert "Method" in {labels[0] for labels in node_types if labels}

        # 验证方法节点有 source_code 属性
        method_nodes = [n for n in nodes if n.has_label("Method")]
        assert len(method_nodes) > 0

        for method in method_nodes:
            assert "source_code" in method.properties
            assert len(method.properties["source_code"]) > 0

        # 验证没有 Variable 节点
        variable_nodes = [n for n in nodes if n.has_label("Variable")]
        assert len(variable_nodes) == 0

        print(f"✓ ARCHITECTURAL scan: {len(nodes)} nodes, {len(edges)} edges")

    def test_codeslicer_expand_method(self, temp_project: tempfile.TemporaryDirectory) -> None:
        """测试 CodeSlicer 展开方法."""
        # 步骤 1: 创建 ARCHITECTURAL 级别的 CPG
        orch = ProjectOrchestrator(
            plugins=[PythonPlugin()],
            analysis_level=AnalysisLevel.ARCHITECTURAL,
        )
        nodes, edges = orch.analyze(temp_project.name)
        slicer = CodeSlicer(nodes, edges)

        # 步骤 2: 找到一个方法节点
        method_nodes = [n for n in nodes if n.has_label("Method")]
        assert len(method_nodes) > 0
        method_id = method_nodes[0].id

        # 步骤 3: 展开方法
        plugin = PythonPlugin()
        expanded_nodes, expanded_edges = slicer.expand_method(method_id, plugin)

        # 验证展开结果
        assert len(expanded_nodes) > 0
        assert len(expanded_edges) > 0

        # 验证展开的节点包含详细的 AST 节点
        node_types = {n.labels for n in nodes}
        expanded_types = {n.labels for n in expanded_nodes}
        # 应该包含表达式节点、变量节点等
        assert len(expanded_types) > len(node_types)

        print(f"✓ Method expansion: {len(expanded_nodes)} nodes, {len(expanded_edges)} edges")

    def test_neo4j_incremental_insert(self, neo4j_adapter: Neo4jAdapter) -> None:
        """测试 Neo4j 增量插入功能."""
        # 注意：这个测试需要 Neo4j 运行，所以在实际环境中可能需要 skip
        pytest.skip("Requires Neo4j connection - run with --neo4j flag")

        # 模拟节点和边
        from omnicpg.models.edge import CPGEdge, EdgeType
        from omnicpg.models.node import CPGNode

        nodes = [
            CPGNode(
                id="test-node-1",
                labels=("Node", "Method"),
                properties={
                    "id": "test-node-1",
                    "name": "test_method",
                    "source_code": "def test_method():\n    return 1",
                },
            ),
            CPGNode(
                id="test-node-2",
                labels=("Node", "Variable"),
                properties={
                    "id": "test-node-2",
                    "name": "x",
                    "code": "x = 1",
                },
            ),
        ]

        edges = [
            CPGEdge(
                source_id="test-node-1",
                target_id="test-node-2",
                edge_type=EdgeType.CONTAINS,
                properties={},
            ),
        ]

        # 测试增量插入
        neo4j_adapter.insert_nodes_incremental(nodes)
        neo4j_adapter.insert_edges_incremental(edges)

        # 验证插入成功
        node_query = "MATCH (n:Node {id: 'test-node-1'}) RETURN count(n) AS count"
        result = neo4j_adapter.query(node_query)
        assert result[0]["count"] == 1

        print("✓ Incremental insert to Neo4j successful")

    def test_expansion_cache(self, neo4j_adapter: Neo4jAdapter) -> None:
        """测试展开缓存管理器."""
        # 注意：这个测试需要 Neo4j 运行
        pytest.skip("Requires Neo4j connection - run with --neo4j flag")

        cache = ExpansionCache(neo4j_adapter)

        # 测试缓存统计
        stats = cache.get_cache_stats()
        assert "memory_cache_size" in stats
        assert "expanded_in_db" in stats
        assert "pending_expansions" in stats

        print(f"✓ Cache stats: {stats}")

    def test_full_hybrid_workflow(self, temp_project: tempfile.TemporaryDirectory) -> None:
        """测试完整的混合工作流程."""
        # 步骤 1: ARCHITECTURAL 级别扫描
        orch = ProjectOrchestrator(
            plugins=[PythonPlugin()],
            analysis_level=AnalysisLevel.ARCHITECTURAL,
        )
        nodes, edges = orch.analyze(temp_project.name)
        slicer = CodeSlicer(nodes, edges)
        plugin = PythonPlugin()

        # 步骤 2: 按需展开方法
        method_nodes = [n for n in nodes if n.has_label("Method")]
        assert len(method_nodes) > 0

        expanded_methods = []
        for method in method_nodes:
            expanded_nodes, expanded_edges = slicer.expand_method(method.id, plugin)
            expanded_methods.append(
                {
                    "method_id": method.id,
                    "nodes_count": len(expanded_nodes),
                    "edges_count": len(expanded_edges),
                }
            )

        print("\n✓ Full hybrid workflow test completed:")
        print(f"  - ARCHITECTURAL scan: {len(nodes)} nodes, {len(edges)} edges")
        print(f"  - Expanded {len(expanded_methods)} methods")
        for exp in expanded_methods:
            print(
                f"    - {exp['method_id'][:8]}: "
                f"{exp['nodes_count']} nodes, {exp['edges_count']} edges"
            )

        # 验证至少有一个方法被成功展开
        assert len(expanded_methods) > 0
        assert any(exp["nodes_count"] > 0 for exp in expanded_methods)

    def test_auto_expansion_tool(self) -> None:
        """测试自动展开 MCP 工具."""
        from mcp_server_omnicpg.tools.auto_expansion import expand_method_on_demand

        # 注意：这个测试需要 Neo4j 运行，以及数据库中有方法节点
        pytest.skip("Requires Neo4j connection with Method nodes")

        # 测试按需展开工具
        result = expand_method_on_demand("test-method-id")

        # 验证返回结果格式
        assert "status" in result
        assert "method_id" in result

        print(f"✓ Auto expansion tool result: {result}")


# ── 运行测试的主函数 ─────────────────────────────────────────────────────────────


def run_hybrid_analysis_tests(neo4j_connected: bool = False) -> None:
    """运行混合分析测试.

    Args:
        neo4j_connected: 是否连接到 Neo4j。
    """
    print("\n" + "=" * 60)
    print("OmniCPG 混合分析方案测试")
    print("=" * 60)

    test_suite = TestHybridAnalysis()

    # 测试 1: ARCHITECTURAL 级别扫描
    print("\n测试 1: ARCHITECTURAL 级别扫描")
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "calculator.py").write_text(SAMPLE_CODE)
        test_suite.test_architectural_scan(tmpdir)

    # 测试 2: CodeSlicer 展开方法
    print("\n测试 2: CodeSlicer 展开方法")
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "calculator.py").write_text(SAMPLE_CODE)
        test_suite.test_codeslicer_expand_method(tmpdir)

    # 测试 3: 完整混合工作流程
    print("\n测试 3: 完整混合工作流程")
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "calculator.py").write_text(SAMPLE_CODE)
        test_suite.test_full_hybrid_workflow(tmpdir)

    # Neo4j 相关测试（仅在连接时运行）
    if neo4j_connected:
        print("\n测试 4: Neo4j 增量插入")
        neo4j_adapter = Neo4jAdapter()
        neo4j_adapter.connect("bolt://localhost:7687", ("neo4j", "password"))
        try:
            test_suite.test_neo4j_incremental_insert(neo4j_adapter)

            print("\n测试 5: 展开缓存管理器")
            test_suite.test_expansion_cache(neo4j_adapter)

            print("\n测试 6: 自动展开 MCP 工具")
            test_suite.test_auto_expansion_tool()
        finally:
            neo4j_adapter.disconnect()
    else:
        print("\n跳过 Neo4j 相关测试（未连接）")

    print("\n" + "=" * 60)
    print("所有测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="运行混合分析测试")
    parser.add_argument(
        "--neo4j",
        action="store_true",
        help="连接到 Neo4j 并运行完整测试",
    )
    args = parser.parse_args()

    run_hybrid_analysis_tests(neo4j_connected=args.neo4j)
