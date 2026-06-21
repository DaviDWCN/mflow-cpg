import logging
import os

from mcp_server_omnicpg.neo4j_adapter import get_adapter
from mcp_server_omnicpg.tools.code_intelligence import trace_variable

# Setup logging
logging.basicConfig(level=logging.INFO)


def verify() -> None:
    """Verify MCP taint trace against a running Neo4j instance."""
    # 1. Connect
    os.environ["NEO4J_PASSWORD"] = "password"
    adapter = get_adapter()
    adapter.ensure_connected()

    # Trace a common variable in OmniCPG
    var_name = "analysis_level"
    print(f"Tracing variable: {var_name}")

    # 2. Test trace_variable
    traces = trace_variable(var_name)

    if traces:
        print(f"SUCCESS: Found {len(traces)} occurrences.")
        inter_traces = [t for t in traces if t.get("context") == "inter-procedural"]
        if inter_traces:
            print(f"WOW: Found {len(inter_traces)} inter-procedural flow points!")
            for t in inter_traces[:3]:
                print(f" - {t['file_path']}:{t['line']} ({t['flow_types']})")
        else:
            print("Found only local occurrences.")
    else:
        print("FAILURE: Could not find any traces for this variable.")


if __name__ == "__main__":
    verify()
