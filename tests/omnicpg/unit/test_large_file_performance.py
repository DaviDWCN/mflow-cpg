"""Performance regression tests for large Java files.

These tests generate Java source files with many methods and statements
to verify that the PARENT_OF edge index keeps CFG/DFG construction time
sub-second, even for files that would have taken minutes with O(n²) scans.
"""

from __future__ import annotations

import time

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.java_plugin.cfg_builder import CFGBuilder
from omnicpg.plugins.java_plugin.dfg_builder import DFGBuilder


def _generate_large_java_class(num_methods: int, stmts_per_method: int) -> str:
    """Generate a Java class with *num_methods*, each containing *stmts_per_method*."""
    lines: list[str] = ["public class LargeClass {"]
    for m in range(num_methods):
        lines.append(f"    public void method{m}() {{")
        for s in range(stmts_per_method):
            lines.append(f"        int v{s} = {s};")
        lines.append(f"        int result{m} = v0;")
        lines.append("        return;")
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


class TestLargeJavaFilePerformance:
    """Performance regression tests for large Java files."""

    @pytest.fixture()
    def builders(self) -> tuple[ASTBuilder, CFGBuilder, DFGBuilder]:
        """Return a fresh (ASTBuilder, CFGBuilder, DFGBuilder) triple."""
        return ASTBuilder(), CFGBuilder(), DFGBuilder()

    def test_cfg_indexed_fast_for_many_methods(
        self,
        builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder],
    ) -> None:
        """CFG with PARENT_OF index handles 50 methods x 50 statements quickly.

        Generates ~5000+ AST nodes.  Without the index, the O(n^2) fallback
        would take several seconds.  With the index it should be sub-second.
        """
        source = _generate_large_java_class(num_methods=50, stmts_per_method=50)
        ast_b, cfg_b, _ = builders
        nodes, edges = ast_b.build("LargeClass.java", source)
        assert len(nodes) > 5_000, f"Expected >5000 nodes, got {len(nodes)}"

        start = time.monotonic()
        cfg_edges = cfg_b.build(nodes, edges)
        elapsed = time.monotonic() - start

        assert len(cfg_edges) > 0
        assert all(e.edge_type == EdgeType.FLOWS_TO for e in cfg_edges)
        # Must complete in under 5 seconds (typically <0.5s with the index).
        assert elapsed < 5.0, f"CFG took {elapsed:.2f}s — expected <5s with index"

    def test_dfg_indexed_fast_for_many_methods(
        self,
        builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder],
    ) -> None:
        """DFG with PARENT_OF index handles 50 methods x 50 statements quickly."""
        source = _generate_large_java_class(num_methods=50, stmts_per_method=50)
        ast_b, cfg_b, dfg_b = builders
        nodes, edges = ast_b.build("LargeClass.java", source)

        cfg_edges = cfg_b.build(nodes, edges)

        start = time.monotonic()
        dfg_edges = dfg_b.build(nodes, cfg_edges, edges)
        elapsed = time.monotonic() - start

        assert len(dfg_edges) > 0
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)
        assert elapsed < 5.0, f"DFG took {elapsed:.2f}s — expected <5s with index"

    def test_indexed_vs_fallback_produces_same_cfg(
        self,
        builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder],
    ) -> None:
        """Indexed and fallback paths produce the same number of CFG edges.

        Uses a moderately sized file (10 methods x 10 stmts) to verify that
        the indexed fast-path doesn't lose any edges compared to the fallback.
        """
        source = _generate_large_java_class(num_methods=10, stmts_per_method=10)
        ast_b, _, _ = builders
        nodes, edges = ast_b.build("Medium.java", source)

        cfg_indexed = CFGBuilder()
        cfg_fallback = CFGBuilder()

        indexed_edges = cfg_indexed.build(nodes, edges)
        fallback_edges = cfg_fallback.build(nodes, None)

        # Both paths should produce the same set of edges.
        assert len(indexed_edges) == len(fallback_edges), (
            f"Indexed produced {len(indexed_edges)} edges, "
            f"fallback produced {len(fallback_edges)} edges"
        )

    def test_indexed_vs_fallback_produces_same_dfg(
        self,
        builders: tuple[ASTBuilder, CFGBuilder, DFGBuilder],
    ) -> None:
        """The indexed reaching-definitions path is precise and complete.

        Each generated method only uses ``v0`` (in ``result = v0;``), so the
        true data flow is exactly one ``v0`` definition → use per method.  The
        V2 indexed path performs reaching-definitions analysis (requiring the
        ``PARENT_OF`` edges) and yields exactly these precise edges, whereas the
        legacy fallback (no AST edges) over-approximates with last-def-before-use
        heuristics.  The indexed path must therefore be non-empty and never
        produce *more* edges than the fallback.
        """
        num_methods = 10
        source = _generate_large_java_class(num_methods=num_methods, stmts_per_method=10)
        ast_b, cfg_b, _ = builders
        nodes, edges = ast_b.build("Medium.java", source)
        cfg_edges = cfg_b.build(nodes, edges)

        dfg_indexed = DFGBuilder()
        dfg_fallback = DFGBuilder()

        indexed_edges = dfg_indexed.build(nodes, cfg_edges, edges)
        fallback_edges = dfg_fallback.build(nodes, cfg_edges, None)

        # Precise path: exactly one v0 def→use edge per method.
        v0_edges = [e for e in indexed_edges if e.properties.get("variable") == "v0"]
        assert len(v0_edges) == num_methods
        # Precision must not exceed the over-approximating legacy fallback.
        assert 0 < len(indexed_edges) <= len(fallback_edges)
