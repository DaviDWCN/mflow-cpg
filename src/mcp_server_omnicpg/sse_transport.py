from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

import mcp.types as types
from mcp.server.models import InitializationOptions
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp_server_omnicpg.config import Config
from mcp_server_omnicpg.mcp_server import TOOL_COUNT, adapter, app

logger = logging.getLogger(__name__)

sse = SseServerTransport("/messages")

_INIT_OPTIONS = InitializationOptions(
    server_name="omnicpg-mcp-server",
    server_version="2.0.0",
    capabilities=types.ServerCapabilities(
        tools=types.ToolsCapability(),
    ),
)


@asynccontextmanager
async def lifespan(app_starlette: Starlette) -> AsyncIterator[None]:
    """Manage Neo4j connection lifecycle for the Starlette app."""
    logger.info("Starting OmniCPG SSE Server...")
    Config.validate()
    try:
        adapter.connect()
        logger.info("Neo4j connection established")
    except Exception:
        logger.warning("Neo4j connection failed at startup — queries will retry on first request")
    yield
    logger.info("Shutting down OmniCPG SSE Server...")
    adapter.disconnect()


async def handle_sse(request: Request) -> None:
    """Handle incoming SSE connections from MCP clients.

    Starlette stores the ASGI send callable as ``request._send``.  We access
    it here to pass the raw ASGI primitive to the MCP transport layer, which
    requires the underlying callable rather than the higher-level Request
    object.
    """
    async with sse.connect_sse(
        request.scope,
        request.receive,
        request._send,
    ) as (read_stream, write_stream):
        await app.run(read_stream, write_stream, _INIT_OPTIONS)


async def handle_health(request: Request) -> JSONResponse:
    """Return server health status.

    Response schema::

        {"status": "ok", "neo4j": <bool>, "has_data": <bool>, "tools": <int>}
    """
    neo4j_ok: bool = adapter.is_connected()
    has_data = False
    if neo4j_ok:
        try:
            rows = adapter.query("MATCH (n:Node) RETURN count(n) AS c LIMIT 1")
            has_data = bool(rows and rows[0].get("c", 0) > 0)
        except Exception:
            pass
    return JSONResponse(
        {"status": "ok", "neo4j": neo4j_ok, "has_data": has_data, "tools": TOOL_COUNT},
        status_code=200,
    )


starlette_app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/health", endpoint=handle_health, methods=["GET"]),
        Mount("/messages", app=sse.handle_post_message),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", "X-Requested-With"],
        ),
    ],
    lifespan=lifespan,
)


def start() -> None:
    """Start the uvicorn server for the SSE transport."""
    import uvicorn

    port = int(os.environ.get("MCP_PORT", Config.MCP_PORT))
    host = os.environ.get("MCP_HOST", Config.MCP_HOST)
    logger.info("Starting uvicorn server on %s:%d", host, port)
    uvicorn.run(starlette_app, host=host, port=port)
