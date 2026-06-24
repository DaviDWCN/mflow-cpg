"""Kuzu adapter for M-Flow."""

import json
from typing import Any, Dict, List, Optional, Tuple, Type, Union
from uuid import UUID

import kuzu

from m_flow.adapters.graph.graph_db_interface import (
    EdgeTriple,
    EdgeTuple,
    GraphProvider,
    NodeProps,
    NodeTuple,
)
from m_flow.core import MemoryNode
from m_flow.shared.logging_utils import get_logger

_log = get_logger("KuzuDB")


def _merge_node_props(node: Union[MemoryNode, str], props: Optional[NodeProps]) -> NodeProps:
    """Helper to unify node properties."""
    base_props = {}
    if hasattr(node, "model_dump"):
        base_props = node.model_dump()
    elif hasattr(node, "__dict__"):
        base_props = vars(node)

    # If it is just a string, it must be node ID
    if isinstance(node, str):
        base_props["id"] = node

    if props:
        base_props.update(props)

    # Ensure all dicts/lists are json serialized (Kuzu requires primitive types or specific complex types)
    for k, v in base_props.items():
        if isinstance(v, (dict, list)):
            base_props[k] = json.dumps(v)
        elif isinstance(v, UUID):
            base_props[k] = str(v)

    return base_props


