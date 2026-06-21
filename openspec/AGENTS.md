# AGENTS.md: M-Flow × OmniCPG Unified MCP Workflow Guide

This guide describes how AI Assistants (e.g., Cursor, Windsurf, Claude Desktop) should interact with the M-Flow × OmniCPG unified codebase using Model Context Protocol (MCP).

You are acting on an integrated architecture that combines:
1. **M-Flow**: A Semantic Memory Knowledge Graph (Business rules, intent, cognitive memory).
2. **OmniCPG**: A Code Property Graph (Syntax, control flow, data flow, structure).

## Core Principles for Hybrid Reasoning

When tasked with complex feature analysis, reverse-engineering, or debugging, you should perform **Hybrid Agent Reasoning**—combining "Why" (M-Flow) with "How & Where" (OmniCPG).

### Workflow: Global-to-Local Hybrid Reasoning

1. **Retrieve Business Context & Procedural Rules (M-Flow)**
   - **When to do this**: You need to understand terminologies, business procedures, chronological rules, or high-level architecture intent before diving into code.
   - **Action**: Use the `mflow_search` MCP tool with appropriate `recall_mode` (e.g., `EPISODIC`, `PROCEDURAL`) to query the knowledge graph. This will give you the *Why* and the *What*.
   - **Result**: You should identify core business entities and, ideally, the names of key classes, tables, or methods involved in the process.

2. **Locate Code Entrypoints (OmniCPG)**
   - **When to do this**: You have the names of classes/methods and need to find their exact location in the codebase and their structural relationships.
   - **Action**: Use OmniCPG MCP tools like `query_nodes` (searching by `name` or `type`) or `search_code` to locate the exact nodes representing these elements.

3. **Trace and Analyze Execution Paths (OmniCPG)**
   - **When to do this**: You need to understand data propagation, control logic, or inter-procedural call chains stemming from the entrypoints.
   - **Action**:
     - Use `get_call_graph` to see cross-system calls or what dependencies a method has.
     - Use `find_data_flow` (Data Flow Graph) to trace variables from definitions/sources to their uses/sinks (e.g., tracing a parameter into a database call).
     - Use `find_control_flow` (Control Flow Graph) to understand branch logic, error handling, and conditions (e.g., why a certain validation failed).
     - Use `analyze_function` to get a complete CPG snapshot of a specific function.

4. **Synthesize**
   - Combine the insights: "Because of business rule X retrieved from M-Flow, the control flow in OmniCPG branches at node Y, passing data to sink Z."

## Example Scenario

**Task**: "Why did the HCS push to reins4 fail during `checkPush`?"

1. **M-Flow step**: Use `mflow_search` with the query "HCS push order to reins4".
   - *Discovery*: M-Flow indicates pushes must follow a strict order: PL -> ES -> RRPN -> RREN.
2. **OmniCPG step 1**: Use `query_nodes` to find the method `checkPush` in the `HtInterFaceServiceImpl` class.
3. **OmniCPG step 2**: Use `find_control_flow` starting from the `checkPush` node to analyze the validation branches.
   - *Discovery*: The control flow shows a branch that blocks execution if a specific table's push status flag is false.
4. **Conclusion**: Provide a comprehensive diagnostic report linking the strict sequence requirement (M-Flow) to the exact conditional branch that caused the failure (OmniCPG).

## Tools Available via the Unified MCP Server

The unified server (port 8080) exposes tools from both systems:

### M-Flow Tools
- `mflow_search`: Semantic search across memory (business context and code graph).
- `mflow_add`: Add a document/text to the memory space.
- `concept_to_code_link`: Establish bidirectional links (`IMPLEMENTED_BY`) in Neo4j between M-Flow concepts and OmniCPG structures.

### OmniCPG Tools (Sample)
- `query_nodes` / `query_edges` / `get_node_by_id`
- `get_call_graph` / `get_dependencies`
- `analyze_function` / `get_file_structure`
- `find_path` / `find_data_flow` / `find_control_flow`

*(Note: Run `get_server_info` or `list_tools` for a complete, authoritative list).*

## Best Practices

- **Always confirm context first**: Before attempting to read hundreds of lines of code or guessing data flows, query M-Flow to understand the intent.
- **Trace deliberately**: Use CPG tools (`find_data_flow`, `get_call_graph`) rather than grep when analyzing complex projects, as CPG tools understand semantics and type resolution.
- **Report findings holistically**: Whenever possible, link code findings back to business constraints.
