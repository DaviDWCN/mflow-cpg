"""OpenAPI language plugin for parsing API specifications."""

import json
from typing import Any

from omnicpg.interfaces.language_plugin import LanguagePlugin
from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.models.edge import CPGEdge
from omnicpg.models.node import CPGNode


class OpenAPIPlugin(LanguagePlugin):
    """Plugin to extract APIEndpoint nodes from OpenAPI/Swagger definitions.

    This plugin is part of the microservice federated parsing effort, allowing
    cross-project CALLS edges to be connected via API boundaries.
    """

    @property
    def supported_extensions(self) -> list[str]:
        """Return supported file extensions."""
        return [".json", ".yaml", ".yml"]

    def parse_to_ast(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel | None = None,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse OpenAPI spec and return File and APIEndpoint nodes.

        Currently supports JSON format. YAML support requires additional dependencies
        like pyyaml.
        """
        nodes: list[CPGNode] = []
        edges: list[CPGEdge] = []

        # We only process if it is a JSON file for this scaffolding iteration.
        # In a full implementation, PyYAML would be used.
        if not file_path.endswith(".json"):
            return [], []

        try:
            spec: dict[str, Any] = json.loads(source_code)
        except json.JSONDecodeError:
            return [], []

        # Very basic heuristic to confirm it's an OpenAPI or Swagger document
        if "openapi" not in spec and "swagger" not in spec:
            return [], []

        from omnicpg.models.edge import EdgeType
        from omnicpg.utils.id_gen import generate_deterministic_id

        file_node_id = generate_deterministic_id(
            file_path=file_path,
            node_type="module",
            name=file_path,
            line_start=1,
            col_start=0,
        )
        from types import MappingProxyType

        file_node = CPGNode(
            id=file_node_id,
            labels=("Node", "File"),
            properties=MappingProxyType(
                {
                    "type": "module",
                    "name": file_path,
                    "file_path": file_path,
                    "line_start": 1,
                    "line_end": len(source_code.splitlines()),
                    "col_start": 0,
                    "col_end": 0,
                }
            ),
        )

        nodes.append(file_node)

        paths = spec.get("paths", {})
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            for method, operation in path_item.items():
                # typical methods: get, post, put, delete, patch, options, head
                if method.lower() not in {
                    "get",
                    "post",
                    "put",
                    "delete",
                    "patch",
                    "options",
                    "head",
                }:
                    continue

                op_dict = operation if isinstance(operation, dict) else {}
                op_id = op_dict.get("operationId", f"{method.upper()} {path}")

                endpoint_node_id = generate_deterministic_id(
                    file_path=file_path,
                    node_type="api_endpoint",
                    name=op_id,
                    line_start=1,
                    col_start=0,
                )

                from types import MappingProxyType

                endpoint_node = CPGNode(
                    id=endpoint_node_id,
                    labels=("Node", "APIEndpoint"),
                    properties=MappingProxyType(
                        {
                            "http_method": method.upper(),
                            "route": path,
                            "type": "api_endpoint",
                            "name": op_id,
                            "file_path": file_path,
                            "line_start": 1,
                            "line_end": 1,
                            "col_start": 0,
                            "col_end": 0,
                            "code": path,
                        }
                    ),
                )

                nodes.append(endpoint_node)

                # Link File to APIEndpoint
                edge = CPGEdge(
                    source_id=file_node.id,
                    target_id=endpoint_node.id,
                    edge_type=EdgeType.PARENT_OF,
                )
                edges.append(edge)

        return nodes, edges

    def build_cfg(
        self,
        ast_nodes: list[CPGNode],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """OpenAPI files have no control flow."""
        return []

    def build_dfg(
        self,
        cfg_nodes: list[CPGNode],
        cfg_edges: list[CPGEdge],
        ast_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """OpenAPI files have no data flow."""
        return []

    def build_call_graph(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """OpenAPI files themselves don't call anything."""
        return []
