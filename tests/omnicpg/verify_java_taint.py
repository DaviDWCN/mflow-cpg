import logging

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.plugins.java_plugin.plugin import JavaPlugin

# Setup logging
logging.basicConfig(level=logging.INFO)


def verify_java_taint() -> None:
    """Verify inter-procedural taint analysis for Java."""
    plugin = JavaPlugin()

    # 1. Parse files
    caller_path = "tests/java_taint_test/Caller.java"
    callee_path = "tests/java_taint_test/Callee.java"

    with open(caller_path, encoding="utf-8") as f:
        caller_code = f.read()
    with open(callee_path, encoding="utf-8") as f:
        callee_code = f.read()

    nodes1, edges1 = plugin.parse_to_ast(caller_path, caller_code, AnalysisLevel.FULL)
    nodes2, edges2 = plugin.parse_to_ast(callee_path, callee_code, AnalysisLevel.FULL)

    all_nodes = nodes1 + nodes2
    all_edges = edges1 + edges2

    # 2. Build intra-procedural CFG/DFG for each
    cfg1 = plugin.build_cfg(nodes1, edges1)
    dfg1 = plugin.build_dfg(nodes1, cfg1, edges1)

    cfg2 = plugin.build_cfg(nodes2, edges2)
    dfg2 = plugin.build_dfg(nodes2, cfg2, edges2)

    all_edges.extend(cfg1 + dfg1 + cfg2 + dfg2)

    # 3. Build Call Graph
    cg_edges = plugin.build_call_graph(all_nodes, all_edges)
    all_edges.extend(cg_edges)

    # 4. Build Inter-procedural DFG (The New Logic!)
    print("\nDebug: Inspecting Call Node children:")
    call_node = next(n for n in all_nodes if n.properties.get("type") == "method_invocation")
    for rel in all_edges:
        if rel.source_id == call_node.id and rel.edge_type == "PARENT_OF":
            child = next(n for n in all_nodes if n.id == rel.target_id)
            print(f"  Child: {child.properties.get('type')} (Labels: {child.labels})")

    print("\nDebug: Inspecting Method Node children:")
    method_node = next(
        n
        for n in all_nodes
        if n.properties.get("type") == "method_declaration"
        and n.properties.get("name") == "doSomething"
    )
    for rel in all_edges:
        if rel.source_id == method_node.id and rel.edge_type == "PARENT_OF":
            child = next(n for n in all_nodes if n.id == rel.target_id)
            print(f"  Child: {child.properties.get('type')} (Labels: {child.labels})")
            if child.properties.get("type") == "formal_parameters":
                for rel2 in all_edges:
                    if rel2.source_id == child.id and rel2.edge_type == "PARENT_OF":
                        grandchild = next(n for n in all_nodes if n.id == rel2.target_id)
                        print(
                            f"    Grandchild: {grandchild.properties.get('type')}"
                            f" (Labels: {grandchild.labels})"
                        )

    inter_edges = plugin.build_interprocedural_dfg(all_nodes, all_edges)

    # 5. Check results
    print(f"Generated {len(inter_edges)} inter-procedural edges.")

    arg_bindings = [e for e in inter_edges if e.properties.get("interprocedural") == "argument"]
    ret_bindings = [e for e in inter_edges if e.properties.get("interprocedural") == "return"]

    print(f"Argument bindings: {len(arg_bindings)}")
    print(f"Return bindings: {len(ret_bindings)}")

    success = len(arg_bindings) > 0 and len(ret_bindings) > 0
    if success:
        print("\nSUCCESS: Java Inter-procedural Taint Analysis is WORKING!")
        for _ in arg_bindings:
            print(" - Argument -> Parameter binding found.")
        for _ in ret_bindings:
            print(" - Return -> CallSite binding found.")
    else:
        print("\nFAILURE: Could not find cross-file bindings.")


if __name__ == "__main__":
    verify_java_taint()
