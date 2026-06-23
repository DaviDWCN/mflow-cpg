"""
SyntaxAwareCodeChunker using OmniCPG for AST/Method-level code splitting.
Falls back to TextChunker for non-code files.
"""

from __future__ import annotations

import os
import tempfile
from uuid import NAMESPACE_OID, uuid5
from typing import AsyncGenerator
import aiohttp

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

    async def _get_contextual_retrieval_prefix(self, full_document: str, chunk_text: str) -> str:
        from mflow_cpg.config import get_config
        config = get_config()

        if not config.semantic_analysis.enabled:
            return ""

        # Anthropic Contextual Retrieval Prompt
        system_prompt = "You are an expert software engineer."
        prompt = (
            "Here is the full document:\n"
            "<document>\n"
            f"{full_document[:8000]}\n" # truncate to avoid overflowing context
            "</document>\n\n"
            "Here is the target code chunk we want to situate within the whole document:\n"
            "<chunk>\n"
            f"{chunk_text}\n"
            "</chunk>\n\n"
            "Please give a short succinct context to situate this chunk within the overall document "
            "to improve search retrieval of the chunk. Answer only with the succinct context and nothing else."
        )

        url = f"{config.semantic_analysis.api_base.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if config.semantic_analysis.api_key:
            headers["Authorization"] = f"Bearer {config.semantic_analysis.api_key}"

        payload = {
            "model": config.semantic_analysis.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 150,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=20) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        if "choices" in res_json and len(res_json["choices"]) > 0:
                            content = res_json["choices"][0].get("message", {}).get("content", "").strip()
                            return content
                    else:
                        resp_text = await resp.text()
                        logger.debug(f"Contextual retrieval API failed with status {resp.status}: {resp_text}")
        except Exception as e:
            logger.debug(f"Error fetching contextual retrieval prefix: {e}")

        return ""

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
        # Note: get_text might be an async generator or a normal function depending on the implementation
        content_parts = []
        text_result = self.get_text() # type: ignore
        if hasattr(text_result, "__aiter__"):
            async for block in text_result: # type: ignore
                content_parts.append(block)
            full_content = "".join(content_parts)
        else:
            full_content = str(text_result)

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

                # Append enclosing Class info to Method
                if "Method" in labels and node.id in method_to_class:
                    parent_class = method_to_class[node.id]
                    cls_name = parent_class.properties.get("name")
                    cls_fqn = parent_class.properties.get("fqn")
                    if cls_name:
                        context_parts.append(f"{comment_style} Enclosing Class: {cls_fqn or cls_name}")
                        # If intent/summary was available, we could append it here,
                        # but in-memory Orchestrator without Neo4j enrichment won't have LLM intents yet.

                # Contextual Retrieval: Query LLM for context prefix if enabled
                if "Method" in labels:
                    contextual_summary = await self._get_contextual_retrieval_prefix(full_content, code_text)
                    if contextual_summary:
                        for line in contextual_summary.split("\n"):
                            context_parts.append(f"{comment_style} {line}")

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
