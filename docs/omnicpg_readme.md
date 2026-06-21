# OmniCPG

> **A Code Property Graph (CPG) engine for multi-language static analysis, connected to AI IDEs via the Model Context Protocol (MCP).**

OmniCPG parses source code into a unified graph structure — combining the **Abstract Syntax Tree (AST)**, **Control-Flow Graph (CFG)**, and **Data-Flow Graph (DFG)** — and persists it to Neo4j. AI assistants (Cursor, Windsurf, Claude Desktop) can then query this graph in real-time through a standard MCP server.

---

## ✨ What can OmniCPG do?

| Capability | Description |
|------------|-------------|
| **Multi-language CPG** | Full AST + CFG + DFG construction for Python and Java. Support for OpenAPI schema extraction and generic LSIF integration. |
| **Graph persistence** | Persists the unified AST/CFG/DFG/Call-graph into Neo4j |
| **MCP integration** | A suite of standardised tools consumable by any MCP-compatible AI IDE (run `get_server_info` for the authoritative tool count) |
| **Call graph** | Inter-procedural call graph; Java uses multi-round typed-resolution (≈99% typed on a real legacy Struts1 project) |
| **Data-flow analysis** | Reaching-definitions via worklist algorithm (handles branches & loops) |
| **Control-flow analysis** | Full CFG including `try/except/finally` exception flows |
| **Code slicing** | Forward/backward program slicing via `CodeSlicer` |
| **Extensible plugins** | Add any language by implementing the `LanguagePlugin` interface |

---

## 🏗️ Architecture

OmniCPG is composed of three distinct layers:

```
┌─────────────────────────────────────────────────────────────┐
│                     AI IDE / Agent                          │
│         (Cursor · Windsurf · Claude Desktop · etc.)        │
└────────────────────────┬────────────────────────────────────┘
                         │  MCP (SSE / stdio)
┌────────────────────────▼────────────────────────────────────┐
│                  MCP Server  (port 8080)                    │
│  query_nodes · query_edges · get_call_graph · find_path    │
│  find_data_flow · find_control_flow · analyze_function ...  │
└────────────────────────┬────────────────────────────────────┘
                         │  Bolt  (port 7687)
┌────────────────────────▼────────────────────────────────────┐
│                   Neo4j Graph Database                      │
│   Nodes: Module, File, Class, Method, Field, Import        │
│   Edges: PARENT_OF · CONTAINS · CALLS · FLOWS_TO · REACHES  │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│              Analysis Engine  (src/omnicpg)                 │
│  ProjectOrchestrator → LanguagePlugin → {AST,CFG,DFG,CG}  │
└─────────────────────────────────────────────────────────────┘
```

