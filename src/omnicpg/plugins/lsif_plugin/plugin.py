"""LSIF plugin for parsing Language Server Index Format files."""

from omnicpg.interfaces.language_plugin import LanguagePlugin
from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.models.edge import CPGEdge
from omnicpg.models.node import CPGNode


class LSIFPlugin(LanguagePlugin):
    """Plugin to extract nodes and edges from LSIF graphs.

    This plugin is part of the microservice federated parsing effort (Phase 3),
    allowing various language definitions to be imported from standard LSIF output.
    """

    @property
    def supported_extensions(self) -> list[str]:
        """Return supported file extensions."""
        return [".lsif"]

    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse LSIF data and return nodes and edges.

        Placeholder for Phase 3 integration.
        """
        return [], []

    def build_cfg(
        self,
        ast_nodes: list[CPGNode],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """LSIF files typically describe definitions and references, not raw control flow."""
        return []

    def build_dfg(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """LSIF files themselves don't provide function-level reaching definitions."""
        return []

    def build_call_graph(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """LSIF files might provide cross-file references, but logic needs to be added later."""
        return []
