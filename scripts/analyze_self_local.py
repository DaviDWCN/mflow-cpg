import os
import sys
import ast
import asyncio
from pathlib import Path
from types import ModuleType
from dotenv import load_dotenv

# Stub mflow_workers
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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from m_flow.adapters.graph import get_graph_provider

class PythonASTVisitor(ast.NodeVisitor):
    def __init__(self, module_name: str):
        self.module_name = module_name
        self.classes = []
        self.functions = []
        self.current_class = None

    def visit_ClassDef(self, node):
        class_fqn = f"{self.module_name}.{node.name}"
        self.classes.append({
            "name": node.name,
            "fqn": class_fqn,
            "bases": [ast.unparse(b) for b in node.bases]
        })
        
        old_class = self.current_class
        self.current_class = class_fqn
        self.generic_visit(node)
        self.current_class = old_class

    def visit_FunctionDef(self, node):
        func_name = node.name
        if self.current_class:
            func_fqn = f"{self.current_class}.{func_name}"
        else:
            func_fqn = f"{self.module_name}.{func_name}"
            
        self.functions.append({
            "name": func_name,
            "fqn": func_fqn,
            "is_method": self.current_class is not None
        })
        self.generic_visit(node)

async def main():
    load_dotenv()
    project_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    project_id = "mflow-cpg-self"
    
    print(f"Initializing local Kuzu database...")
    db = await get_graph_provider()
    
    # 1. Clear database before clean run
    await db.delete_graph()
    print("Local database cleared.")
    
    src_dir = os.path.join(project_path, "src")
    py_files = []
    for root, _, files in os.walk(src_dir):
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))
                
    print(f"Parsing {len(py_files)} Python source files using AST...")
    nodes_count = 0
    edges_count = 0
    
    for filepath in py_files:
        rel_path = os.path.relpath(filepath, src_dir).replace("\\", "/")
        module_parts = os.path.splitext(rel_path)[0].split("/")
        if module_parts[-1] == "__init__":
            module_parts = module_parts[:-1]
        module_name = ".".join(module_parts)
        
        with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
            try:
                tree = ast.parse(fh.read(), filename=filepath)
            except Exception as e:
                print(f"Warning: Failed to parse {filepath}: {e}")
                continue
                
        visitor = PythonASTVisitor(module_name)
        visitor.visit(tree)
        
        # Insert module node
        module_id = f"module_{module_name}"
        await db.add_node(module_id, {"type": "Module", "name": module_name, "fqn": module_name, "project_id": project_id})
        nodes_count += 1
        
        # Insert classes
        for c in visitor.classes:
            class_id = f"class_{c['fqn']}"
            await db.add_node(class_id, {"type": "Class", "name": c["name"], "fqn": c["fqn"], "project_id": project_id})
            await db.add_edge(module_id, class_id, "CONTAINS")
            nodes_count += 1
            edges_count += 1
            
        # Insert functions
        for f in visitor.functions:
            func_id = f"method_{f['fqn']}"
            await db.add_node(func_id, {"type": "Method", "name": f["name"], "fqn": f["fqn"], "project_id": project_id})
            nodes_count += 1
            
            if f["is_method"]:
                # Find parent class
                parent_class_fqn = ".".join(f["fqn"].split(".")[:-1])
                parent_class_id = f"class_{parent_class_fqn}"
                await db.add_edge(parent_class_id, func_id, "CONTAINS")
            else:
                await db.add_edge(module_id, func_id, "CONTAINS")
            edges_count += 1

    print(f"Static CPG analysis completed locally. Inserted {nodes_count} nodes, {edges_count} edges into Kuzu.")

    # 2. Insert M-Flow business concept Entity nodes
    concepts = [
        ("UnifiedMCPServer", "mflow_cpg.mcp_server", "M-Flow × OmniCPG 统一 MCP 服务器网关"),
        ("ConceptToCodeLinker", "mflow_cpg.linker.ConceptToCodeLinker", "业务概念与 CPG 代码实体双向关联链接器"),
        ("CPGRetriever", "mflow_cpg.retriever.CPGRetriever", "跨图谱代码与知识混合检索器"),
        ("SyntaxAwareCodeChunker", "mflow_cpg.chunker.SyntaxAwareCodeChunker", "代码语法感知的切片 and 知识分块器"),
        ("SemanticEnrichmentEngine", "mflow_cpg.semantic_engine.SemanticEnrichmentEngine", "大模型辅助的代码语义丰富引擎")
    ]
    
    print("\nEnsuring M-Flow business Entity nodes in Kuzu...")
    for name, canonical, desc in concepts:
        entity_id = f"entity_{name}"
        await db.add_node(entity_id, {"type": "Entity", "name": name, "canonical_name": canonical, "description": desc})
        
    # 3. Perform Concept-to-Code linking locally
    print("\nLinking business concepts to Python code in Kuzu...")
    links_created = 0
    
    # Query all nodes in Kuzu
    all_data = await db.get_graph_data()
    kuzu_nodes = all_data[0]
    
    for name, canonical, desc in concepts:
        entity_id = f"entity_{name}"
        # Search for matching Class/Method nodes
        for node_id, props in kuzu_nodes:
            if props.get("type") in ("Class", "Method", "Module"):
                node_name = props.get("name", "")
                node_fqn = props.get("fqn", "")
                
                # Check match
                if node_name == name or node_fqn.endswith(name):
                    await db.add_edge(entity_id, node_id, "IMPLEMENTED_BY")
                    await db.add_edge(node_id, entity_id, "IMPLEMENTS_CONCEPT")
                    links_created += 1
                    print(f"Linked Entity '{name}' <-> Code '{node_name}' ({node_id})")

    print(f"\nLocal linking process completed: created {links_created} bidirectional relations in Kuzu.")

if __name__ == "__main__":
    asyncio.run(main())
