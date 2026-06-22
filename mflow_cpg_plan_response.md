# M-Flow × OmniCPG 代码与 Spec 差距评估及执行计划

根据对 `openspec/` 目录（尤其是 `sota_improvement_plan.md`, `omnicpg_plan.md` 和 `data_flow_improvements_proposal.md`）与当前代码库的深度对比，我总结了项目目前的进展差距（Gap），并据此制定了执行计划。

## 一、 当前代码与 Spec 的差距评估

### 1. 语法感知与上下文检索 (Syntax-Aware & Contextual Retrieval)
* **Spec 目标**: (`omnicpg_plan.md`) Refactor `_truncate_code`: Introduce AST-aware compression. (`sota_improvement_plan.md`) Contextual Retrieval。
* **现状**: **已完成**。代码库中的 `SyntaxAwareCodeChunker` 已经实现了带有类名的 Contextual Retrieval，并且 `graph_enrichment.py` 中的 `_truncate_code` 已经使用了 `tree-sitter-python` 和 `tree-sitter-java` 进行抽象语法树层面的代码压缩。

### 2. 数据流与污点分析 (Data Flow Tracking)
* **Spec 目标**: (`data_flow_improvements_proposal.md`) 短期解决集合容器粗粒度传染；中期通过 CHA 打通多态回调。
* **现状**: **已完成**。`java_plugin/interprocedural_dfg_builder.py` 和 `graph_enrichment.py` 已实现了相应的集合容器污点传染和接口多态虚拟调用边。

### 3. GraphRAG 社区发现 (Community-based GraphRAG)
* **Spec 目标**: (`sota_improvement_plan.md`) 实现基于社区的图谱聚合 (Community-based GraphRAG)。
* **现状**: **已完成**。代码库中存在 `mflow_cpg/graph_rag.py` 以及相关测试 `tests/mflow_cpg/test_graph_rag.py`，实现了 GDS/Leiden 社区发现。

### 4. 混合检索重排序 (Cross-Encoder Late Interaction Reranking)
* **Spec 目标**: (`sota_improvement_plan.md`) 引入轻量级 Reranker。
* **现状**: **已完成**。配置中存在 `RerankerSettings`，检索时能够进行重新排序。

### 5. 待完成的短中期目标 (Unfinished Short/Mid-term Goals)
* **MCP 混合推理编排 (Agentic Workflow)**: 在 MCP Server 内部实现原生的 Agentic Workflow (如 ReAct 或 Reflection 模式)，让搜索线索能够自主触发代码结构查询。目前 MCP Server 只提供了基础的工具，尚未封装原生的 Agentic Workflow。
* **2-Degree 图谱上下文增强**: `graph_enrichment.py` 的意图生成 Prompt 前置条件 `_build_context_str` 中，尚未包含 2 度调用图上下文。

### 6. 待完成的长期目标 (Unfinished Long-term Goals)
* **GNN 代码表征学习 (Graph-aware Embeddings)**: 目前依旧是将代码转化为文本进行 Embedding，尚未引入 GraphSAGE 或 GCN 等图神经网络进行直接的高维向量编码。
* **自动化 SOTA 评测基准**: 尚未接入 SWE-bench 或缺陷靶场的自动化持续评估。
* **属性敏感的 DFG 建模 (Field-Sensitive DFG)**: 目前只有 `_bind_field_writes_to_reads`，尚未完全为对象的内部成员分配独立的虚拟节点。

## 二、 补齐差距的执行计划 (Execution Plan)

> **注**：鉴于部分基础设施（Reranker、Contextual Retrieval、GraphRAG 等）已经落实，我们未来的攻坚重点应放在 MCP 增强与长线模型演进上。

### 步骤 1：完善 MCP 混合推理编排 (Phase 1)
- **目标**: 在 MCP Server 中增加复合工具（Compound Tools），将简单的 `mflow_search` 和 `query_nodes` 等组装为具有 ReAct / Reflection 能力的智能体流。
- **行动**: 在 `mcp_server_omnicpg/tools/` 目录下新增 `agentic_tools.py`。利用现有的检索结果构建提示词，并在 Server 端通过 LLM API 进行自动反思和步进式推理，最终返回诊断报告。

### 步骤 2：补齐二度调用链路上下文 (Phase 1 - `_build_context_str`)
- **目标**: 落实 `omnicpg_plan.md`，让大模型分析语义意图时看到 2 度的调用图环境。
- **行动**: 修改 `src/omnicpg/orchestrator/graph_enrichment.py`。在 `enrich_semantic_intent` 的 Cypher 中加入 `[(n)-[:CALLS]->(:Method)-[:CALLS]->(c2) | c2.name] AS c2_callees` 和 `[(c2:Method)-[:CALLS]->(:Method)-[:CALLS]->(n) | c2.name] AS c2_callers`。更新 `_build_context_str` 以渲染这些上下文。

### 步骤 3：预研 GNN 代码表征学习与 SOTA 评估 (Phase 3)
- **目标**: 推进项目的核心算法升级，达到学术界前沿标准。
- **行动**:
  1. 将现存测试用例转化为评测靶场格式，搭建自动化 Pipeline。
  2. 调研 Neo4j GraphSAGE 插件或 PyTorch Geometric，用于替代当前的 `nomic-embed-text`，将 AST/DFG 的拓扑结构也编码入向量空间中。
