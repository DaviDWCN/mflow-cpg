# M-Flow × OmniCPG: Coding Agents MCP Integration & Maintenance Guide

This guide describes how to configure and maintain the unified **M-Flow × OmniCPG** (Code Memory & Property Graph) engine for AI Coding Agents (such as **Cursor**, **Windsurf**, or **Claude Desktop**).

---

## 1. Overview of Architecture

* **M-Flow (Why & What)**: Stores business domain concepts (`Entity`), historical context, and rules.
* **OmniCPG (How & Where)**: Stores concrete syntax structures (`Class`, `Method`, `Field`), Control Flow Graphs (CFG), and Data Flow Graphs (DFG).
* **Unified MCP Server**: Bridges both engines into a single Model Context Protocol gateway, exposing semantic search and graph traversals to LLMs.

---

## 2. Usage Mode 1: Local & Lightweight (Kùzu + stdio MCP)

**Recommended for**: Small to medium-sized codebases, local laptop development, high privacy environments, and quick setups.

### How it works:
* **Database**: Uses **Kùzu**, an embedded, in-process graph database (similar to SQLite but for graphs). It runs inside the Python process and saves database files locally. No Docker container or server installation is required.
* **MCP Transport**: Uses standard input/output (**stdio**) pipes for direct communication between the IDE and the local Python interpreter.

### Setup Steps:

1. **Configure Environment (`.env` or `config.yaml`)**:
   Set M-Flow graph database provider to `kuzu` in `config.yaml`:
   ```yaml
   neo4j:
     # Unused when using kuzu, but keep config clean
   
   # Graph provider override via environment (or configuration)
   # MFLOW_GRAPH_DATABASE_PROVIDER=kuzu
   ```
   Or in `d:/workspace/mflow-cpg/.env`:
   ```ini
   MFLOW_GRAPH_DATABASE_PROVIDER=kuzu
   ```

2. **Configure your AI IDE / Client**:
   Configure the local stdio command in your client settings.

   * **For Claude Desktop (`claude_desktop_config.json`)**:
     ```json
     {
       "mcpServers": {
         "mflow-cpg-local": {
           "command": "d:\\workspace\\mflow-cpg\\.venv\\Scripts\\python.exe",
           "args": [
             "-m",
             "mflow_cpg.mcp_server",
             "--transport",
             "stdio"
           ]
         }
       }
     }
     ```
   * **For Cursor / Windsurf**:
     Add a new MCP server:
     * **Name**: `mflow-cpg-local`
     * **Type**: `command`
     * **Command**: `d:\workspace\mflow-cpg\.venv\Scripts\python.exe -m mflow_cpg.mcp_server --transport stdio`

---

## 3. Usage Mode 2: Remote & Production (Neo4j + SSE MCP)

**Recommended for**: Large-scale codebases, shared/team environments, cloud deployments, and remote AI agents.

### How it works:
* **Database**: Uses **Neo4j** (community or enterprise server) running inside Docker or a cloud instance, offering full index support and visualization via Neo4j Browser.
* **MCP Transport**: Uses **SSE** (Server-Sent Events) over HTTP, allowing external agents to query the graph database remotely.

### Setup Steps:

1. **Start Neo4j**:
   Ensure Neo4j is running (e.g., via Docker container) and configured:
   ```yaml
   # config.yaml
   neo4j:
     uri: "bolt://localhost:7687"
     username: "neo4j"
     password: "your_password"
   ```

2. **Start the SSE MCP Daemon**:
   Run the SSE server in the background:
   ```bash
   .venv\Scripts\python -m mflow_cpg.mcp_server --transport sse --port 8080
   ```

3. **Configure your AI IDE / Client**:
   * **For Cursor / Windsurf**:
     Add a new MCP server:
     * **Name**: `omnicpg-hcs-print`
     * **Type**: `SSE`
     * **URL**: `http://localhost:8080/sse`

---

## 4. Graph Incremental Updates (When Code Changes)

When the source codebase is modified (files added, deleted, or edited), the CPG graph database must be updated to prevent **ghost nodes** and stale relationships.

The orchestrator pipeline supports high-performance incremental updates.

### Step 1: Incremental CPG Re-Analysis
Instead of wiping the database (which takes time and deletes custom nodes), run the analysis pipeline in **incremental mode**:

1. **Using Git to capture modified files (Recommended)**:
   You can query modified and deleted files using `git status` and feed them into the orchestrator script:
   ```python
   # Example Python script for incremental updates
   from omnicpg.orchestrator.pipeline import run_analysis_pipeline
   
   run_analysis_pipeline(
       path="d:/workspace/hcs_print",
       project_id="hcs_print",
       clear_db=False,            # CRITICAL: Retains existing database data
       specific_files=modified_list, # Only parse modified files
       deleted_files=deleted_list   # Purge deleted files from the graph
   )
   ```

2. **Using the Resume Flag**:
   If you want the pipeline to automatically scan the directory and only parse files that do not exist in the database:
   ```python
   run_analysis_pipeline(
       path="d:/workspace/hcs_print",
       project_id="hcs_print",
       clear_db=False,
       resume=True  # Skips files already parsed in previous sessions
   )
   ```

3. **How Stale Nodes are Cleaned**:
   When `clear_db=False` is passed, the pipeline runs an **incremental file-level cleanup**. For every file in the analysis list, it executes:
   ```cypher
   MATCH (n:Node {project_id: $project_id, file_path: $file_path}) 
   DETACH DELETE n
   ```
   This ensures that any deleted methods, classes, or fields in the modified files are completely purged before the new AST structure is inserted, maintaining graph integrity.

### Step 2: Re-link Concepts (Business-to-Code mapping)
Once CPG nodes are updated, rerun the bidirectional linker to bind M-Flow business concepts (Entities) back to the updated CPG classes or methods:

* **Via CLI**:
  Rerun the orchestrator analysis script (`scripts/analyze_hcs_print.py`).
* **Via MCP Tool**:
  Ask the coding agent to trigger the MCP tool `concept_to_code_link` with arguments:
  ```json
  {
    "project_id": "hcs_print"
  }
  ```
  This will dynamically refresh the `IMPLEMENTED_BY` and `IMPLEMENTS_CONCEPT` relationship edges in Neo4j or Kùzu.
