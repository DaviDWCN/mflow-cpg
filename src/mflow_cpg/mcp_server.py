"""
Unified MCP Server for M-Flow × OmniCPG.
Proxies OmniCPG tools and registers M-Flow tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.sse import SseServerTransport
from mcp.types import (
    ServerCapabilities,
    TextContent,
    Tool,
    ToolsCapability,
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UnifiedMCPServer")

# Create unified server
app = Server("mflow-cpg-mcp-server")

# Try to import omnicpg MCP server
omni_mcp: Any = None
try:
    import mcp_server_omnicpg.mcp_server as _omni_mcp
    omni_mcp = _omni_mcp
except ImportError as e:
    logger.warning(f"Failed to import mcp_server_omnicpg: {e}. CPG tools will be unavailable.")

# Register GraphRAG retrievers
try:
    import mflow_cpg.graph_rag
except ImportError as e:
    logger.warning(f"Failed to import graph_rag module: {e}")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List all unified tools."""
    omni_tools: list[Tool] = []
    if omni_mcp is not None:
        try:
            omni_tools = await omni_mcp.list_tools()
        except Exception as e:
            logger.error(f"Error fetching OmniCPG tools: {e}")

    mflow_tools = [
        Tool(
            name="mflow_search",
            description="Semantic search across M-Flow memory (including business context and code graph).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The natural language query or code symbol to search for."},
                    "recall_mode": {
                        "type": "string",
                        "description": "Recall strategy: CODE_GRAPH (default), GRAPH_RAG, EPISODIC, PROCEDURAL, TRIPLET_COMPLETION, CHUNKS_LEXICAL.",
                        "default": "CODE_GRAPH"
                    },
                    "top_k": {"type": "integer", "description": "Number of results to retrieve.", "default": 5}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="mflow_add",
            description="Add document/text to M-Flow's memory space.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The raw text or document content to add."},
                    "name": {"type": "string", "description": "Descriptive name/title for the document."},
                    "dataset_name": {"type": "string", "description": "Target dataset name.", "default": "main_dataset"},
                    "metadata": {"type": "object", "description": "Optional metadata key-value pairs."}
                },
                "required": ["text", "name"]
            }
        ),
        Tool(
            name="concept_to_code_link",
            description="Establish bidirectional links in Neo4j between M-Flow business concepts and OmniCPG code structures.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The project isolation ID."}
                },
                "required": ["project_id"]
            }
        ),
        Tool(
            name="mflow_graph_rag_enrich",
            description="Run community detection and generate LLM community summaries for GraphRAG.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The project isolation ID."}
                },
                "required": ["project_id"]
            }
        )
    ]

    return omni_tools + mflow_tools


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute a tool by name."""
    if name == "mflow_search":
        query_text = arguments.get("query", "")
        mode_str = arguments.get("recall_mode", "CODE_GRAPH").upper()
        top_k = arguments.get("top_k", 5)

        from m_flow.search.types import RecallMode
        from m_flow.search.methods.no_access_control_search import no_access_control_search

        try:
            recall_mode = RecallMode(mode_str)
        except ValueError:
            return [TextContent(type="text", text=f"Error: Invalid recall mode '{mode_str}'.")]

        try:
            comp, context, datasets = await no_access_control_search(
                query_type=recall_mode,
                query_text=query_text,
                top_k=top_k,
                only_context=True
            )
            return [TextContent(type="text", text=str(context))]
        except Exception as e:
            logger.error(f"Search failed: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Search failed: {e}")]

    elif name == "mflow_add":
        text = arguments.get("text", "")
        doc_name = arguments.get("name", "")
        dataset_name = arguments.get("dataset_name", "main_dataset")

        from m_flow.api.v1.add.add import add as mflow_add
        from m_flow.api.v1.memorize.memorize import memorize as mflow_memorize
        from mflow_cpg.chunker import SyntaxAwareCodeChunker

        try:
            # 1. Add document text to ingestion
            await mflow_add(text, dataset_name=dataset_name, dataset_id=None)
            
            # 2. Run memorization using SyntaxAwareCodeChunker
            await mflow_memorize(
                datasets=[dataset_name],
                chunker=SyntaxAwareCodeChunker
            )
            return [TextContent(type="text", text=f"Successfully added and memorized document '{doc_name}' using syntax chunker.")]
        except Exception as e:
            logger.error(f"Failed to add document: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Failed to add document: {e}")]

    elif name == "concept_to_code_link":
        project_id = arguments.get("project_id", "")
        from m_flow.adapters.graph import get_graph_provider
        from mflow_cpg.linker import ConceptToCodeLinker

        try:
            db = await get_graph_provider()
            linker = ConceptToCodeLinker(db)
            res = await linker.link_concepts_and_code(project_id)
            return [TextContent(type="text", text=json.dumps(res, indent=2))]
        except Exception as e:
            logger.error(f"Linking failed: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Linking failed: {e}")]

    elif name == "mflow_graph_rag_enrich":
        project_id = arguments.get("project_id", "")
        from m_flow.adapters.graph import get_graph_provider
        from mflow_cpg.graph_rag import GraphRAGManager

        try:
            db = await get_graph_provider()
            manager = GraphRAGManager(db)
            comms_count = manager.detect_communities(project_id)
            summaries_result = manager.generate_community_summaries(project_id)
            res = {
                "communities_detected": comms_count,
                "summaries_generated": summaries_result.get("communities_summarized", 0)
            }
            return [TextContent(type="text", text=json.dumps(res, indent=2))]
        except Exception as e:
            logger.error(f"GraphRAG enrichment failed: {e}", exc_info=True)
            return [TextContent(type="text", text=f"GraphRAG enrichment failed: {e}")]

    # Fallback to OmniCPG tools
    if omni_mcp is not None:
        try:
            return await omni_mcp.call_tool(name, arguments)
        except Exception as e:
            logger.error(f"OmniCPG tool call {name} failed: {e}")
            return [TextContent(type="text", text=f"OmniCPG tool execution failed: {e}")]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="M-Flow × OmniCPG Unified MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="Transport protocol")
    parser.add_argument("--port", type=int, default=8080, help="SSE port (for SSE transport)")
    args = parser.parse_args()

    if args.transport == "stdio":
        from mcp.server.stdio import stdio_server
        async def run_stdio():
            async with stdio_server() as (read_stream, write_stream):
                await app.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="mflow-cpg-mcp-server",
                        server_version="0.1.0",
                        capabilities=ServerCapabilities(
                            tools=ToolsCapability()
                        )
                    )
                )
        try:
            asyncio.run(run_stdio())
        except KeyboardInterrupt:
            logger.info("Server stopped.")
    else:
        from starlette.applications import Starlette
        from starlette.routing import Route
        import uvicorn
        
        sse = SseServerTransport("/sse")
        starlette_app = Starlette(
            routes=[
                Route("/sse", endpoint=sse.handle_sse_request),
                Route("/messages", endpoint=sse.handle_post_message, methods=["POST"])
            ]
        )

        async def run_sse():
            await app.run(
                sse.read_stream,
                sse.write_stream,
                InitializationOptions(
                    server_name="mflow-cpg-mcp-server",
                    server_version="0.1.0",
                    capabilities=ServerCapabilities(
                        tools=ToolsCapability()
                    )
                )
            )

        @starlette_app.on_event("startup")
        def startup():
            asyncio.create_task(run_sse())

        logger.info(f"Starting SSE MCP Server on port {args.port}...")
        uvicorn.run(starlette_app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
