"""InterProceduralDFGBuilder — binds Java data-flow across function boundaries.

Connects:
1. Argument nodes (at call site) → Parameter nodes (at method definition).
2. Return nodes (inside method) → Call site nodes (at caller).
3. Field writes (``this.f = ...``) → Field reads (``... = this.f``) of the same
   field within the same class (field-sensitive object propagation), bridging
   the common setter→getter taint pattern across method boundaries.

This builder relies on CALLS edges having been established by the CallGraphBuilder.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING

from omnicpg.models.edge import CPGEdge, EdgeType

if TYPE_CHECKING:
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)


class InterProceduralDFGBuilder:
    """Builds inter-procedural REACHES edges for Java applications.

    Expects CALLS edges to originate from CallSite nodes (method_invocation,
    object_creation_expression).
    """

    def __init__(self) -> None:
        """Initialise builder state."""
        self._node_map: dict[str, CPGNode] = {}
        self._parent_children: dict[str, list[str]] = {}
        self._child_parent: dict[str, str] = {}

    def build(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge],
    ) -> list[CPGEdge]:
        """Generate inter-procedural REACHES edges.

        Args:
            all_nodes: Complete set of nodes (cross-file).
            all_edges: Complete set of edges (Parent-off, Calls, etc.).

        Returns:
            A list of new inter-procedural REACHES edges.
        """
        self._node_map = {n.id: n for n in all_nodes}
        self._parent_children = {}
        self._child_parent = {}
        calls_edges: list[CPGEdge] = []

        for edge in all_edges:
            if edge.edge_type == EdgeType.PARENT_OF:
                self._parent_children.setdefault(edge.source_id, []).append(edge.target_id)
                self._child_parent[edge.target_id] = edge.source_id
            elif edge.edge_type == EdgeType.CALLS:
                calls_edges.append(edge)

        new_edges: list[CPGEdge] = []

        for calls in calls_edges:
            caller_id = calls.source_id  # caller Method node (V2) or CallSite (legacy)
            callee_id = calls.target_id  # callee Method node

            # Prefer the explicit ``callsite_id`` recorded by the V2 call-graph
            # builder; fall back to ``source_id`` for legacy CALLS edges where
            # the source was the call-site itself.
            callsite_id = calls.properties.get("callsite_id")
            call_node = None
            if callsite_id:
                call_node = self._node_map.get(str(callsite_id))
            if call_node is None:
                call_node = self._node_map.get(caller_id)
            method_node = self._node_map.get(callee_id)

            if not call_node or not method_node:
                continue

            # 1. Bind Arguments to Parameters
            arg_to_param = self._bind_arguments_to_parameters(call_node, method_node)
            new_edges.extend(arg_to_param)

            # 2. Bind Returns to Call Site
            return_to_call = self._bind_returns_to_callsite(call_node, method_node)
            new_edges.extend(return_to_call)

        # 3. Bridge field writes to field reads within the same class
        new_edges.extend(self._bind_field_writes_to_reads())

        # 4. Collection Taint Infection (Coarse-grained)
        new_edges.extend(self._bind_collection_taint_infection())

        logger.info("Java Inter-procedural DFG: generated %d REACHES edges", len(new_edges))
        return new_edges

    def _bind_arguments_to_parameters(
        self, call_node: CPGNode, method_node: CPGNode
    ) -> list[CPGEdge]:
        """Connect argument nodes at call site to parameter nodes at method def."""
        edges: list[CPGEdge] = []

        # Find Parameters (formal_parameter nodes)
        parameters: list[CPGNode] = []
        method_child_ids = self._parent_children.get(method_node.id, [])
        for mid in method_child_ids:
            child = self._node_map.get(mid)
            if child and child.properties.get("type") == "formal_parameters":
                param_ids = self._parent_children.get(child.id, [])
                for pid in param_ids:
                    pnode = self._node_map.get(pid)
                    if pnode and "Parameter" in pnode.labels:
                        parameters.append(pnode)
                break

        if not parameters:
            # Fallback for alternative AST structures
            for mid in method_child_ids:
                pnode = self._node_map.get(mid)
                if pnode and "Parameter" in pnode.labels:
                    parameters.append(pnode)

        # Find Arguments (children of argument_list container)
        arguments: list[CPGNode] = []
        call_child_ids = self._parent_children.get(call_node.id, [])
        for cid in call_child_ids:
            child = self._node_map.get(cid)
            if child and child.properties.get("type") == "argument_list":
                arg_ids = self._parent_children.get(child.id, [])
                for aid in arg_ids:
                    anode = self._node_map.get(aid)
                    # Skip punctuation/anonymous nodes
                    if anode and anode.properties.get("type") not in {"(", ")", ","}:
                        arguments.append(anode)
                break

        # Positional binding
        for i, arg in enumerate(arguments):
            if i < len(parameters):
                param = parameters[i]
                edges.append(
                    CPGEdge(
                        source_id=arg.id,
                        target_id=param.id,
                        edge_type=EdgeType.REACHES,
                        properties=MappingProxyType(
                            {
                                "variable": param.properties.get("name", f"p{i}"),
                                "interprocedural": "argument",
                                "index": str(i),
                            }
                        ),
                    )
                )

        return edges

    def _bind_returns_to_callsite(self, call_node: CPGNode, method_node: CPGNode) -> list[CPGEdge]:
        """Connect return nodes inside method to the call site node."""
        edges: list[CPGEdge] = []

        returns: list[CPGNode] = []
        visited: set[str] = set()
        stack = list(self._parent_children.get(method_node.id, []))
        while stack:
            curr_id = stack.pop()
            if curr_id in visited:
                continue
            visited.add(curr_id)
            curr_node = self._node_map.get(curr_id)
            if curr_node:
                if "Return" in curr_node.labels:
                    returns.append(curr_node)
                # Don't descend into nested declarations
                if not ("Class" in curr_node.labels or "Method" in curr_node.labels):
                    stack.extend(self._parent_children.get(curr_id, []))

        for ret in returns:
            edges.append(
                CPGEdge(
                    source_id=ret.id,
                    target_id=call_node.id,
                    edge_type=EdgeType.REACHES,
                    properties=MappingProxyType(
                        {
                            "interprocedural": "return",
                        }
                    ),
                )
            )

        return edges

    def _is_write_target(self, node_id: str) -> bool:
        """True if a ``field_access`` is the LHS (write target) of an assignment."""
        parent_id = self._child_parent.get(node_id)
        if parent_id is None:
            return False
        parent = self._node_map.get(parent_id)
        if parent is None or parent.properties.get("type") != "assignment_expression":
            return False
        siblings = self._parent_children.get(parent_id, [])
        return bool(siblings) and siblings[0] == node_id

    def _enclosing_class_id(self, node_id: str) -> str | None:
        """Climb PARENT_OF links to find the nearest enclosing class node."""
        curr = self._child_parent.get(node_id)
        while curr is not None:
            cnode = self._node_map.get(curr)
            if cnode is not None and "Class" in cnode.labels:
                return curr
            curr = self._child_parent.get(curr)
        return None

    @staticmethod
    def _field_name_from_access(code: str) -> str | None:
        """Extract the accessed field name from a ``field_access`` code string."""
        text = (code or "").strip()
        if not text:
            return None
        # Strip trailing call/index syntax and take the final dotted segment.
        segment = text.split(".")[-1].strip()
        # Guard against array/method noise such as ``data()`` or ``data[0]``.
        for stop in ("(", "[", " "):
            segment = segment.split(stop)[0]
        return segment or None

    def _bind_field_writes_to_reads(self) -> list[CPGEdge]:
        """Connect field writes to field reads of the same field within a class.

        Models object-field taint propagation (e.g. ``obj.set(x)`` followed by
        ``obj.get()``) at the class level. Field-sensitive but not
        instance-sensitive: every write of ``C.f`` is assumed to reach every
        read of ``C.f``. This is a standard sound over-approximation that
        bridges setter→getter flows the intra-procedural DFG cannot see.
        """
        writes: dict[tuple[str, str], list[str]] = {}
        reads: dict[tuple[str, str], list[str]] = {}

        for node in self._node_map.values():
            ntype = node.properties.get("type")
            if ntype == "assignment_expression" and node.properties.get("assign_kind") == "field":
                field = node.properties.get("assign_target")
                class_id = self._enclosing_class_id(node.id)
                if field and class_id:
                    writes.setdefault((class_id, str(field)), []).append(node.id)
            elif ntype == "field_access":
                if self._is_write_target(node.id):
                    continue
                field = self._field_name_from_access(node.properties.get("code", ""))
                class_id = self._enclosing_class_id(node.id)
                if field and class_id:
                    reads.setdefault((class_id, field), []).append(node.id)

        edges: list[CPGEdge] = []
        for key, write_ids in writes.items():
            read_ids = reads.get(key)
            if not read_ids:
                continue
            _, field_name = key
            for write_id in write_ids:
                for read_id in read_ids:
                    edges.append(
                        CPGEdge(
                            source_id=write_id,
                            target_id=read_id,
                            edge_type=EdgeType.REACHES,
                            properties=MappingProxyType(
                                {
                                    "variable": field_name,
                                    "interprocedural": "field",
                                }
                            ),
                        )
                    )
        return edges

    def _find_receiver_node(self, call_node: CPGNode, receiver_name: str) -> CPGNode | None:
        """Find the child node of call_node representing the receiver variable."""
        child_ids = self._parent_children.get(call_node.id, [])
        for cid in child_ids:
            child = self._node_map.get(cid)
            if child:
                code = str(child.properties.get("code", "")).strip()
                if code == receiver_name:
                    return child
                # Fallback: check descendants
                visited = set()
                descendants = [child]
                while descendants:
                    curr = descendants.pop()
                    if curr.id in visited:
                        continue
                    visited.add(curr.id)
                    curr_code = str(curr.properties.get("code", "")).strip()
                    if curr_code == receiver_name and curr.properties.get("type") in {"identifier", "field_access"}:
                        return curr
                    desc_ids = self._parent_children.get(curr.id, [])
                    for did in desc_ids:
                        dnode = self._node_map.get(did)
                        if dnode:
                            descendants.append(dnode)
        return None

    def _bind_collection_taint_infection(self) -> list[CPGEdge]:
        """Connect write arguments to receiver collection and receiver collection to read callsites."""
        edges: list[CPGEdge] = []
        
        # Collection write methods
        write_methods = {"add", "put", "addAll", "putAll", "push", "insert", "addElement"}
        # Collection read methods
        read_methods = {"get", "remove", "pop", "peek", "elementAt"}

        for node in self._node_map.values():
            ntype = node.properties.get("type")
            if ntype != "method_invocation":
                continue
            
            # Extract method name and receiver
            method_name = node.properties.get("name")
            receiver = node.properties.get("receiver")
            if not method_name or not receiver:
                continue
            
            method_name_str = str(method_name)
            receiver_str = str(receiver).strip()
            
            if method_name_str in write_methods:
                # 1. Collection Write: source -> collection
                receiver_node = self._find_receiver_node(node, receiver_str)
                if not receiver_node:
                    continue
                
                # Retrieve arguments of this method invocation
                arguments: list[CPGNode] = []
                call_child_ids = self._parent_children.get(node.id, [])
                for cid in call_child_ids:
                    child = self._node_map.get(cid)
                    if child and child.properties.get("type") == "argument_list":
                        arg_ids = self._parent_children.get(child.id, [])
                        for aid in arg_ids:
                            anode = self._node_map.get(aid)
                            if anode and anode.properties.get("type") not in {"(", ")", ","}:
                                arguments.append(anode)
                        break
                
                # Emit REACHES edges from all arguments to receiver node
                for arg in arguments:
                    edges.append(
                        CPGEdge(
                            source_id=arg.id,
                            target_id=receiver_node.id,
                            edge_type=EdgeType.REACHES,
                            properties=MappingProxyType(
                                {
                                    "variable": receiver_str,
                                    "interprocedural": "collection_write",
                                }
                            ),
                        )
                    )
                    
            elif method_name_str in read_methods:
                # 2. Collection Read: collection -> method_invocation (which flows to LHS)
                receiver_node = self._find_receiver_node(node, receiver_str)
                if not receiver_node:
                    continue
                
                edges.append(
                    CPGEdge(
                        source_id=receiver_node.id,
                        target_id=node.id,
                        edge_type=EdgeType.REACHES,
                        properties=MappingProxyType(
                            {
                                "variable": receiver_str,
                                "interprocedural": "collection_read",
                            }
                        ),
                    )
                )

        return edges
