"""
SyntaxAwareCodeChunker using OmniCPG for AST/Method-level code splitting.
Falls back to TextChunker for non-code files.
"""

from __future__ import annotations

import os
import tempfile
from uuid import NAMESPACE_OID, uuid5
from typing import Any, AsyncGenerator, Callable

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

        # 3. Create temp file if we don't have a valid local processed_path
        temp_file = None
        if not file_path or not os.path.exists(file_path):
            temp_file = tempfile.NamedTemporaryFile(suffix=ext, delete=False, mode="w", encoding="utf-8")
            temp_file.write(full_content)
            temp_file.close()
            file_path = temp_file.name

        try:
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

            nodes, edges = orchestrator.analyze(file_path)
            
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

        finally:
            if temp_file and os.path.exists(file_path):
                os.remove(file_path)
