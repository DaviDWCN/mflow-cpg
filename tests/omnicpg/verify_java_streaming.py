import logging

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator
from omnicpg.plugins.java_plugin.plugin import JavaPlugin

# Setup logging
logging.basicConfig(level=logging.INFO)


def verify_java_streaming_orchestrator() -> None:
    """Verify the Java streaming orchestrator end-to-end."""
    java_plugin = JavaPlugin()
    orchestrator = ProjectOrchestrator(plugins=[java_plugin], analysis_level=AnalysisLevel.FULL)

    test_dir = "tests/java_taint_test"

    # 2. Run streaming analysis
    print("\nRunning ProjectOrchestrator.analyze_streaming on Java test project...")
    all_nodes = []
    all_edges = []

    for nodes, edges in orchestrator.analyze_streaming(test_dir, chunk_size=1):
        all_nodes.extend(nodes)
        all_edges.extend(edges)

    print(f"Total nodes: {len(all_nodes)}")
    print(f"Total edges: {len(all_edges)}")

    # 3. Check for inter-procedural REACHES edges
    inter_edges = [
        e for e in all_edges if e.properties.get("interprocedural") in ("argument", "return")
    ]

    print(f"Generated {len(inter_edges)} inter-procedural REACHES edges in streaming mode.")

    arg_bindings = [e for e in inter_edges if e.properties.get("interprocedural") == "argument"]
    ret_bindings = [e for e in inter_edges if e.properties.get("interprocedural") == "return"]

    print(f"Streaming Argument bindings: {len(arg_bindings)}")
    print(f"Streaming Return bindings: {len(ret_bindings)}")

    success = len(arg_bindings) > 0 and len(ret_bindings) > 0
    if success:
        print("\nSUCCESS: Java Streaming/Incremental Analysis is WORKING!")
    else:
        print("\nFAILURE: Streaming mode failed to bind Java data flow.")


if __name__ == "__main__":
    verify_java_streaming_orchestrator()
