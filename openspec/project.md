# M-Flow × OmniCPG (Unified Code Memory & Property Graph Engine)

This project integrates **M-Flow** (a semantic memory knowledge graph layer) and **OmniCPG** (a static analysis Code Property Graph engine) into a unified codebase.

Both systems now share a single **Neo4j** graph database backend and are configured through a single unified `config.yaml` file.

## Core Fusion Value

The unified architecture aims to bridge the gap between **business rules and development decisions** (M-Flow's semantic memory) and **source code structure and program logic** (OmniCPG's code property graph).
- **M-Flow (Cognitive Memory Graph)**: Excels at understanding the "Why" (business context, design intent, historical decisions).
- **OmniCPG (Code Property Graph)**: Excels at analyzing the "How & Where" (syntax, control flow, data flow, call chains).

## Technology Stack

### Languages & Execution
- **Python 3.11+**
- **Docker & Docker Compose** (Optional, only for remote Neo4j + SSE mode)

### Backends
- **Kuzu**: Default database for local mode. Embedded, in-process, zero-dependency.
- **Neo4j (5.x)**: Optional database for remote/production mode. Can be run via Docker.
- **Ollama / GPUStack**: For local LLM processing (semantic summaries, intent extraction) and text embeddings (e.g., `nomic-embed-text`, `bge-m3`).

### Core Frameworks
- **M-Flow**: Internal semantic memory graph and search pipeline.
- **OmniCPG**: Static analysis engine powered by **tree-sitter** (Python, Java plugins).
- **MCP (Model Context Protocol)**: Exposes a unified set of tools (SSE/stdio transports) for AI IDEs like Cursor, Windsurf, and Claude Desktop.

## Architecture

```text
                    ┌──────────────────────────────────────┐
                    │          M-Flow 认知记忆图谱          │
                    │   (Episode · Facet · Business Entity)│
                    └──────────────────┬───────────────────┘
                                       │
                                       │ 关联映射 (Concept-to-Code)
                                       │
                    ┌──────────────────▼───────────────────┐
                    │         OmniCPG 代码属性图谱         │
                    │   (Method · Class · CallSite · DFG)  │
                    └──────────────────────────────────────┘
```

The system is composed of several layers:
- **`src/m_flow/`**: M-Flow core codebase (semantic indexing, recall, memory pipeline).
- **`src/omnicpg/`**: OmniCPG core codebase (tree-sitter parsers, orchestrator, static analysis).
- **`src/mflow_cpg/`**: Thin integration layer, including a unified `MCP server`, `SyntaxAwareCodeChunker`, `CPGRetriever`, and `ConceptToCodeLinker`.

## Conventions & Rules

1. **Single Source of Truth**: All configuration must be defined in `config.yaml` at the project root.
2. **Documentation**: The `openspec/` directory is the single source of truth for all documentation. Changes and proposals go into `openspec/changes/`, and verified capabilities into `openspec/specs/`.
3. **Graph Persistence**: Everything converges in Neo4j. We do not use separate vector stores if Neo4j can handle vector indices, keeping the architecture unified.
4. **Code Quality**: Code coverage target is >= 80%. Type-checking with `mypy` and linting with `ruff` is enforced. Unit tests and Behavior-Driven Development (BDD) tests reside in `tests/` and `features/`.

## Workflow Overview

- **Analysis**: OmniCPG parses Python/Java into AST, CFG, and DFG and stores them in Neo4j.
- **Ingestion**: M-Flow can now ingest code structurally (using OmniCPG as a frontend) to preserve method/class boundaries via `SyntaxAwareCodeChunker`.
- **Linking**: The `ConceptToCodeLinker` establishes bidirectional links (e.g., `IMPLEMENTED_BY`) between M-Flow business concepts and OmniCPG code entities.
- **Agent Usage**: AI agents connect to the unified MCP server on port 8080 and can perform hybrid reasoning (querying M-Flow for business context and OmniCPG for exact code paths).
