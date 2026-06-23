import os
import sys
import asyncio
from dotenv import load_dotenv

# Add src to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from omnicpg.orchestrator.pipeline import run_analysis_pipeline
from m_flow.adapters.graph import get_graph_provider
from mflow_cpg.linker import ConceptToCodeLinker

def scan_project_files(root_dir: str) -> list[str]:
    exclude_dirs = {
        ".git", ".venv", "venv", ".mypy_cache", ".pytest_cache", 
        ".ruff_cache", "__pycache__", "mflow_cpg.egg-info", "allure-results", "allure-report"
    }
    supported_extensions = {".py"}
    matched_files = []
    
    for root, dirs, files in os.walk(root_dir):
        # Modify dirs in-place to prevent walking excluded directories
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]
        
        for file in files:
            ext = os.path.splitext(file)[1]
            if ext in supported_extensions:
                full_path = os.path.join(root, file).replace("\\", "/")
                matched_files.append(full_path)
                
    matched_files.sort()
    return matched_files

async def main():
    load_dotenv()
    project_path = os.getenv("PROJECT_PATH", "d:/workspace/mflow-cpg")
    project_id = os.getenv("OMNICPG_PROJECT_ID", "mflow-cpg-self")
    
    print(f"Scanning project files under: {project_path}")
    files_to_analyze = scan_project_files(project_path)
    print(f"Found {len(files_to_analyze)} files to analyze:")
    for f in files_to_analyze:
        print(f"  - {os.path.relpath(f, project_path)}")
        
    print(f"\nStarting analysis of project: {project_path}")
    print(f"Project ID: {project_id}")
    
    # Run CPG analysis pipeline
    result = run_analysis_pipeline(
        path=project_path,
        project_id=project_id,
        clear_db=True,
        language="python",
        specific_files=files_to_analyze
    )
    print("\nCPG Pipeline Analysis Result:")
    import json
    print(json.dumps(result, indent=2))
    
    if result.get("status") == "success":
        print("\nEstablishing links between business concepts and code structures...")
        try:
            db = await get_graph_provider()
            linker = ConceptToCodeLinker(db)
            link_res = await linker.link_concepts_and_code(project_id=project_id)
            print("Linker result:", json.dumps(link_res, indent=2))
        except Exception as e:
            print(f"Warning: Linking business concepts failed: {e}")
            print("Make sure Neo4j is running and accessible.")

if __name__ == "__main__":
    asyncio.run(main())
