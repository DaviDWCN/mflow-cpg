import os
import sys
import asyncio
from types import ModuleType
from dotenv import load_dotenv

# Stub mflow_workers to bypass import errors in m_flow core
mw = ModuleType("mflow_workers")
mw_tasks = ModuleType("mflow_workers.tasks")
mw_tasks_edges = ModuleType("mflow_workers.tasks.queued_add_edges")
mw_tasks_nodes = ModuleType("mflow_workers.tasks.queued_add_nodes")
mw_utils = ModuleType("mflow_workers.utils")

mw_tasks_edges.queued_add_edges = lambda *a, **k: None
mw_tasks_nodes.queued_add_nodes = lambda *a, **k: None
mw_utils.override_distributed = lambda task: (lambda func: func)

sys.modules["mflow_workers"] = mw
sys.modules["mflow_workers.tasks"] = mw_tasks
sys.modules["mflow_workers.tasks.queued_add_edges"] = mw_tasks_edges
sys.modules["mflow_workers.tasks.queued_add_nodes"] = mw_tasks_nodes
sys.modules["mflow_workers.utils"] = mw_utils

# Add src to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from omnicpg.orchestrator.pipeline import run_analysis_pipeline
from m_flow.adapters.graph import get_graph_provider
from mflow_cpg.linker import ConceptToCodeLinker

def scan_project_files(root_dir: str) -> list[str]:
    exclude_dirs = {
        ".git", ".venv", "venv", ".mypy_cache", ".pytest_cache", 
        ".ruff_cache", "__pycache__", "mflow_cpg.egg-info", "allure-results", "allure-report",
        "target", "out", "bin", ".settings", ".codebuddy"
    }
    supported_extensions = {".java"}
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
    project_path = os.getenv("PROJECT_PATH", "D:/workspace/hcs_print")
    project_id = os.getenv("OMNICPG_PROJECT_ID", "hcs_print")
    
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
        language="java",
        specific_files=files_to_analyze
    )
    print("\nCPG Pipeline Analysis Result:")
    import json
    print(json.dumps(result, indent=2))
    
    if result.get("status") == "success":
        print("\nEstablishing links between business concepts and code structures...")
        try:
            db = await get_graph_provider()
            
            # Ensure M-Flow business concept Entity nodes exist
            concepts = [
                ("DocOperationService", "DocOperationService", "保险文档生成与打印核心业务服务"),
                ("DefaultWordAssembleServiceImpl", "DefaultWordAssembleServiceImpl", "Word 文档组装核心服务"),
                ("TablePrint", "TablePrint", "表格数据处理与打印策略"),
                ("NestTablePrint", "NestTablePrint", "嵌套表格处理策略"),
                ("QrCodePrint", "QrCodePrint", "二维码嵌入与生成打印策略"),
                ("WatermarkPrint", "WatermarkPrint", "水印印记打印策略"),
                ("RideSealPrint", "RideSealPrint", "骑缝章电子签章打印策略"),
                ("CoverWord", "CoverWord", "保单封面结构组装"),
                ("BodyWord", "BodyWord", "保险正文合同文本结构组装"),
                ("ClauseWord", "ClauseWord", "保单风险条款说明结构组装"),
                ("PrintMain", "PrintMain", "保单打印主任务记录模型"),
                ("GgRiskClause", "GgRiskClause", "风险控制条款数据实体"),
                ("GgCompany", "GgCompany", "保险公司实体数据"),
                ("ZipUtils", "ZipUtils", "打印包解压与压缩工具")
            ]
            for name, canonical, desc in concepts:
                await db.query(
                    "MERGE (e:Entity {name: $name}) "
                    "ON CREATE SET e.canonical_name = $canonical, e.description = $desc "
                    "ON MATCH SET e.canonical_name = $canonical, e.description = $desc",
                    {"name": name, "canonical": canonical, "desc": desc}
                )
            print(f"Ensured {len(concepts)} M-Flow Entity nodes exist in Neo4j.")

            linker = ConceptToCodeLinker(db)
            link_res = await linker.link_concepts_and_code(project_id=project_id)
            print("Linker result:", json.dumps(link_res, indent=2))
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Warning: Linking business concepts failed: {e}")
            print("Make sure Neo4j is running and accessible.")

if __name__ == "__main__":
    asyncio.run(main())