class KuzuAdapter(GraphProvider):
    """
    Kuzu local graph database adapter.
    """

    def __init__(self, db_path: str = "./databases/kuzu_db"):
        self.db_path = db_path
        self.db = None
        self.conn = None

    async def initialize(self) -> None:
        """Initialize Kuzu database and connection."""
        self.db = kuzu.Database(self.db_path)
        self.conn = kuzu.Connection(self.db)
        _log.info(f"Initialized Kuzu database at {self.db_path}")

        # Set up schema if it doesn't exist
        await self._ensure_schema()

    async def _ensure_schema(self) -> None:
        """Create basic Node and Edge tables if they don't exist."""
        # Check existing tables
        result = self.conn.execute("CALL show_tables() RETURN *;")
        tables = [row["name"] for row in result.rows_as_dict()] if result.has_next() else []

        # Simplified schema for generic graph operations
        if "Node" not in tables:
            self.conn.execute(
                "CREATE NODE TABLE Node (id STRING, type STRING, name STRING, properties STRING, PRIMARY KEY (id))"
            )

        if "Edge" not in tables:
            self.conn.execute(
                "CREATE REL TABLE Edge (FROM Node TO Node, label STRING, properties STRING)"
            )

    async def query(self, cypher: str, params: Dict[str, Any]) -> List[Any]:
        """Execute a raw openCypher query."""
        if not self.conn:
            raise RuntimeError("Database not initialized")

        result = self.conn.execute(cypher, params)
        out = []
        while result.has_next():
            out.append(result.get_next())
        return out

    async def is_empty(self) -> bool:
        """Return True if the graph has no nodes."""
        result = self.conn.execute("MATCH (n:Node) RETURN count(n) AS cnt")
        row = result.get_next()
        return row[0] == 0

    async def add_node(
        self,
        node: Union["MemoryNode", str],
        props: Optional[NodeProps] = None,
    ) -> None:
        merged_props = _merge_node_props(node, props)
        node_id = str(merged_props.get("id"))
        node_type = str(merged_props.get("type", "Node"))
        name = str(merged_props.get("name", node_id))

        # Keep non-core properties as JSON string in 'properties'
        core_keys = {"id", "type", "name"}
        extra_props = {k: v for k, v in merged_props.items() if k not in core_keys}
        props_str = json.dumps(extra_props)

        query = """
            MERGE (n:Node {id: $id})
            ON MATCH SET n.type = $type, n.name = $name, n.properties = $props
            ON CREATE SET n.type = $type, n.name = $name, n.properties = $props
        """
        self.conn.execute(query, {"id": node_id, "type": node_type, "name": name, "props": props_str})

    async def add_nodes(self, nodes: List[Any]) -> None:
        for node in nodes:
            await self.add_node(node)

    async def has_node(self, node_id: str) -> bool:
        result = self.conn.execute("MATCH (n:Node {id: $id}) RETURN count(n) AS cnt", {"id": str(node_id)})
        return result.get_next()[0] > 0

    async def get_node(self, node_id: str) -> Optional[NodeProps]:
        result = self.conn.execute("MATCH (n:Node {id: $id}) RETURN n", {"id": str(node_id)})
        if not result.has_next():
            return None

        row = result.get_next()[0]
        # Reconstruct full dict
        out = {"id": row["id"], "type": row["type"], "name": row["name"]}
        if row.get("properties"):
            try:
                extra = json.loads(row["properties"])
                out.update(extra)
            except Exception:
                pass
        return out

    async def get_nodes(self, ids: List[str]) -> List[NodeProps]:
        nodes = []
        for nid in ids:
            n = await self.get_node(nid)
            if n:
                nodes.append(n)
        return nodes

    async def delete_node(self, node_id: str) -> None:
        self.conn.execute("MATCH (n:Node {id: $id}) DETACH DELETE n", {"id": str(node_id)})

    async def delete_nodes(self, ids: List[str]) -> None:
        for nid in ids:
            await self.delete_node(nid)

    async def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        props: Optional[Dict[str, Any]] = None,
    ) -> None:
        props_str = json.dumps(props) if props else "{}"

        # Ensure nodes exist to prevent FK errors in Kuzu
        self.conn.execute("MERGE (n:Node {id: $id})", {"id": str(src)})
        self.conn.execute("MERGE (n:Node {id: $id})", {"id": str(dst)})

        query = """
            MATCH (a:Node {id: $src}), (b:Node {id: $dst})
            MERGE (a)-[e:Edge {label: $label}]->(b)
            ON MATCH SET e.properties = $props
            ON CREATE SET e.properties = $props
        """
        self.conn.execute(query, {"src": str(src), "dst": str(dst), "label": rel, "props": props_str})

    async def add_edges(self, edges: List[EdgeTuple]) -> None:
        for edge in edges:
            src = str(edge[0])
            dst = str(edge[1])
            rel = str(edge[2])
            props = edge[3] if len(edge) > 3 else {}
            await self.add_edge(src, dst, rel, props)

    async def has_edge(self, src: str, dst: str, rel: str) -> bool:
        query = """
            MATCH (a:Node {id: $src})-[e:Edge {label: $label}]->(b:Node {id: $dst})
            RETURN count(e) AS cnt
        """
        result = self.conn.execute(query, {"src": str(src), "dst": str(dst), "label": rel})
        return result.get_next()[0] > 0

    async def has_edges(self, edges: List[EdgeTuple]) -> List[EdgeTuple]:
        return [e for e in edges if await self.has_edge(str(e[0]), str(e[1]), str(e[2]))]

    async def get_edges(self, node_id: str) -> List[EdgeTriple]:
        # Outgoing
        query_out = """
            MATCH (a:Node {id: $id})-[e:Edge]->(b:Node)
            RETURN a, e.label, b
        """
        result_out = self.conn.execute(query_out, {"id": str(node_id)})

        # Incoming
        query_in = """
            MATCH (a:Node)-[e:Edge]->(b:Node {id: $id})
            RETURN a, e.label, b
        """
        result_in = self.conn.execute(query_in, {"id": str(node_id)})

        edges = []

        def process_row(a_node, label, b_node):
            def to_dict(n):
                out = {"id": n["id"], "type": n["type"], "name": n["name"]}
                if n.get("properties"):
                    try:
                        out.update(json.loads(n["properties"]))
                    except:
                        pass
                return out
            return (to_dict(a_node), label, to_dict(b_node))

        while result_out.has_next():
            row = result_out.get_next()
            edges.append(process_row(row[0], row[1], row[2]))

        while result_in.has_next():
            row = result_in.get_next()
            edges.append(process_row(row[0], row[1], row[2]))

        return edges

    async def delete_graph(self) -> None:
        # OpenCypher doesn't have a simple DETACH DELETE * that works unconditionally fast.
        self.conn.execute("MATCH (a)-[e]->(b) DELETE e")
        self.conn.execute("MATCH (n) DELETE n")

    async def get_graph_data(self) -> Tuple[List[NodeTuple], List[EdgeTuple]]:
        # Nodes
        res_nodes = self.conn.execute("MATCH (n:Node) RETURN n")
        nodes = []
        while res_nodes.has_next():
            row = res_nodes.get_next()[0]
            props = {"id": row["id"], "type": row["type"], "name": row["name"]}
            if row.get("properties"):
                try:
                    props.update(json.loads(row["properties"]))
                except:
                    pass
            nodes.append((row["id"], props))

        # Edges
        res_edges = self.conn.execute("MATCH (a:Node)-[e:Edge]->(b:Node) RETURN a.id, b.id, e.label, e.properties")
        edges = []
        while res_edges.has_next():
            row = res_edges.get_next()
            eprops = {}
            if row[3]:
                try:
                    eprops = json.loads(row[3])
                except:
                    pass
            edges.append((row[0], row[1], row[2], eprops))

        return nodes, edges

    async def get_graph_metrics(self, extended: bool = False) -> Dict[str, Any]:
        node_cnt = self.conn.execute("MATCH (n:Node) RETURN count(n)").get_next()[0]
        edge_cnt = self.conn.execute("MATCH ()-[e:Edge]->() RETURN count(e)").get_next()[0]
        return {
            "node_count": node_cnt,
            "edge_count": edge_cnt,
        }

    async def query_by_attributes(
        self,
        attribute_filters: List[Dict[str, List[Union[str, int]]]],
    ) -> Tuple[List[NodeTuple], List[EdgeTuple]]:
        # For simplicity, fallback to fetching all and filtering in python since
        # attributes are hidden inside a JSON string.
        nodes, edges = await self.get_graph_data()

        matched_nodes = []
        for n_id, data in nodes:
            for f in attribute_filters:
                match = True
                for k, v_list in f.items():
                    if data.get(k) not in v_list:
                        match = False
                        break
                if match:
                    matched_nodes.append((n_id, data))
                    break

        matched_ids = {n[0] for n in matched_nodes}
        matched_edges = [e for e in edges if e[0] in matched_ids and e[1] in matched_ids]

        return matched_nodes, matched_edges

    async def get_neighbors(self, node_id: str) -> List[NodeProps]:
        edges = await self.get_edges(node_id)
        neighbors = []
        for src, rel, dst in edges:
            if src["id"] != str(node_id):
                neighbors.append(src)
            if dst["id"] != str(node_id):
                neighbors.append(dst)
        return neighbors

    async def get_triplets(
        self,
        node_id: Union[str, UUID],
    ) -> List[Tuple[NodeProps, Dict[str, Any], NodeProps]]:
        n_id = str(node_id)
        edges = await self.get_edges(n_id)
        # Filter for outgoing
        out = []
        for src, rel, dst in edges:
            if src["id"] == n_id:
                out.append((src, {"label": rel}, dst))
        return out

    async def extract_typed_subgraph(
        self,
        node_type: Type[Any],
        names: List[str],
    ) -> Tuple[List[Tuple[int, dict]], List[Tuple[int, int, str, dict]]]:
        nodes, edges = await self.get_graph_data()

        sub_nodes = []
        sub_edges = []
        node_map = {}
        idx = 0

        for n_id, data in nodes:
            if data.get('name') in names:
                sub_nodes.append((idx, data))
                node_map[n_id] = idx
                idx += 1

        for u, v, k, data in edges:
            if u in node_map and v in node_map:
                sub_edges.append((node_map[u], node_map[v], k, data))

        return sub_nodes, sub_edges

    async def checkpoint(self) -> None:
        """Kuzu uses WAL, checkpoint flushes it."""
        try:
            self.conn.execute("CHECKPOINT;")
        except Exception as e:
            _log.warning(f"Failed to checkpoint Kuzu: {e}")
