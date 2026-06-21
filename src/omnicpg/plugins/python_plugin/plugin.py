"""PythonPlugin — language plugin for analysing Python source files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnicpg.interfaces.language_plugin import LanguagePlugin
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder
from omnicpg.plugins.python_plugin.call_graph_builder import CallGraphBuilder
from omnicpg.plugins.python_plugin.cfg_builder import CFGBuilder
from omnicpg.plugins.python_plugin.dfg_builder import DFGBuilder
from omnicpg.plugins.python_plugin.inter_procedural_dfg_builder import InterProceduralDFGBuilder

if TYPE_CHECKING:
    from omnicpg.models.analysis_level import AnalysisLevel
    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode


class PythonPlugin(LanguagePlugin):
    """Concrete :class:`LanguagePlugin` for Python source files.

    Delegates the heavy lifting to dedicated builder classes:

    * :class:`ASTBuilder` — tree-sitter → ``CPGNode`` / ``PARENT_OF`` edges.
    * :class:`CFGBuilder` — AST → ``FLOWS_TO`` edges.
    * :class:`DFGBuilder` — CFG → ``REACHES`` edges.
    * :class:`CallGraphBuilder` — cross-file ``CALLS`` edges.
    """

    def __init__(self) -> None:
        """Initialise the Python plugin with its builder components."""
        self._ast_builder = ASTBuilder()
        self._cfg_builder = CFGBuilder()
        self._dfg_builder = DFGBuilder()
        self._call_graph_builder = CallGraphBuilder()
        self._inter_dfg_builder = InterProceduralDFGBuilder()

    # ── LanguagePlugin interface ──────────────────────────────────────────

    @property
    def supported_extensions(self) -> list[str]:
        """Return ``['.py']``."""
        return [".py"]

    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse Python *source_code* into AST nodes and edges."""
        from omnicpg.models.analysis_level import AnalysisLevel

        level = analysis_level if analysis_level is not None else AnalysisLevel.FULL
        return self._ast_builder.build(file_path, source_code, analysis_level=level)

    def build_cfg(
        self,
        ast_nodes: list[CPGNode],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive ``FLOWS_TO`` edges from *ast_nodes*."""
        return self._cfg_builder.build(ast_nodes, ast_edges)

    def build_dfg(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive ``REACHES`` edges from the control-flow graph."""
        return self._dfg_builder.build(cfg_nodes, cfg_edges, ast_edges)

    def build_call_graph(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive ``CALLS`` edges across all analysed Python files."""
        return self._call_graph_builder.build(all_nodes, all_edges)

    def build_interprocedural_dfg(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive inter-procedural ``REACHES`` edges across Python function boundaries."""
        return self._inter_dfg_builder.build(all_nodes, all_edges or [])
