# M-Flow × OmniCPG 扩展：CPG 与 DB Schema 融合可行性分析与实施方案

将代码属性图（Code Property Graph, CPG）与数据库表结构（Database Schema）及元数据在 Neo4j 中有效结合，是静态程序分析、变更影响分析、数据安全治理（如污点分析）以及微服务重构领域的**前沿实践**。

要将两者无缝融合，核心在于**设计合理的图谱 Schema（统一元模型）**，并建立**“桥接（Bridge）关系”**。本文档评估了在当前 M-Flow × OmniCPG 架构下引入该融合方案的可行性，并系统化地提出了图谱构建方案、桥接方法以及实用的 Cypher 分析场景。

---

## 一、 统一图谱 Schema 设计

在当前的 Neo4j 数据库中，我们需要将 **OmniCPG 生成的代码层** 与 **外部导入的数据库物理/逻辑层** 通过**桥接关系**连接，形成一个“双层异构图”。

```text
   【 OmniCPG 代码属性图层 】
    (Endpoint) ──[:ROUTES_TO]──> (Method) ──[:CALLS]──> (DAO/Repository Method)
                                    │                           │
                                [:AST/DFG]                 [:MAPS_TO] (Entity-Table)
                                    ▼                           ▼
                             (Literal:SQL) ───────────────> (Table)
                                    │                           │
                             [:REF_COLUMN]                 [:HAS_COLUMN]
                                    ▼                           ▼
                             (Identifier) ────────────────> (Column)

   【 数据库 Schema 层 】
```

### 1. CPG 子图（代码层节点，由 OmniCPG 维护）
保留 OmniCPG 目前生成的标准节点（由 tree-sitter 解析）：
*   `Method`：方法/函数。
*   `Class`：类或数据类型。
*   `Call`：方法调用。
*   `Literal`：字面量（常用于存放 SQL 模板或常量）。
*   `Annotation` / `Decorator`：注解（如 Java 里的 `@Table`, `@Column` 或 Python 里的 `@table`）。

### 2. DB Schema 子图（数据库层节点，新增元数据体系）
需要自定义导入的数据库元数据节点：
*   `Database`：数据库实例。
*   `Schema`：命名空间。
*   `Table`：物理表/视图（属性：`name`, `comment`, `is_view`）。
*   `Column`：字段（属性：`name`, `type`, `is_nullable`, `comment`, `is_primary_key`）。

### 3. 桥接关系（The Bridges）—— 核心纽带
为了实现“双层联通”，我们拟新增以下核心边（Relationship）：
*   `[:MAPS_TO]`：将代码中的 ORM 实体类（Class）映射到数据库物理表（Table）。
*   `[:ACCESSES_TABLE]`：方法（Method）或 SQL 语句直接访问了某张表。
*   `[:ACCESSES_COLUMN]`：代码中的变量或 SQL 字段引用了某个物理列。

---

## 二、 桥接构建的实践方法

在 OmniCPG 的解析 Pipeline 中，可通过以下三种桥接手段自动连线：

### 1. 声明式桥接：解析 ORM 实体注解（基于 AST）
针对使用 Hibernate/JPA, SQLAlchemy 等 ORM 框架的项目，代码中存在显式映射：
*   **提取方法**：在 OmniCPG 解析时，定位带有 `@Table(name="xxx")`（或类似语义）注解的 `Class` 节点。
*   **连线逻辑**：
    ```cypher
    // 伪代码：将带有表注解的类连接到对应的物理表
    MATCH (c:Class)-[:HAS_ANNOTATION]->(a:Annotation {name: "Table"})
    MATCH (t:Table {name: a.arguments.name}) // 需适配 OmniCPG AST 结构
    MERGE (c)-[:MAPS_TO]->(t)
    ```
*   同理，将类中的属性（Field）通过相应的 `@Column` 注解连接到具体的 `Column` 节点。

### 2. 编程式桥接：对 SQL 字面量进行二次 AST 解析
代码中存在原生 SQL 语句或动态查询构建器时：
*   **提取方法**：在 OmniCPG 图中，通过静态分析过滤出传入数据库执行方法（如 `session.execute()`）的 `Literal` 节点（SQL 字符串）。
*   **连线逻辑**：
    1.  借助 Python 的 `sqlglot` 等解析库对 SQL 字符串进行**二次 AST 解析**，提取引用的 `Table` 和 `Column`。
    2.  在 Neo4j 中，将这些 `Literal` 节点或所在的方法节点与 `Table`、`Column` 建立 `[:ACCESSES_TABLE]` 和 `[:ACCESSES_COLUMN]`。

