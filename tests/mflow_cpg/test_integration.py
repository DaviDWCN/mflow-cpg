import os
import json
import pytest
from mflow_cpg.config import get_config
from m_flow.adapters.graph import get_graph_provider
from mflow_cpg.semantic_engine import SemanticEnrichmentEngine
from mflow_cpg.linker import ConceptToCodeLinker
from mflow_cpg.retriever import CPGRetriever
from omnicpg.orchestrator.pipeline import run_analysis_pipeline

@pytest.mark.asyncio
async def test_unified_flow():
    # 1. Load configuration
    cfg = get_config()
    print("\nUnified config loaded:", cfg)
    
    # 2. Check Neo4j connectivity
    try:
        db = await get_graph_provider()
        # Clean up database
        await db.query("MATCH (n) DETACH DELETE n")
        print("Neo4j connected and database cleared.")
    except Exception as e:
        pytest.skip(f"Neo4j is not reachable. Skipping live integration test: {e}")

    # 3. Create a temporary Python codebase
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_code = """
class PMLCalculator:
    \"\"\"Calculates Reinsurance PML (Probable Maximum Loss) shares.\"\"\"
    
    def __init__(self, limit: float, share: float):
        self.limit = limit
        self.share = share
        
    def calculate_pml_share(self) -> float:
        \"\"\"Computes the PML share amount.\"\"\"
        return self.limit * self.share
"""
        code_file = os.path.join(tmpdir, "pml_calc.py")
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(sample_code)

        # 4. Run OmniCPG analysis pipeline to parse the codebase to Neo4j
        print("Running OmniCPG analysis pipeline on temporary python file...")
        result = run_analysis_pipeline(
            path=tmpdir,
            project_id="test_pml_project",
            clear_db=True
        )
        print("CPG pipeline result:", result)
        assert result["status"] == "success"

        # 5. Create a mock Entity in Neo4j to simulate M-Flow memory
        await db.query(
            "CREATE (e:Entity {name: 'PMLCalculator', canonical_name: 'PMLCalculator'})"
        )
        print("Created mock Entity 'PMLCalculator' in Neo4j.")
        
        # 6. Run Concept to Code Linker
        linker = ConceptToCodeLinker(db)
        link_res = await linker.link_concepts_and_code(project_id="test_pml_project")
        print("Linker result:", link_res)
        assert link_res["links_created"] > 0

        # 7. Retrieve code context using CPGRetriever (which searches Neo4j cross-graph)
        retriever = CPGRetriever()
        context = await retriever.get_context("PMLCalculator")
        print("Retrieved context from CPGRetriever:\n", context)
        assert "Class: PMLCalculator" in context or "PMLCalculator" in context
