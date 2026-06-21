"""Shared utilities for Neo4j adapters."""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnicpg.models.node import CPGNode

_VALID_LABEL_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SEMANTIC_LABEL_MAP: dict[str, str] = {
    "function_definition": "Function",
    "method_declaration": "Function",
    "class_definition": "Class",
    "class_declaration": "Class",
    "module": "Module",
    "call": "CallSite",
    "method_invocation": "CallSite",
    "import_statement": "Import",
    "import_from_statement": "Import",
    "import_declaration": "Import",
    "identifier": "Identifier",
}


def _enrich_labels(node: CPGNode) -> list[str]:
    """Return the node's labels enriched with semantic labels."""
    labels = list(node.labels)
    node_type = node.properties.get("type", "")
    semantic = _SEMANTIC_LABEL_MAP.get(str(node_type))
    if semantic and semantic not in labels:
        labels.append(semantic)
    return labels


def _neo4j_safe_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Convert property values to Neo4j-safe types."""
    return {key: _convert_value(value) for key, value in props.items()}


def _convert_value(value: Any) -> Any:
    """Recursively convert a single value to a Neo4j-safe type."""
    if isinstance(value, tuple):
        return [_convert_value(v) for v in value]
    if isinstance(value, list):
        return [_convert_value(v) for v in value]
    if isinstance(value, MappingProxyType):
        return {k: _convert_value(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {k: _convert_value(v) for k, v in value.items()}
    return value


def _format_progress_bar(current: int, total: int, width: int = 20) -> str:
    """Format a compact progress bar string for batch insertion logs."""
    if total <= 0:
        return "[--------------------] 0/0 (0.0%)"
    ratio = current / total
    filled = min(width, int(ratio * width))
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {current}/{total} ({ratio * 100:.1f}%)"
