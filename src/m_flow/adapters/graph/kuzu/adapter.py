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
        if not nodes:
            return

        batch = []
        for node in nodes:
            merged_props = _merge_node_props(node, None)
            node_id = str(merged_props.get("id"))
            node_type = str(merged_props.get("type", "Node"))
            name = str(merged_props.get("name", node_id))

            core_keys = {"id", "type", "name"}
            extra_props = {k: v for k, v in merged_props.items() if k not in core_keys}
            props_str = json.dumps(extra_props)

            batch.append({
                "id": node_id,
                "type": node_type,
                "name": name,
                "props": props_str
            })

        query = """
            UNWIND $batch AS item
            MERGE (n:Node {id: item.id})
            ON MATCH SET n.type = item.type, n.name = item.name, n.properties = item.props
            ON CREATE SET n.type = item.type, n.name = item.name, n.properties = item.props
        """
        self.conn.execute(query, {"batch": batch})

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
        if not ids:
            return []
        ids_str = [str(nid) for nid in ids]
        result = self.conn.execute("MATCH (n:Node) WHERE n.id IN $ids RETURN n", {"ids": ids_str})
        nodes = {}
        while result.has_next():
            row = result.get_next()[0]
            out = {"id": row["id"], "type": row["type"], "name": row["name"]}
            if row.get("properties"):
                try:
                    extra = json.loads(row["properties"])
                    out.update(extra)
                except Exception:
                    pass
            nodes[row["id"]] = out
        return [nodes[nid] for nid in ids_str if nid in nodes]

    async def delete_node(self, node_id: str) -> None:
        self.conn.execute("MATCH (n:Node {id: $id}) DETACH DELETE n", {"id": str(node_id)})

    async def delete_nodes(self, ids: List[str]) -> None:
        if not ids:
            return
        ids_str = [str(nid) for nid in ids]
        self.conn.execute("MATCH (n:Node) WHERE n.id IN $ids DETACH DELETE n", {"ids": ids_str})

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
        if not edges:
            return

        # 1. First batch-merge referenced node placeholders to avoid FK errors
        node_ids = set()
        for edge in edges:
            node_ids.add(str(edge[0]))
            node_ids.add(str(edge[1]))

        if node_ids:
            self.conn.execute("""
                UNWIND $ids AS id
                MERGE (n:Node {id: id})
                ON CREATE SET n.type = 'Node', n.name = id, n.properties = '{}'
            """, {"ids": list(node_ids)})

        # 2. Batch-merge all edges
        batch = []
        for edge in edges:
            src = str(edge[0])
            dst = str(edge[1])
            rel = str(edge[2])
            props = edge[3] if len(edge) > 3 else {}
            props_str = json.dumps(props) if props else "{}"
            batch.append({
                "src": src,
                "dst": dst,
                "label": rel,
                "props": props_str
            })

        self.conn.execute("""
            UNWIND $edges AS edge
            MATCH (a:Node {id: edge.src}), (b:Node {id: edge.dst})
            MERGE (a)-[e:Edge {label: edge.label}]->(b)
            ON MATCH SET e.properties = edge.props
            ON CREATE SET e.properties = edge.props
        """, {"edges": batch})

    async def has_edge(self, src: str, dst: str, rel: str) -> bool:
        query = """
            MATCH (a:Node {id: $src})-[e:Edge {label: $label}]->(b:Node {id: $dst})
            RETURN count(e) AS cnt
        """
        result = self.conn.execute(query, {"src": str(src), "dst": str(dst), "label": rel})
        return result.get_next()[0] > 0

    async def has_edges(self, edges: List[EdgeTuple]) -> List[EdgeTuple]:
        if not edges:
            return []

        batch = []
        mapping = {}
        for e in edges:
            src = str(e[0])
            dst = str(e[1])
            label = str(e[2])
            batch.append({
                "src": src,
                "dst": dst,
                "label": label
            })
            mapping[(src, dst, label)] = e

        query = """
            UNWIND $batch AS edge
            MATCH (a:Node {id: edge.src})-[rel:Edge {label: edge.label}]->(b:Node {id: edge.dst})
            RETURN edge.src, edge.dst, edge.label
        """
        result = self.conn.execute(query, {"batch": batch})
        existing = []
        while result.has_next():
            row = result.get_next()
            key = (str(row[0]), str(row[1]), str(row[2]))
            if key in mapping:
                existing.append(mapping[key])
        return existing

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
                    except Exception:
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
                except Exception:
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
                except Exception:
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
        if not attribute_filters:
            return [], []

        or_parts = []
        params = {}
        for i, flt in enumerate(attribute_filters):
            and_parts = []
            if not flt:
                and_parts.append("true")
            else:
                for k, vals in flt.items():
                    pname = f"vals_{i}_{k}"
                    if k in {"id", "type", "name"}:
                        and_parts.append(f"n.{k} IN ${pname}")
                        params[pname] = [str(v) for v in vals]
                    else:
                        and_parts.append(f"json_extract(n.properties, '$.{k}') IN ${pname}")
                        params[pname] = [json.dumps(v) for v in vals]
            if and_parts:
                or_parts.append(f"({' AND '.join(and_parts)})")

        if not or_parts:
            return [], []

        where_clause = " OR ".join(or_parts)

        # 1. Fetch matching nodes natively
        node_query = f"""
            MATCH (n:Node)
            WHERE {where_clause}
            RETURN n.id, n.type, n.name, n.properties
        """
        res_nodes = self.conn.execute(node_query, params)
        nodes = []
        while res_nodes.has_next():
            row = res_nodes.get_next()
            props = {"id": row[0], "type": row[1], "name": row[2]}
            if row[3]:
                try:
                    props.update(json.loads(row[3]))
                except Exception:
                    pass
            nodes.append((row[0], props))

        # 2. Fetch edges linking matching nodes natively
        where_clause_a = where_clause.replace("n.", "a.")
        where_clause_b = where_clause.replace("n.", "b.")
        edge_query = f"""
            MATCH (a:Node)-[e:Edge]->(b:Node)
            WHERE ({where_clause_a}) AND ({where_clause_b})
            RETURN a.id, b.id, e.label, e.properties
        """
        res_edges = self.conn.execute(edge_query, params)
        edges = []
        while res_edges.has_next():
            row = res_edges.get_next()
            eprops = {}
            if row[3]:
                try:
                    eprops = json.loads(row[3])
                except Exception:
                    pass
            edges.append((row[0], row[1], row[2], eprops))

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
