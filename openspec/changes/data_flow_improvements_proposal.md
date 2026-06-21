# OmniCPG 数据流 (DFG) 断裂分析与改进方案

根据此前的评估测试，OmniCPG 在处理基础的参数传递和变量赋值时，数据流 (`REACHES` 边) 追踪非常准确。但在面对涉及**堆内存对象**和**动态类型**的场景时，数据流发生了断裂。以下是详细的失败案例分析以及架构层面的改进方案。

---

## 失败场景 1: 对象包装 (Object Wrapping)

### 现象与举例
当一个污染源（Source）被传入一个对象的构造函数，赋值给成员变量，随后又通过 Getter 方法被取出来时，数据流无法连贯。

**代码示例:**
```java
public void testObjectWrapping(String sourceInput) {
    // 数据流在这里成功: sourceInput -> DataWrapper 构造函数的参数
    DataWrapper wrapper = new DataWrapper(sourceInput);

    // 数据流在这里断裂: 引擎无法知道 getValue() 返回的内容就是当初的 sourceInput
    String extracted = wrapper.getValue();

    executeSink(extracted); // 漏报
}
```

**原因分析:**
当前的 DFG 生成器（如 `java_dfg.py`）主要是基于方法内部的控制流图（CFG）进行的**可达定义分析（Reaching Definition）**。它把 `wrapper` 看作一个整体变量，而没有对 `wrapper` 内部的属性（Field）进行状态建模（即缺少 Field-sensitive 分析）。

### 改进方案：引入属性敏感（Field-Sensitive）的污点传播模型
1. **轻量级启发式规则（Heuristics）**：
   对于简单的 Getter/Setter，可以在 AST 提取阶段将其直接内联（Inline）或者生成特殊的 `ALIAS_OF` 边。例如，识别到 `return this.value;`，建立从 `this.value` 到 `extracted` 的 `REACHES` 边。
2. **构建对象字段图 (Object Field Graph)**：
   在 DFG 中，不再仅将 `wrapper` 视为一个节点，而是将其视为一个命名空间。生成类似 `wrapper.value` 的虚拟节点。构造函数中的赋值操作 `this.value = val` 产生边：`val -> REACHES -> wrapper.value`。调用 `getValue()` 时产生边：`wrapper.value -> REACHES -> extracted`。

---

## 失败场景 2: 集合存取 (List / Collection Storage)

### 现象与举例
当污染源存入集合类（如 List, Map），稍后又从集合中读取时，追踪断裂。

**代码示例:**
```java
public void testListStorage(String sourceInput) {
    List<String> list = new ArrayList<>();

    // 数据流成功: sourceInput 作为参数传给了 list.add
    list.add(sourceInput);

    // 数据流断裂: 引擎无法判定 get(1) 取出来的是不是 sourceInput
    String extracted = list.get(1);

    executeSink(extracted); // 漏报
}
```

**原因分析:**
与对象包装类似，这属于典型的堆内存建模难题。由于 `List.add` 和 `List.get` 是外部方法，静态分析无法在不执行代码的情况下准确推导出 `get(1)` 到底返回什么（这在学术上属于未解难题的近似推导范围）。

### 改进方案：容器建模与污点传染（Taint Infection）
1. **粗粒度污点传染（Taint-style Abstraction）**：
   在漏洞分析工具中，通常不对 `List` 的索引进行精细建模（因为太复杂），而是采用“一脏全脏”的策略（Collection-sensitive 但 Index-insensitive）。
   - **实现策略**：维护一个“知名集合方法”列表。当遇到 `collection.add(X)` 且 X 是节点时，生成边 `X -> REACHES -> collection`。当遇到 `Y = collection.get()` 时，生成边 `collection -> REACHES -> Y`。这会导致一定程度的误报（把干净的元素也当成脏的），但在安全分析中，宁可误报（False Positive）不可漏报（False Negative）。

---

## 失败场景 3: 接口多态回调 (Polymorphism & Callbacks)

### 现象与举例
污染源传给了一个接口的方法，但实际上执行的是实现了该接口的恶意实现类。

**代码示例:**
```java
public interface Callback { void call(String data); }
public class VulnerableSink implements Callback { ... }

public void testPolymorphism(String sourceInput) {
    Callback callback = new VulnerableSink();

    // 数据流成功: sourceInput 连向了 Callback.call 的参数
    // 数据流断裂: 无法连向 VulnerableSink.call 的参数
    callback.call(sourceInput);
}
```

**原因分析:**
虽然 OmniCPG 有类型解析（Typed Resolution），但在构建调用图（Call Graph，即 `CALLS` 边）时，如果没有做深度的 **指针分析（Points-to Analysis/Class Hierarchy Analysis）**，引擎只知道调用了接口 `Callback.call`，不知道实际上指向的是 `VulnerableSink`。

### 改进方案：类层次结构分析（CHA）与动态分发解析
1. **引入 CHA（Class Hierarchy Analysis）**：
   利用现有的 `IMPLEMENTS` 边，当遇到对接口 `I.method()` 的调用时，自动查找所有实现了 `I` 的类（例如 `C1, C2`）。
2. **CallSite 边扩展（虚拟调用图生成）**：
   在后处理阶段（Graph Enrichment），将 `CALLS` 边从 `I.method()` 扇出（Fan-out）到 `C1.method()` 和 `C2.method()`。
   数据流 `REACHES` 边则沿着这些扩展的 `CALLS` 边，将实参同时传递给所有潜在实现类的形参。

---

## 演进路线建议 (Roadmap)

本方案的各项优化排期已整合并同步合入项目整体的 [sota_improvement_plan.md](file:///d:/workspace/mflow-cpg/openspec/changes/sota_improvement_plan.md) 演进路线图中的**阶段二**。

如果要在 OmniCPG 中修复这些问题，建议按照以下优先级排期：

1. **短期 (Quick Win)**：实现**集合容器的粗粒度污点传染**。在 DFG 生成器的后处理阶段，拦截 `add/put` 和 `get`，建立跳板边。
2. **中期 (Medium Effort)**：在 `enrich_graph.py` 中增强 **CHA 多态解析**。对于接口调用，沿着 `IMPLEMENTS` 边自动将 `CALLS` 边桥接到实现类，从而打通数据流。
3. **长期 (High Effort)**：实现**属性敏感的 DFG 建模**（Field-Sensitive DFG），为对象的内部成员分配独立的节点，解决 Object Wrapping 问题。