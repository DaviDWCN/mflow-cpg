"""Java analysis precision-quantification metrics.

Provides a small, dependency-free function that summarises *measurable*
precision indicators for a Java CPG: how many call sites were resolved, what
fraction of ``CALLS`` edges are type-resolved (vs. name-based heuristics),
inter-procedural data-flow coverage, and security-tagging coverage.

These metrics make analysis quality observable and regression-testable, in the
spirit of the precision benchmarks reported by tools such as CodeQL and Joern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnicpg.models.edge import EdgeType

if TYPE_CHECKING:
    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode

# Node types that represent invokable call sites.
_CALL_SITE_TYPES = frozenset(
    {
        "method_invocation",
        "object_creation_expression",
        "method_reference",
    }
)


def _ratio(numerator: int, denominator: int) -> float:
    """Return ``numerator / denominator`` rounded to 4 dp, 0.0 when undefined."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def compute_java_metrics(
    nodes: list[CPGNode],
    edges: list[CPGEdge],
) -> dict[str, float | int]:
    """Compute precision / coverage metrics for a Java CPG.

    Args:
        nodes: All CPG nodes for the analysed Java sources.
        edges: All CPG edges (``CALLS``, ``REACHES``, ``PARENT_OF`` ...).

    Returns:
        A JSON-friendly dict of counts and ratios:

        * ``call_sites`` / ``resolved_call_sites`` / ``call_resolution_rate``
        * ``calls_edges`` / ``typed_calls`` / ``heuristic_calls`` /
          ``typed_call_ratio``
        * ``interprocedural_edges`` and per-kind counts
          (``interproc_argument`` / ``interproc_return`` / ``interproc_field``)
        * ``security_sources`` / ``security_sinks`` / ``security_sanitizers``
        * ``methods`` / ``classes``
    """
    call_site_ids: set[str] = set()
    security_sources = security_sinks = security_sanitizers = 0
    methods = classes = 0

    for node in nodes:
        ntype = node.properties.get("type")
        if ntype in _CALL_SITE_TYPES or "CallSite" in node.labels:
            call_site_ids.add(node.id)
        role = node.properties.get("security_role")
        if role == "source":
            security_sources += 1
        elif role == "sink":
            security_sinks += 1
        elif role == "sanitizer":
            security_sanitizers += 1
        if "Method" in node.labels:
            methods += 1
        if "Class" in node.labels:
            classes += 1

    calls_edges = 0
    typed_calls = 0
    heuristic_calls = 0
    resolved_call_sites: set[str] = set()
    interproc_argument = interproc_return = interproc_field = 0

    for edge in edges:
        if edge.edge_type == EdgeType.CALLS:
            calls_edges += 1
            resolution = edge.properties.get("resolution")
            if resolution == "typed":
                typed_calls += 1
            elif resolution == "heuristic":
                heuristic_calls += 1
            callsite_id = edge.properties.get("callsite_id")
            if callsite_id:
                resolved_call_sites.add(str(callsite_id))
            elif edge.source_id in call_site_ids:
                resolved_call_sites.add(edge.source_id)
        elif edge.edge_type == EdgeType.REACHES:
            kind = edge.properties.get("interprocedural")
            if kind == "argument":
                interproc_argument += 1
            elif kind == "return":
                interproc_return += 1
            elif kind == "field":
                interproc_field += 1

    interprocedural_edges = interproc_argument + interproc_return + interproc_field

    return {
        "call_sites": len(call_site_ids),
        "resolved_call_sites": len(resolved_call_sites),
        "call_resolution_rate": _ratio(len(resolved_call_sites), len(call_site_ids)),
        "calls_edges": calls_edges,
        "typed_calls": typed_calls,
        "heuristic_calls": heuristic_calls,
        "typed_call_ratio": _ratio(typed_calls, calls_edges),
        "interprocedural_edges": interprocedural_edges,
        "interproc_argument": interproc_argument,
        "interproc_return": interproc_return,
        "interproc_field": interproc_field,
        "security_sources": security_sources,
        "security_sinks": security_sinks,
        "security_sanitizers": security_sanitizers,
        "methods": methods,
        "classes": classes,
    }
