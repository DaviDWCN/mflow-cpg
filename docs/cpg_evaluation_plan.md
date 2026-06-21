# OmniCPG 评估与分析计划 (基于 CPG SOTA 标准)

根据您提供的 CPG 评估标准，结合 OmniCPG 项目的实际架构与现有资产，我们可以从以下几个维度对本项目进行深度评估，并制定如下落地计划：

## 一、 可用于评估 OmniCPG 的项目资产匹配

### 1. 微基准测试 (Micro-benchmarks) 与 图构建质量评估
**理论对照**：通过极简代码验证 CPG 边连接（跨文件调用、数据流断裂、变量捕获）。
**OmniCPG 资产**：
- **BDD 测试 (`features/`)**：项目包含基于 Behave 的 BDD 测试（如 `omnicpg_python_ast.feature`），非常适合作为 AST/CFG/DFG 构建的微基准测试。可以清晰验证每种语法结构是否生成了正确的 `PARENT_OF`, `FLOWS_TO`, `REACHES` 边。
- **测试代码库**：`test_java_project/` 和 `test_python_project/` 包含了专门用于验证跨文件调用、控制流和数据流的测试用例。
- **评估执行**：运行 `make bdd` 和查看 Allure 报告，即可直观评估微型基准的构建质量。

### 2. 下游任务准确性 (Downstream Task Accuracy) 与 数据流/污点分析
**理论对照**：数据流 (Data Flow) 从 Source 到 Sink 的路径追踪，解决漏洞漏报/误报。
**OmniCPG 资产**：
- **Java Taint 测试 (`tests/java_taint_test/` & `tests/integration/verify_mcp_taint.py`)**：这是现成的端到端漏洞追踪基准。可以用来评估 OmniCPG `find_data_flow` (基于 `REACHES` 边) 查找污点路径的能力（Precision & Recall）。
- **MCP 工具能力 (`mcp_server_omnicpg/`)**：可以直接调用 `find_data_flow`、`find_control_flow` 评估查询准确度。

### 3. 指针/类型推导与调用图分析
**理论对照**：多态调用、类型恢复、跨函数/文件能力。
**OmniCPG 资产**：
- **Java Typed Resolution 测试**：OmniCPG 实现了多轮的 Java 类型解析。仓库中有 `scripts/measure_typed_rate.py` 专门用来评估真实项目（如 legacy Struts1）中 `CALLS` 边的类型解析率（目前 README 宣称 ≈ 99%）。
- **评估执行**：运行 `measure_typed_rate.py`，验证动态调用 (Dynamic Dispatch) 和 `CallSite` 的解析效果。

### 4. 性能与可扩展性 (Performance & Scalability)
**理论对照**：构建时间、内存占用、查询延迟。
**OmniCPG 资产**：
- **离线分析模式 (`run.py --mode analyze`)**：可以绕过数据库写入，纯测试 CPG 提取引擎（Frontend）在内存中的解析速度和吞吐量。
- **并发控制**：评估 `CHUNK_SIZE` 和 `MAX_WORKERS` 机制在大规模代码（kLoC）上的内存峰值表现。

### 5. 查询表达力 (Query Expressiveness)
**理论对照**：对比完成分析任务的查询复杂程度。
**OmniCPG 资产**：
- **MCP 工具抽象**：OmniCPG 没有强迫用户写复杂的 Cypher，而是封装了 42 个标准化工具（如 `get_call_graph`, `find_path`）。评估其封装的 DSL（MCP Context）是否能高效替代复杂的原生 Cypher 查询。

---

## 二、 OmniCPG 专项评估执行计划 (Action Plan)

如果您希望进一步验证该系统，建议按以下步骤进行操作：

### 阶段 1：基础图语义正确性评估（本地微基准验证）
1. 运行单元测试集 `uv run --extra dev pytest tests/unit/`，查看核心 CPG 节点和边的解析成功率。
2. 运行 `make bdd` 验证特定语法树（如 Lambda, 异常处理）的图结构覆盖情况，对照 Allure 报告分析“解析失败率”。

### 阶段 2：数据流连通性与“断头路”排查（核心弱点评估）
1. **构建挑战用例**：在 `test_java_project/` 中添加“对象包装 (Object Wrapping)”、“List 存取”、“多态接口回调”等会导致数据流断裂的盲区用例。
2. **执行查询**：运行 `uv run run.py --mode analyze` 提取 CPG。
3. **断言验证**：调用 `tests/integration/verify_mcp_taint.py` 或 MCP `find_data_flow` 工具，检查 Sink 是否被成功触达。

### 阶段 3：真实项目压力与类型解析率评估（Scalability & Type Recovery）
1. 选取一个中型开源 Java 项目。
2. 执行 `scripts/measure_typed_rate.py`，记录：
   - 跨文件解析耗时 (Build Time)。
   - `CALLS` 边中被明确类型的比例（验证 Type Recovery 深度）。
3. 观察内存峰值 (Memory Footprint)。

### 阶段 4：对接标准漏洞靶场 (可选进阶扩展)
1. 下载 Juliet Test Suite (Java) 的子集（如 SQLi 或 XSS 部分）。
2. 编写自动化脚本，将 OmniCPG 解析结果与 SARD 的预期 Source/Sink 列表对比，计算精确率 (Precision) 和召回率 (Recall)。

## 总结建议
本项目在**微基准测试覆盖 (BDD)** 和 **类型解析率监控 (`measure_typed_rate`)** 上已有很好的基础设施。接下来的评估重点应放在：**复杂数据流的穿越能力 (如 `REACHES` 边在跨层/集合操作时的稳健性)**。