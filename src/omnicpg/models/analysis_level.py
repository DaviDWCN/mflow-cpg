"""AnalysisLevel — controls the granularity of CPG generation."""

from __future__ import annotations

from enum import StrEnum


class AnalysisLevel(StrEnum):
    """Controls how deeply the AST is expanded into CPG nodes.

    Three levels are available:

    * **FULL** — Every AST node becomes a CPG node.  CFG and DFG edges
      are derived for every function.  This is the original behaviour and
      remains the default for backward compatibility.
    * **ARCHITECTURAL** — Only *skeleton* nodes are emitted: ``Module``,
      ``Class`` / ``Interface``, ``Method`` (signature-level), and
      ``Field``.  Method bodies are captured as a ``source_code`` string
      property on the ``Method`` node instead of being expanded into
      individual AST nodes.  CFG / DFG derivation is skipped entirely.
      This dramatically reduces node counts (~95 %) and is ideal for
      large-scale architectural exploration by AI agents.
    * **STRUCTURAL** — An intermediate level that keeps statement-level
      nodes (``if``, ``for``, ``while``, ``return``, etc.) but prunes
      expression-level and literal nodes.  CFG edges are still derived;
      DFG edges are skipped.

    Attributes:
        FULL: Full AST + CFG + DFG (default).
        ARCHITECTURAL: Skeleton only (Module / Class / Method / Field).
        STRUCTURAL: Statement-level (no expressions or literals).
    """

    FULL = "FULL"
    ARCHITECTURAL = "ARCHITECTURAL"
    STRUCTURAL = "STRUCTURAL"
