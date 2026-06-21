"""
SyntaxAwareCodeChunker using OmniCPG for AST/Method-level code splitting.
Falls back to TextChunker for non-code files.
"""

from __future__ import annotations

import os
import tempfile
from uuid import NAMESPACE_OID, uuid5
from typing import AsyncGenerator

from m_flow.ingestion.chunking.Chunker import Chunker
from m_flow.ingestion.chunking.TextChunker import TextChunker
from m_flow.ingestion.chunking.models.ContentFragment import ContentFragment
from m_flow.shared.logging_utils import get_logger

logger = get_logger()

class SyntaxAwareCodeChunker(Chunker):
    """
    Syntax-aware code chunker using OmniCPG parser.
    Parses methods and classes as syntactic blocks, creating ContentFragments.
    Falls back to TextChunker for non-code files.
    """

    async def read(self) -> AsyncGenerator[ContentFragment, None]:
        # 1. Determine if it's a code file
        file_path = getattr(self.document, "processed_path", "")
        doc_name = getattr(self.document, "name", "")
        
        is_code = False
        ext = ""
        if file_path:
            ext = os.path.splitext(file_path)[1].lower()
        elif doc_name:
            ext = os.path.splitext(doc_name)[1].lower()

        if ext in (".py", ".java"):
            is_code = True

        if not is_code:
            # Fallback to TextChunker
            logger.info(f"File {doc_name} is not Python/Java. Falling back to TextChunker.")
            text_chunker = TextChunker(self.document, self.get_text, self.max_chunk_size)
            async for chunk in text_chunker.read():
                yield chunk
            return

        # 2. Get full text content
        content_parts = []
        async for block in self.get_text():
            content_parts.append(block)
        full_content = "".join(content_parts)

        # 3. Create a temporary directory containing only this file to analyze with ProjectOrchestrator
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_name = os.path.basename(file_path or doc_name or f"temp{ext}")
            temp_file_path = os.path.join(temp_dir, temp_file_name)
            with open(temp_file_path, "w", encoding="utf-8") as f:
                f.write(full_content)

            # 4. Run OmniCPG ProjectOrchestrator in-memory
            from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator
            from omnicpg.plugins.python_plugin.plugin import PythonPlugin
            from omnicpg.plugins.java_plugin.plugin import JavaPlugin
            from omnicpg.models.analysis_level import AnalysisLevel

            plugin = PythonPlugin() if ext == ".py" else JavaPlugin()
            orchestrator = ProjectOrchestrator(
                plugins=[plugin],
                analysis_level=AnalysisLevel.ARCHITECTURAL
            )

            nodes, edges = orchestrator.analyze(temp_dir)
            
            # Pre-compute Class nodes map to attach context to Methods
            class_nodes = {n.id: n for n in nodes if "Class" in n.labels}
            # Find which Class owns each Method using PARENT_OF edges
            method_to_class = {}
            for edge in edges:
                if edge.edge_type == "PARENT_OF" and edge.source_id in class_nodes:
                    method_to_class[edge.target_id] = class_nodes[edge.source_id]

            # Yield ContentFragments for Classes and Methods
            chunk_index = 0
            for node in nodes:
                # We yield Method bodies, and Class definitions
                labels = node.labels
                
                # Fetch text content for the node (defaulting to name/code)
                code_text = node.properties.get("code") or node.properties.get("source_code")
                if not code_text:
                    name = node.properties.get("name", "")
                    fqn = node.properties.get("fqn", "")
                    if name:
                        code_text = f"// Definition of {fqn or name}\nclass {name}: pass"
                    else:
                        continue

                # Construct context prefix (using base filename)
                fqn_or_name = node.properties.get("fqn") or node.properties.get("name") or "unknown"
                node_file_path = os.path.basename(doc_name or node.properties.get("file_path") or "unknown")
                comment_style = "#" if ext == ".py" else "//"

                context_parts = [f"{comment_style} Context: defined in {fqn_or_name} in {node_file_path}"]

                # Contextual Retrieval: Append enclosing Class info to Method
                if "Method" in labels and node.id in method_to_class:
                    parent_class = method_to_class[node.id]
                    cls_name = parent_class.properties.get("name")
                    cls_fqn = parent_class.properties.get("fqn")
                    if cls_name:
                        context_parts.append(f"{comment_style} Enclosing Class: {cls_fqn or cls_name}")
                        # If intent/summary was available, we could append it here,
                        # but in-memory Orchestrator without Neo4j enrichment won't have LLM intents yet.

                context_prefix = "\n".join(context_parts) + "\n"
                code_text = context_prefix + code_text

                # Add CPG metadata
                metadata = {
                    "cpg_node_id": node.id,
                    "cpg_labels": list(labels),
                    "file_path": node.properties.get("file_path", doc_name),
                    "line_start": node.properties.get("line_start"),
                    "line_end": node.properties.get("line_end"),
                    "signature": node.properties.get("signature"),
                    "index_fields": ["text"],
                }

                # Construct ContentFragment
                yield ContentFragment(
                    id=uuid5(NAMESPACE_OID, f"{self.document.id}-{chunk_index}"),
                    text=code_text,
                    chunk_size=len(code_text),
                    is_part_of=self.document,
                    chunk_index=chunk_index,
                    cut_type="syntax_boundary",
                    contains=[],
                    metadata=metadata,
                )
                chunk_index += 1

            if chunk_index == 0:
                # If no AST structures found, yield whole content as fallback
                yield ContentFragment(
                    id=uuid5(NAMESPACE_OID, f"{self.document.id}-0"),
                    text=full_content,
                    chunk_size=len(full_content),
                    is_part_of=self.document,
                    chunk_index=0,
                    cut_type="file_fallback",
                    contains=[],
                    metadata={"index_fields": ["text"], "file_path": doc_name},
                )
