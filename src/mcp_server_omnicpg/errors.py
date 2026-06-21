"""Unified error codes for MCP tool responses."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Machine-readable error codes for MCP tool responses."""

    INVALID_PARAM = "INVALID_PARAM"
    APOC_REQUIRED = "APOC_REQUIRED"
    NOT_CONNECTED = "NOT_CONNECTED"
    QUERY_FAILED = "QUERY_FAILED"
    NOT_FOUND = "NOT_FOUND"
    SAFETY_REJECTED = "SAFETY_REJECTED"


def tool_error(message: str, code: ErrorCode, details: str | None = None) -> dict[str, str]:
    """Create a structured error response dict."""
    result = {"error": message, "error_code": str(code)}
    if details is not None:
        result["details"] = details
    return result
