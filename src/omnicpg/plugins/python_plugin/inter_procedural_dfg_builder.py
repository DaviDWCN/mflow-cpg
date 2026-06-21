"""InterProceduralDFGBuilder — binds data-flow across function boundaries."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING

from omnicpg.models.edge import CPGEdge, EdgeType

if TYPE_CHECKING:
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)


class InterProceduralDFGBuilder:
    """Binds call arguments to parameters and return values to call results.

    This builder relies on the presence of CALLS edges to identify link points
    between callers and callees.
    """

    def __init__(self) -> None:
        """Initialize the inter-procedural DFG builder."""
        self._node_index: dict[str, CPGNode] = {}
        self._parent_to_children: dict[str, list[str]] = {}

    def build(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge],
    ) -> list[CPGEdge]:
        """Return inter-procedural REACHES edges."""
        self._node_index = {n.id: n for n in all_nodes}
        self._parent_to_children = {}

        # Index parent-child relationships for fast traversal.
        for e in all_edges:
            if e.edge_type == EdgeType.PARENT_OF:
                self._parent_to_children.setdefault(e.source_id, []).append(e.target_id)

        new_edges: list[CPGEdge] = []

        # 1. Map Method IDs to their parameters.
        method_params: dict[str, list[CPGNode]] = {}
        for node in all_nodes:
            if node.has_label("Method"):
                params = self._find_parameters(node)
                method_params[node.id] = params

        # 2. Iterate over CALLS edges to bind data flow.
        for edge in all_edges:
            if edge.edge_type != EdgeType.CALLS:
                continue

            target_method_id = edge.target_id
            callee_name = edge.properties.get("callee")
            params = method_params.get(target_method_id, [])

            source_node = self._node_index.get(edge.source_id)
            if not source_node:
                continue

            call_nodes = []
            if source_node.properties.get("type") == "call":
                call_nodes = [source_node]
            elif source_node.has_label("Method"):
                call_nodes = self._find_matching_calls(source_node, callee_name)

            for call_node in call_nodes:
                # A. Bind Arguments -> Parameters
                args = self._find_arguments(call_node)

                # In Python instance methods, we skip 'self' for positional binding.
                actual_params = params
                if params and params[0].properties.get("code") == "self":
                    actual_params = params[1:]

                for i, arg in enumerate(args):
                    if i < len(actual_params):
                        param = actual_params[i]
                        new_edges.append(
                            CPGEdge(
                                source_id=arg.id,
                                target_id=param.id,
                                edge_type=EdgeType.REACHES,
                                properties=MappingProxyType(
                                    {"variable": param.properties.get("code", "")}
                                ),
                            )
                        )

                # B. Bind Return Statements -> Call Node (Result)
                returns = self._find_returns(target_method_id)
                for ret in returns:
                    new_edges.append(
                        CPGEdge(
                            source_id=ret.id,
                            target_id=call_node.id,
                            edge_type=EdgeType.REACHES,
                            properties=MappingProxyType({"interprocedural": "return"}),
                        )
                    )

        logger.info("Inter-procedural DFG: generated %d edges", len(new_edges))
        return new_edges

    def _find_parameters(self, method_node: CPGNode) -> list[CPGNode]:
        """Find identifier nodes that are parameters of the method."""
        params_node_id = None
        for cid in self._parent_to_children.get(method_node.id, []):
            child = self._node_index.get(cid)
            if child and child.properties.get("type") == "parameters":
                params_node_id = cid
                break

        if not params_node_id:
            return []

        params = []
        for cid in self._parent_to_children.get(params_node_id, []):
            child = self._node_index.get(cid)
            if not child:
                continue

            if child.properties.get("type") == "identifier":
                params.append(child)
            elif child.properties.get("type") in ("typed_parameter", "default_parameter"):
                for gcid in self._parent_to_children.get(child.id, []):
                    gchild = self._node_index.get(gcid)
                    if gchild and gchild.properties.get("type") == "identifier":
                        params.append(gchild)
                        break
        return params

    def _find_arguments(self, call_node: CPGNode) -> list[CPGNode]:
        """Find nodes that are arguments to the call."""
        arg_list_id = None
        for cid in self._parent_to_children.get(call_node.id, []):
            child = self._node_index.get(cid)
            if child and child.properties.get("type") == "argument_list":
                arg_list_id = cid
                break

        if not arg_list_id:
            return []

        args = []
        for cid in self._parent_to_children.get(arg_list_id, []):
            child = self._node_index.get(cid)
            if child and child.properties.get("type") not in ("(", ")", ","):
                args.append(child)
        return args

    def _find_matching_calls(self, method_node: CPGNode, callee_name: str | None) -> list[CPGNode]:
        """Find 'call' nodes within method_node whose code matches callee_name."""
        if not callee_name:
            return []

        matches = []
        stack = list(self._parent_to_children.get(method_node.id, []))
        while stack:
            curr_id = stack.pop()
            node = self._node_index.get(curr_id)
            if not node:
                continue

            if node.properties.get("type") == "call":
                code = node.properties.get("code", "")
                if callee_name in code:
                    matches.append(node)
                    continue

            stack.extend(self._parent_to_children.get(curr_id, []))
        return matches

    def _find_returns(self, method_id: str) -> list[CPGNode]:
        """Find 'return_statement' nodes within the entire method subtree."""
        returns = []
        stack = list(self._parent_to_children.get(method_id, []))
        visited = {method_id}
        while stack:
            curr_id = stack.pop()
            if curr_id in visited:
                continue
            visited.add(curr_id)

            node = self._node_index.get(curr_id)
            if not node:
                continue

            if node.properties.get("type") == "return_statement":
                returns.append(node)

            stack.extend(self._parent_to_children.get(curr_id, []))
        return returns
