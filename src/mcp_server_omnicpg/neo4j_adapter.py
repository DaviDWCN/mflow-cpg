"""Neo4j adapter wrapper for MCP server.

Provides a singleton adapter that wraps the existing Neo4jAdapter
to provide a convenient interface for MCP tools.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from mcp_server_omnicpg.config import Config
from omnicpg.adapters.shared import (
    _VALID_LABEL_RE,
    _enrich_labels,
    _format_progress_bar,
    _neo4j_safe_properties,
)

logger = logging.getLogger(__name__)


class MCPNeo4jAdapter:
    """Singleton Neo4j adapter wrapper for MCP server.

    This adapter wraps the existing Neo4jAdapter and provides
    connection management, retry logic, and error handling
    specifically for MCP tool operations.

    Uses singleton pattern to ensure only one connection is maintained.
    """

    _instance: MCPNeo4jAdapter | None = None

    def __new__(cls) -> MCPNeo4jAdapter:
        """Create or return the singleton instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialise instance attributes on first construction."""
        # Guard against re-initialisation on subsequent singleton lookups.
        if not hasattr(self, "_driver"):
            self._driver: Any = None
            self._connected: bool = False

    def connect(self) -> None:
        """Connect to Neo4j with retry logic.

        Raises:
            ConnectionError: If connection fails after max retries.
        """
        if self._connected and self._driver is not None:
            return

        Config.validate()

        from neo4j import GraphDatabase
        from neo4j.exceptions import ServiceUnavailable

        for attempt in range(1, Config.MAX_RETRIES + 1):
            try:
                self._driver = GraphDatabase.driver(
                    Config.NEO4J_URI,
                    auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
                )
                self._driver.verify_connectivity()
                self._connected = True
                logger.info("Connected to Neo4j at %s", Config.NEO4J_URI)
                return
            except ServiceUnavailable:
                logger.warning(
                    "Connection attempt %d/%d failed; retrying in %.1fs …",
                    attempt,
                    Config.MAX_RETRIES,
                    Config.RETRY_DELAY_SECONDS,
                )
                time.sleep(Config.RETRY_DELAY_SECONDS)

        raise ConnectionError(
            f"Could not connect to Neo4j at {Config.NEO4J_URI} after {Config.MAX_RETRIES} attempts"
        )

    def disconnect(self) -> None:
        """Close the Neo4j connection."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None
            self._connected = False
            logger.info("Disconnected from Neo4j")

    def query(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a Cypher query with retry logic.

        Args:
            query_string: A Cypher read query.
            **params: Named parameters to bind into the query.

        Returns:
            A list of dictionaries, one per result row.

        Raises:
            RuntimeError: If not connected to Neo4j.
            ConnectionError: If query fails after max retries.
        """
        if not self._connected or self._driver is None:
            self.connect()

        from neo4j.exceptions import ServiceUnavailable

        for attempt in range(1, Config.MAX_RETRIES + 1):
            try:
                with self._driver.session() as session:
                    # Route read queries through APOC for better planning/caching behavior
                    # in complex workloads. Fall back to direct Cypher when APOC is not
                    # available for any reason.
                    apoc_query = "CALL apoc.cypher.run($cypher, $params) YIELD value RETURN value"
                    try:
                        result = session.run(apoc_query, cypher=query_string, params=params)
                        rows: list[dict[str, Any]] = []
                        for record in result:
                            # NB: neo4j ``Record.__contains__`` checks VALUES, not
                            # keys, so membership must be tested against ``keys()``
                            # to detect the APOC single-column ``value`` wrapper.
                            if "value" not in record.keys():  # noqa: SIM118
                                rows.append(dict(record))
                                continue
                            value = record.get("value")
                            if isinstance(value, dict):
                                rows.append(value)
                            else:
                                rows.append({"value": value})
                        return rows
                    except Exception:
                        logger.warning(
                            "APOC read wrapper failed; falling back to direct Cypher",
                            exc_info=True,
                        )
                        result = session.run(query_string, **params)
                        return [dict(record) for record in result]
            except ServiceUnavailable:
                logger.warning(
                    "Query attempt %d/%d failed; retrying in %.1fs …",
                    attempt,
                    Config.MAX_RETRIES,
                    Config.RETRY_DELAY_SECONDS,
                )
                time.sleep(Config.RETRY_DELAY_SECONDS)

        raise ConnectionError(
            f"Query failed after {Config.MAX_RETRIES} attempts: {query_string[:100]}"
        )

    def ensure_connected(self) -> None:
        """Ensure the adapter is connected to Neo4j."""
        if not self._connected or self._driver is None:
            self.connect()

    def is_connected(self) -> bool:
        """Check if the adapter is connected to Neo4j."""
        return self._connected and self._driver is not None

    # ── Write helpers for on-demand expansion ────────────────────────────

    def execute_write(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a Cypher write query with retry logic.

        Args:
            query_string: A Cypher write query.
            **params: Named parameters to bind into the query.

        Returns:
            A list of dictionaries, one per result row.
        """
        if not self._connected or self._driver is None:
            self.connect()

        from neo4j.exceptions import ServiceUnavailable

        for attempt in range(1, Config.MAX_RETRIES + 1):
            try:
                with self._driver.session() as session:
                    result = session.run(query_string, **params)
                    return [dict(record) for record in result]
            except ServiceUnavailable:
                logger.warning(
                    "Write attempt %d/%d failed; retrying in %.1fs …",
                    attempt,
                    Config.MAX_RETRIES,
                    Config.RETRY_DELAY_SECONDS,
                )
                time.sleep(Config.RETRY_DELAY_SECONDS)

        raise ConnectionError(
            f"Write failed after {Config.MAX_RETRIES} attempts: {query_string[:100]}"
        )

    def check_method_expanded(self, method_id: str) -> bool:
        """Check whether a method has already been expanded.

        Args:
            method_id: The CPG node ID of the method.

        Returns:
            ``True`` if the method's ``expanded`` property is ``true``.
        """
        results = self.query(
            "MATCH (m:Node {id: $method_id, type: 'function_definition'}) "
            "RETURN m.expanded AS expanded",
            method_id=method_id,
        )
        if not results:
            return False
        return bool(results[0].get("expanded"))

    def mark_method_expanded(self, method_id: str) -> None:
        """Set the ``expanded`` flag on a method node.

        Args:
            method_id: The CPG node ID of the method.
        """
        self.execute_write(
            "MATCH (m:Node {id: $method_id, type: 'function_definition'}) SET m.expanded = true",
            method_id=method_id,
        )

    def clear_file(self, file_path: str) -> int:
        """Delete all nodes (and their edges) belonging to a specific file.

        Enables clean incremental re-analysis by removing stale nodes
        before inserting fresh analysis results.

        Args:
            file_path: The ``file_path`` property value identifying nodes
                to delete.

        Returns:
            The number of nodes deleted.
        """
        self.ensure_connected()
        total_deleted = 0
        batch_limit = 100_000

        while True:
            results = self.execute_write(
                "MATCH (n:Node {file_path: $file_path}) "
                f"WITH n LIMIT {batch_limit} "
                "DETACH DELETE n RETURN count(*) AS deleted",
                file_path=file_path,
            )
            deleted_count = results[0]["deleted"] if results else 0
            total_deleted += deleted_count
            if deleted_count == 0:
                break

        if total_deleted > 0:
            logger.info("Cleared %d nodes for file %s", total_deleted, file_path)
        return total_deleted

    def insert_cpg_nodes(self, nodes: list[Any], project_id: str = "") -> None:
        """Batch-upsert CPG nodes into Neo4j.

        Re-uses the label enrichment logic from the core adapter so that
        nodes receive semantic labels (``Function``, ``Class``, …).

        Args:
            nodes: CPG nodes to persist.
            project_id: Project isolation key injected into each node when set.
        """
        if not nodes:
            return

        self.ensure_connected()

        merge_query = "UNWIND $batch AS n MERGE (node:Node {id: n.id}) SET node += n.properties"

        batch_size = 500
        total_batches = (len(nodes) + batch_size - 1) // batch_size
        with self._driver.session() as session:
            for batch_idx, i in enumerate(range(0, len(nodes), batch_size), start=1):
                batch = nodes[i : i + batch_size]
                records: list[dict[str, Any]] = []
                label_to_ids: dict[str, list[str]] = {}
                for node in batch:
                    labels = _enrich_labels(node)
                    properties = _neo4j_safe_properties(dict(node.properties))
                    if project_id:
                        properties["project_id"] = project_id
                    records.append(
                        {
                            "id": node.id,
                            "labels": labels,
                            "properties": properties,
                        }
                    )
                    for label in labels:
                        if not label or label == "Node":
                            continue
                        if not _VALID_LABEL_RE.match(label):
                            logger.warning("Skipping invalid Neo4j label: %r", label)
                            continue
                        label_to_ids.setdefault(label, []).append(node.id)

                session.run(merge_query, batch=records)

                for label in sorted(label_to_ids):
                    session.run(
                        f"MATCH (node:Node) WHERE node.id IN $ids SET node:{label}",
                        ids=label_to_ids[label],
                    )
                logger.info(
                    "MCP node upsert progress %s",
                    _format_progress_bar(batch_idx, total_batches),
                )

        logger.info("Inserted %d nodes via MCP adapter", len(nodes))

    def insert_cpg_edges(self, edges: list[Any], project_id: str = "") -> None:
        """Batch-upsert CPG edges into Neo4j using ``MERGE``.

        Uses ``MERGE`` so that re-analysing the same codebase does not
        create duplicate relationships.

        Args:
            edges: CPG edges to persist.
            project_id: Project isolation key injected into each edge when set.
        """
        if not edges:
            return

        self.ensure_connected()

        from omnicpg.models.edge import EdgeType

        valid_edge_types = set(EdgeType.__members__.keys())
        valid_edge_type_re = re.compile(r"^[A-Z_][A-Z0-9_]*$")

        by_type: dict[str, list[Any]] = {}
        for edge in edges:
            edge_type = str(edge.edge_type)
            if edge_type not in valid_edge_types or not valid_edge_type_re.match(edge_type):
                raise ValueError(f"Invalid edge_type '{edge_type}'")
            by_type.setdefault(edge_type, []).append(edge)

        batch_size = 500
        for edge_type, typed_edges in by_type.items():
            query = (
                "UNWIND $batch AS e "
                "MATCH (src:Node {id: e.source_id}) "
                "MATCH (tgt:Node {id: e.target_id}) "
                f"MERGE (src)-[r:{edge_type}]->(tgt) "
                "SET r += e.properties"
            )
            total_batches = (len(typed_edges) + batch_size - 1) // batch_size
            with self._driver.session() as session:
                for batch_idx, i in enumerate(range(0, len(typed_edges), batch_size), start=1):
                    batch = typed_edges[i : i + batch_size]
                    records = []
                    for e in batch:
                        properties = dict(e.properties)
                        if project_id:
                            properties["project_id"] = project_id
                        records.append(
                            {
                                "source_id": e.source_id,
                                "target_id": e.target_id,
                                "properties": properties,
                            }
                        )
                    session.run(query, batch=records)
                    logger.info(
                        "MCP edge upsert progress [%s] %s",
                        edge_type,
                        _format_progress_bar(batch_idx, total_batches),
                    )

        logger.info("Inserted %d edges via MCP adapter", len(edges))


# Global singleton instance
_adapter: MCPNeo4jAdapter | None = None


def get_adapter() -> MCPNeo4jAdapter:
    """Get the global Neo4j adapter instance.

    Returns:
        The singleton MCPNeo4jAdapter instance.
    """
    global _adapter
    if _adapter is None:
        _adapter = MCPNeo4jAdapter()
    return _adapter
