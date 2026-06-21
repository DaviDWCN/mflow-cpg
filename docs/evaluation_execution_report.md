# OmniCPG Data Flow Evaluation Report

## Executive Summary
This report summarizes the execution of the evaluation plan formulated to test OmniCPG's Code Property Graph (CPG) capabilities, focusing heavily on micro-benchmarking and data-flow edge (`REACHES`) tracking through complex scenarios.

## Phase 1: Micro-benchmark Validation
We ran the project's foundational testing suites to establish a baseline of graph construction quality.

*   **Unit Tests:** Executed 643 unit tests (`uv run --extra dev pytest tests/unit/ -q`). Result: **100% Pass**. This confirms the integrity of core extraction logic, language plugins (Python/Java), the pipeline orchestrator, and the MCP adapter logic.
*   **BDD Tests (Features):** Executed behavior-driven scenarios (`uv run behave features/`). Result: **100% Pass**. This verified that fundamental AST shapes (e.g., Python class to method `PARENT_OF` relationships) are structured precisely as specified.

## Phase 2: Data Flow Inter-procedural Challenges
We created a custom `DataFlowChallenge.java` containing common blindspots for static analysis tools:
1.  **Object Wrapping:** `new DataWrapper(sourceInput)` then `wrapper.getValue()`
2.  **Collection Storage:** `List<String> list = new ArrayList<>(); list.add(sourceInput); list.get(1);`
3.  **Polymorphism:** `Callback callback = new VulnerableSink(); callback.call(sourceInput);`

### Findings
We analyzed the challenge file using `ProjectOrchestrator` running in `FULL` analysis level. OmniCPG successfully generated 272 nodes, 22 CFG edges (`FLOWS_TO`), and 14 intra-procedural + 6 inter-procedural DFG edges (`REACHES`).

By inspecting the generated `REACHES` edges directly, we observed:
*   **Standard Assignments (Pass):** OmniCPG correctly maps standard parameter-to-variable definitions. e.g., `[formal_parameter] String sourceInput -> [identifier] sourceInput`.
*   **Direct Method Invocations (Pass):** The inter-procedural data flow successfully bridges call arguments to formal parameters in simple cases. e.g., `[identifier] extracted -> [formal_parameter] String param`.
*   **Object Wrapping (Fail):** The flow breaks inside the class definition. While `sourceInput` goes into the constructor, there is no structural edge mapping the constructor's assigned field back out through `getValue()`.
*   **List Storage (Fail):** Static taint analysis fundamentally struggles here without heap modeling. OmniCPG correctly traces `sourceInput` into `list.add()`, but fails to bridge the data flow back out when calling `list.get(1)`.
*   **Polymorphism (Pass/Fail mix):** It generates edges mapping `sourceInput` to `[formal_parameter] String data`. Due to the `CallSite` fallback or typed resolution limits, bridging interface invocations directly to the concrete `VulnerableSink` implementation remains challenging without deeper points-to analysis.

## Conclusion
OmniCPG generates an exceptionally clean and robust structural graph (AST, CFG) and handles standard variable-to-variable and clear inter-procedural data flows. However, for deep vulnerability analysis tracking "Source-to-Sink", the engine exhibits typical data flow blind spots (Object Wrapping and Collection tracking). Future enhancements should focus on introducing lightweight taint propagation or heap-modeling heuristics during DFG generation to bridge these "broken" paths.