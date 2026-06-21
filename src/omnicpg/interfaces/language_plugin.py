"""Abstract base class for language plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnicpg.models.analysis_level import AnalysisLevel
    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode


class LanguagePlugin(ABC):
    """Interface that every language plugin must implement.

    A language plugin is responsible for:
    1. Declaring which file extensions it supports.
    2. Parsing source code into AST nodes and edges.
    3. Deriving control-flow (CFG) edges from the AST.
    4. Deriving data-flow (DFG) edges from the CFG.
    5. Optionally building cross-file call-graph edges.
    """

    @property
    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """Return file extensions handled by this plugin (e.g. ``['.py']``)."""

    @abstractmethod
    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse *source_code* and return AST nodes and edges.

        The depth of the returned graph is controlled by *analysis_level*:

        * ``FULL`` (default) — every AST node becomes a ``CPGNode`` and
          all edges are ``PARENT_OF``.
        * ``ARCHITECTURAL`` — only skeleton nodes (Module, Class, Method,
          Field) are emitted.  Method bodies are stored as a
          ``source_code`` property on the Method node.  Edges use
          ``CONTAINS`` instead of ``PARENT_OF`` for parent→child.
        * ``STRUCTURAL`` — statement-level nodes are kept but
          expression/literal nodes are pruned.

        When *analysis_level* is ``None`` the plugin should default to
        ``AnalysisLevel.FULL`` to preserve backward compatibility.

        Args:
            file_path: Path of the source file (used for metadata).
            source_code: The full text content of the file.
            analysis_level: Desired granularity.  Defaults to ``None``
                (treated as ``FULL``).

        Returns:
            A tuple of ``(nodes, edges)`` representing the AST sub-graph.
        """

    @abstractmethod
    def build_cfg(
        self,
        ast_nodes: list[CPGNode],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive control-flow ``FLOWS_TO`` edges from AST nodes.

        When *ast_edges* (the ``PARENT_OF`` edges from :meth:`parse_to_ast`)
        are provided, the builder can construct a parent→children index for
        **O(1)** child lookups instead of scanning the full node list.  This
        dramatically speeds up large files (100 k+ nodes).

        Args:
            ast_nodes: AST nodes produced by ``parse_to_ast``.
            ast_edges: ``PARENT_OF`` edges produced by ``parse_to_ast``.
                When ``None``, the builder falls back to a slower
                line-range scan.

        Returns:
            A list of ``FLOWS_TO`` edges.
        """

    @abstractmethod
    def build_dfg(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive data-flow ``REACHES`` edges from the CFG.

        When *ast_edges* are provided, scope-children lookups use the
        ``PARENT_OF`` tree index instead of a full-list scan.

        Args:
            cfg_nodes: Nodes involved in control flow.
            cfg_edges: ``FLOWS_TO`` edges produced by ``build_cfg``.
            ast_edges: ``PARENT_OF`` edges produced by ``parse_to_ast``.
                When ``None``, the builder falls back to a slower
                line-range scan.

        Returns:
            A list of ``REACHES`` edges.
        """

    def build_call_graph(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive inter-procedural ``CALLS`` edges across files.

        This is an *optional* capability. The default implementation returns
        an empty list.  Plugins that support cross-file call-graph analysis
        should override this method.

        Args:
            all_nodes: The complete set of AST nodes from **all** analysed
                files — not just one file.
            all_edges: The complete set of edges (including ``PARENT_OF``)
                used to resolve call-site → enclosing method.

        Returns:
            A list of ``CALLS`` edges linking methods to methods.
        """
        return []

    def build_interprocedural_dfg(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Derive inter-procedural ``REACHES`` edges across function boundaries.

        This connects function call arguments to parameters, and function return
        values to call results.  It relies on the presence of ``CALLS`` edges.

        Args:
            all_nodes: The complete set of nodes.
            all_edges: The complete set of edges (including ``CALLS`` and ``PARENT_OF``).

        Returns:
            A list of inter-procedural ``REACHES`` edges.
        """
        return []