### 3. 数据流桥接：利用 DFG（数据流图）传播关系
由于 OmniCPG 已具备初步的 DFG 能力，我们可以隐式传播关系：
*   如果 OmniCPG 推导出：`Controller -> Service -> DAO -> SQL-Literal` 的数据流。
*   一旦 `SQL-Literal` 连至 `Table`，可通过 Cypher 遍历路径，推断并持久化高层业务接口（Controller）对底层数据表（Table）的依赖关系。

---

## 三、 经典 SOTA 分析场景及 Cypher 示例

融合后，系统可支持传统 CPG 或单纯的数据库 DDL 无法完成的**跨域（Cross-Domain）联合分析**。这与 M-Flow 的业务实体追踪（Business Entity）高度契合。

### 场景 1：Schema 变更影响分析 (Impact Analysis)
*   **痛点**：计划修改或删除 `users.phone_number`，想预知**这会破坏代码里的哪些业务接口**。
*   **Cypher 查询**：
    ```cypher
    // 从物理列出发，反向寻找能流向该列的 API 路由节点
    MATCH (col:Column {name: "phone_number"})<-[:HAS_COLUMN]-(tab:Table {name: "users"})
    MATCH path = (endpoint:Method {is_api_endpoint: true})-[:CALLS|DFG*1..10]->(codeNode)-[:ACCESSES_COLUMN|MAPS_TO]->(col)
    RETURN endpoint.name AS ApiEndpoint, codeNode.name AS AccessPoint, path
    ```
    *价值：在编译期前实现精准的变更风险评估。*

### 场景 2：端到端数据血缘与隐私合规性检测 (Data Lineage & Taint Analysis)
*   **痛点**：检测系统中敏感数据（如 `password_hash` 列）是否泄露到不安全出口（如未经脱敏的日志、HTTP 响应）。
*   **Cypher 查询**：
    ```cypher
    // 污点分析：敏感列 (Source) -> 代码处理 -> 外部序列化/打印方法 (Sink)
    MATCH (sensitive:Column {name: "password_hash"})<-[:MAPS_TO|ACCESSES_COLUMN]-(field)
    MATCH (sink:Method) WHERE sink.name IN ["log", "print", "writeResponse"]
    MATCH path = shortestPath((field)-[:DFG*1..15]->(sink))
    RETURN path
    ```

### 场景 3：检测循环中的数据库操作 (N+1 Query Detection)
*   **痛点**：怀疑存在 `For/While` 循环内反复执行 DB 查询导致的性能瓶颈。
*   **Cypher 查询**：
    ```cypher
    // 寻找处于循环控制结构 AST 节点下的数据库调用
    MATCH (loop) WHERE loop.type IN ["for_statement", "while_statement"] // OmniCPG AST 类型
    MATCH (loop)-[:AST_CHILD*1..10]->(call:Call)
    MATCH (call)-[:CALLS*0..3]->(dao:Method)-[:ACCESSES_TABLE]->(t:Table)
    RETURN loop.code AS LoopCode, dao.name AS BadQueryMethod, t.name AS AffectedTable
    ```

### 场景 4：基于数据高内聚的微服务拆分评估 (Domain Clustering)
*   **痛点**：将大型单体拆分为微服务，不仅需拆代码，还要拆分数据库。
*   **应用**：
    利用现有的 `mflow_cpg/graph_rag.py` 中集成的 **Neo4j GDS（Louvain 算法）**。
    设定权重：代码方法间 `CALLS` 为 1.0，代码类与数据库表间 `MAPS_TO/ACCESSES_TABLE` 为 2.0。聚类结果将给出“代码+表”的最佳共同边界。

---

## 四、 进阶：结合大模型 (GraphRAG) 的终极形态

M-Flow 与 OmniCPG 的核心愿景是建立强大的 Code Agent。融合 DB Schema 后，我们将解锁真正的 **业务-数据-代码 全链路 GraphRAG**：

*   **工作原理**：当开发提问：“*当用户注销账号时，系统是如何在数据库中清理数据的？*”
*   **执行逻辑**：
    1.  M-Flow 的 Agent 意图生成模块将提问转化为图搜索。
    2.  Neo4j 检索出混合路径：`LogoutController` -> `UserService.deleteUser()` -> `userRepository.delete()` -> 物理表 `users` 和 `user_sessions`。
    3.  结合 `SyntaxAwareCodeChunker` 获取代码片段和 Schema 结构，大模型（LLM）生成 100% 精确的业务与数据流向解释。

## 结论
该可行性分析证明，基于现有的 OmniCPG 引擎和 M-Flow GraphRAG 基础设施，在 Neo4j 中引入数据库 Schema 并通过 AST/DFG/SQL 解析构建桥接关系，在技术路线上完全走得通。这将是项目后续演进的重要里程碑。