**Key modules** (verified from graph analysis of OmniCPG's own codebase):

| Module path | Purpose |
|-------------|---------|
| `src/omnicpg/orchestrator/` | `ProjectOrchestrator` — scans directories, batches files, coordinates plugins |
| `src/omnicpg/plugins/python_plugin/` | Python AST/CFG/DFG/CallGraph builders |
| `src/omnicpg/plugins/java_plugin/` | Java AST/CFG/DFG/CallGraph builders (+ Spring/Struts/Hibernate) |
| `src/omnicpg/plugins/openapi_plugin/` | OpenAPI schema parsing (APIEndpoint node extraction) |
| `src/omnicpg/plugins/lsif_plugin/` | Generic LSIF plugin adapter |
| `src/omnicpg/interfaces/` | `LanguagePlugin` ABC + `GraphDBAdapter` ABC |
| `src/omnicpg/adapters/` | `Neo4jAdapter` + `JoernAdapter` (CSV export) |
| `src/omnicpg/models/` | `CPGNode`, `CPGEdge`, `EdgeType`, `AnalysisLevel` |
| `src/omnicpg/slicer/` | `CodeSlicer` — forward/backward slicing, neighbourhood queries |
| `src/omnicpg/cache/` | `ExpansionCache` — hot-method preloading for MCP tools |
| `mcp_server_omnicpg/` | MCP server, SSE transport, tool implementations |

---

## 🚀 Quick Start

### Prerequisites
- Docker Desktop
- Python 3.11+
- `uv` (recommended) or `pip`

### 1. Clone & install
```bash
git clone https://github.com/DaviDWCN/OmniCPG.git
cd OmniCPG
pip install -e ".[dev]"
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env:
#   PROJECT_PATH   = /path/to/your/project   # directory to analyse
#   LANGUAGE       = auto                     # auto | python | java | openapi | lsif
#   ANALYSIS_MODE  = export                   # analyze (in-memory) | export (to Neo4j)
#   ANALYSIS_LEVEL = ARCHITECTURAL            # ARCHITECTURAL | STRUCTURAL | FULL
#   NEO4J_PASSWORD = password
```

### 3. Start services
```bash
docker-compose up -d          # starts Neo4j + MCP server
```

### 4. Analyse a project
```bash
python run.py --mode export   # parses code → Neo4j
```

### 5. Connect your IDE

**Cursor / Windsurf** — add to `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "omnicpg": {
      "url": "http://localhost:8080/sse",
      "transport": "sse"
    }
  }
}
```

**Claude Desktop** — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "omnicpg": {
      "url": "http://localhost:8080/sse"
    }
  }
}
```

Then ask your AI: *"What functions does `ProjectOrchestrator.analyze` call?"*

---

## 🧪 Test Case Visualization

AI-generated or hand-written behavior tests can be described in Chinese Gherkin under
`features/*.feature`. Behave executes those scenarios, and Allure result files show each
scenario's test intent, source input, expected graph facts, actual CPG nodes, and edge counts.

```bash
make bdd         # Run Behave and emit allure-results/
make bdd-report  # Generate allure-report/ (requires the Allure CLI)
make bdd-serve   # Serve the Allure report locally (requires the Allure CLI)
```

`pytest` remains the core quality gate; the BDD/Allure layer is for readable acceptance
coverage and visualizing what generated test cases are proving.

---

## 🔧 MCP Tools

The MCP server at `http://localhost:8080/sse` exposes a suite of tools. Run
`get_server_info` for the authoritative count and full list; the core query/analysis
tools are:

| Tool | Description |
|------|-------------|
| `query_nodes` | Filter nodes by type, name, file path |
| `query_edges` | Filter edges by type, source, or target |
| `get_node_by_id` | Fetch any node by its unique ID |
| `get_call_graph` | Traverse caller/callee chains from a named function |
| `get_dependencies` | In/out-bound dependency analysis for any node |
| `get_file_structure` | Hierarchical view of classes and methods in a file |
| `analyze_function` | Full CPG snapshot of a single function |
| `find_path` | Shortest path between any two nodes |
| `find_data_flow` | REACHES-edge path from a definition to a use |
| `find_control_flow` | FLOWS_TO-edge path through the CFG |

For Java call graphs, `find_callsite_method` / `find_callers_of` /
`batch_callsite_methods` resolve call sites via the `CallSite` path. See
[`openspec/specs/mcp-server/spec.md`](openspec/specs/mcp-server/spec.md) for the
full tool contract.

---

## 📦 Graph Schema

After analysis, Neo4j contains nodes labelled:

`Module` · `File` · `Package` · `Class` · `Interface` · `Method` · `Function` ·
`Field` · `Parameter` · `Variable` · `Import` · `CallSite`

Java framework / enrichment labels (when present): `StrutsAction` ·
`AnnotationUsage` · `JspPage` · `ExternalMethod`. All nodes also carry the generic
`Node` label. The label set is non-closed — see
[`openspec/specs/neo4j-schema/spec.md`](openspec/specs/neo4j-schema/spec.md).

### Edge types

| Edge | Meaning |
|------|---------|
| `PARENT_OF` | AST parent → child containment |
| `CONTAINS` | Structural containment (file → class → method) |
| `CALLS` | Inter-procedural call graph edge |
| `FLOWS_TO` | CFG control-flow edge (with optional `condition` property) |
| `REACHES` | DFG data-flow edge (variable definition → use) |
| `DEPENDS_ON` | Module/package import dependency |
| `IMPLEMENTS` | Type implementation / inheritance |

---

## 🐳 Docker Services

```yaml
# docker-compose.yml starts two containers:
omnicpg-neo4j:   Neo4j 5.x  — http://localhost:7474  bolt://localhost:7687
omnicpg-mcp:     MCP Server  — http://localhost:8080/sse
```

---

## ⚠️ Current Limitations (Data Flow)

While OmniCPG performs robust intra-procedural assignment tracking and direct inter-procedural tracing, the Data-Flow Graph (`REACHES` edges) currently exhibits blind spots typical of static analysis engines lacking heap-modeling. Specifically, data flow tracing may break across:
1. **Object Wrapping:** Field assignments via constructors and subsequent extractions via getters.
2. **Collection Storage:** Variables stored and retrieved from structures like `java.util.List`.
3. **Polymorphism:** Interfaces resolving to dynamic implementation classes.

For a detailed analysis of these limitations and the proposed roadmap for mitigation, please refer to the [Data Flow Improvements Proposal](data_flow_improvements_proposal.md) and the [Evaluation Report](evaluation_execution_report.md).

---

## 📊 Graph Statistics (OmniCPG analyzed on itself)

| Metric | Count |
|--------|-------|
| Total nodes | 78,940 |
| Total edges | 91,733 |
| PARENT_OF edges | 78,784 |
| CALLS edges | 2,497 |
| FLOWS_TO edges | 5,360 |
| REACHES edges | 2,711 |
| CONTAINS edges | 738 |
| Source files analysed | 78 |

---

## 🧪 Development

```bash
# Run all unit tests
python -m pytest tests/unit/ -q

# Type-check
mypy src/ mcp_server_omnicpg/

# Lint
ruff check src/ mcp_server_omnicpg/
```

**Code coverage target**: ≥ 80% (enforced by `pyproject.toml`)

For the development workflow, testing, and contribution guidelines, see the
OpenSpec docs below.

---

## 📚 Documentation

All project documentation lives in [`openspec/`](openspec/) — the **single source
of truth**. Start here:

| Document | Content |
|----------|---------|
| [`openspec/project.md`](openspec/project.md) | Project context, stack, conventions (read first) |
| [`openspec/AGENTS.md`](openspec/AGENTS.md) | Guide for AI assistants / MCP workflow |
| [`openspec/specs/`](openspec/specs/) | Capability specs (Requirements + Scenarios) |
| [`openspec/changes/`](openspec/changes/) | In-flight proposals and archive |

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.
