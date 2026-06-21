"""Neo4jAdapter — persists CPG nodes and edges to a Neo4j graph database."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, TypeVar

import neo4j
from neo4j import GraphDatabase
from neo4j.exceptions import ClientError, ServiceUnavailable

from omnicpg.adapters import shared as _shared
from omnicpg.adapters.shared import (
    _VALID_LABEL_RE,
    _enrich_labels,
    _format_progress_bar,
    _neo4j_safe_properties,
)
from omnicpg.interfaces.graph_db_adapter import GraphDBAdapter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_BATCH_SIZE = 500
_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 2.0
_MAX_BATCH_DELETE = 10000
_SEMANTIC_LABEL_MAP = _shared._SEMANTIC_LABEL_MAP

_SECONDARY_INDEX_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("node_name_index", "CREATE INDEX node_name_index IF NOT EXISTS FOR (n:Node) ON (n.name)"),
    (
        "node_file_path_index",
        "CREATE INDEX node_file_path_index IF NOT EXISTS FOR (n:Node) ON (n.file_path)",
    ),
    (
        "node_project_id_index",
        "CREATE INDEX node_project_id_index IF NOT EXISTS FOR (n:Node) ON (n.project_id)",
    ),
    (
        "node_project_type_index",
        "CREATE INDEX node_project_type_index IF NOT EXISTS "
        "FOR (n:Node) ON (n.project_id, n.type)",
    ),
    (
        "node_project_name_index",
        "CREATE INDEX node_project_name_index IF NOT EXISTS "
        "FOR (n:Node) ON (n.project_id, n.name)",
    ),
    (
        "node_project_file_path_index",
        "CREATE INDEX node_project_file_path_index IF NOT EXISTS "
        "FOR (n:Node) ON (n.project_id, n.file_path)",
    ),
    (
        "node_project_fqn_index",
        "CREATE INDEX node_project_fqn_index IF NOT EXISTS FOR (n:Node) ON (n.project_id, n.fqn)",
    ),
    (
        "parent_of_project_id_index",
        "CREATE INDEX parent_of_project_id_index IF NOT EXISTS "
        "FOR ()-[r:PARENT_OF]-() ON (r.project_id)",
    ),
    (
        "contains_project_id_index",
        "CREATE INDEX contains_project_id_index IF NOT EXISTS "
        "FOR ()-[r:CONTAINS]-() ON (r.project_id)",
    ),
    (
        "flows_to_project_id_index",
        "CREATE INDEX flows_to_project_id_index IF NOT EXISTS "
        "FOR ()-[r:FLOWS_TO]-() ON (r.project_id)",
    ),
    (
        "calls_project_id_index",
        "CREATE INDEX calls_project_id_index IF NOT EXISTS FOR ()-[r:CALLS]-() ON (r.project_id)",
    ),
    (
        "reaches_project_id_index",
        "CREATE INDEX reaches_project_id_index IF NOT EXISTS "
        "FOR ()-[r:REACHES]-() ON (r.project_id)",
    ),
    (
        "depends_on_project_id_index",
        "CREATE INDEX depends_on_project_id_index IF NOT EXISTS "
        "FOR ()-[r:DEPENDS_ON]-() ON (r.project_id)",
    ),
    (
        "implements_project_id_index",
        "CREATE INDEX implements_project_id_index IF NOT EXISTS "
        "FOR ()-[r:IMPLEMENTS]-() ON (r.project_id)",
    ),
    (
        "tests_project_id_index",
        "CREATE INDEX tests_project_id_index IF NOT EXISTS FOR ()-[r:TESTS]-() ON (r.project_id)",
    ),
    (
        "calls_callsite_id_index",
        "CREATE INDEX calls_callsite_id_index IF NOT EXISTS "
        "FOR ()-[r:CALLS]-() ON (r.callsite_id)",
    ),
    (
        "calls_resolution_index",
        "CREATE INDEX calls_resolution_index IF NOT EXISTS FOR ()-[r:CALLS]-() ON (r.resolution)",
    ),
    (
        "reaches_interprocedural_index",
        "CREATE INDEX reaches_interprocedural_index IF NOT EXISTS "
        "FOR ()-[r:REACHES]-() ON (r.interprocedural)",
    ),
)


class Neo4jAdapter(GraphDBAdapter):
    """Concrete :class:`GraphDBAdapter` for Neo4j.

    Uses the official ``neo4j`` Python driver with ``UNWIND``-based batch
    inserts for efficient bulk writes.

    Args:
        batch_size: Number of records per ``UNWIND`` batch.
    """

    def __init__(
        self, batch_size: int = _DEFAULT_BATCH_SIZE, max_connection_pool_size: int = 1
    ) -> None:
        """Initialise the adapter with the given batch size and connection pool size.

        Args:
            batch_size: Number of records per ``UNWIND`` batch.
            max_connection_pool_size: Size of the Bolt connection pool.  The
                default of ``1`` is correct for single-threaded serial writes
                (the pipeline never opens more than one session at a time).
                Increase if you add concurrent write paths in the future.
        """
        self._batch_size = batch_size
        self._max_connection_pool_size = max_connection_pool_size
        self._driver: neo4j.Driver | None = None

    # ── GraphDBAdapter interface ──────────────────────────────────────────

    def connect(self, uri: str, auth: tuple[str, str]) -> None:
        """Connect to Neo4j and verify connectivity.

        Retries up to ``_MAX_RETRIES`` times on transient failures.

        Args:
            uri: Bolt URI (e.g. ``bolt://localhost:7687``).
            auth: ``(username, password)`` tuple.
        """
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._driver = GraphDatabase.driver(
                    uri,
                    auth=auth,
                    max_connection_pool_size=self._max_connection_pool_size,
                )
                self._driver.verify_connectivity()
                logger.info("Connected to Neo4j at %s", uri)
                self._ensure_constraints()
                return
            except ServiceUnavailable:
                logger.warning(
                    "Connection attempt %d/%d failed; retrying in %.1fs …",
                    attempt,
                    _MAX_RETRIES,
                    _RETRY_DELAY_SECONDS,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
        raise ConnectionError(f"Could not connect to Neo4j at {uri} after {_MAX_RETRIES} attempts")

    def disconnect(self) -> None:
        """Close the Neo4j driver."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None
            logger.info("Disconnected from Neo4j")

    def insert_nodes(
        self, nodes: list[CPGNode], bulk_load: bool = False, project_id: str = ""
    ) -> None:
        """Batch-upsert (or bulk-create) CPG nodes using ``UNWIND``.

        Each node receives its CPG labels plus an additional semantic label
        derived from ``_SEMANTIC_LABEL_MAP``, applied in a single round-trip
        via APOC ``addLabels``.

        Args:
            nodes: Nodes to persist.
            bulk_load: When ``True`` (full-rebuild scenario), use ``CREATE``
                instead of ``MERGE``.  Skips deduplication overhead; only
                safe when the database is known to be empty (``clear_db=True``).
            project_id: Project isolation key injected into each node when set.
        """
        driver = self._get_driver()

        if bulk_load:
            upsert_query = (
                "UNWIND $batch AS n "
                "CREATE (node:Node {id: n.id}) "
                "SET node += n.properties "
                "RETURN count(*)"
            )
        else:
            upsert_query = (
                "UNWIND $batch AS n "
                "MERGE (node:Node {id: n.id}) "
                "SET node += n.properties "
                "RETURN count(*)"
            )

        add_labels_query = (
            "UNWIND $batch AS n "
            "MATCH (node:Node {id: n.id}) "
            "WITH node, n "
            "CALL apoc.create.addLabels(node, n.labels) YIELD node AS _ "
            "RETURN count(*)"
        )

        total_batches = (len(nodes) + self._batch_size - 1) // self._batch_size
        with driver.session() as session:
            for batch_idx, batch in enumerate(self._batches(nodes), start=1):
                records: list[dict[str, Any]] = []
                for node in batch:
                    labels = [
                        lbl
                        for lbl in _enrich_labels(node)
                        if lbl and lbl != "Node" and _VALID_LABEL_RE.match(lbl)
                    ]
                    properties = _neo4j_safe_properties(dict(node.properties))
                    if project_id:
                        properties["project_id"] = project_id
                    records.append({"id": node.id, "labels": labels, "properties": properties})

                session.run(upsert_query, batch=records).consume()
                self._apply_labels(session, records, add_labels_query)

                logger.info(
                    "Node upsert progress %s", _format_progress_bar(batch_idx, total_batches)
                )

        logger.info("Inserted %d nodes into Neo4j", len(nodes))

    def insert_edges(
        self, edges: list[CPGEdge], bulk_load: bool = False, project_id: str = ""
    ) -> None:
        """Batch-upsert (or bulk-create) CPG edges using ``UNWIND``.

        Args:
            edges: Edges to persist.
            bulk_load: When ``True`` (full-rebuild scenario where the DB was
                just cleared), use ``CREATE`` instead of ``MERGE``.  This
                eliminates the two ``MATCH`` index lookups and the MERGE
                deduplication check per edge, giving a 3-5× throughput
                improvement for 20M+ edge imports.  Only safe when the
                database is known to be empty (``clear_db=True``).
            project_id: Project isolation key injected into each edge when set.
        """
        driver = self._get_driver()
        # Group edges by type so we can create the correct relationship type.
        by_type: dict[str, list[CPGEdge]] = {}
        for edge in edges:
            by_type.setdefault(str(edge.edge_type), []).append(edge)

        # Reuse a single session for all edge types — avoids N connection
        # checkout/checkin roundtrips where N = number of distinct edge types.
        with driver.session() as session:
            for edge_type, typed_edges in by_type.items():
                if bulk_load:
                    # Bulk path: DB just cleared; CREATE skips MERGE dedup check.
                    query = (
                        "UNWIND $batch AS e "
                        "MATCH (src:Node {id: e.source_id}) "
                        "MATCH (tgt:Node {id: e.target_id}) "
                        f"CREATE (src)-[r:{edge_type}]->(tgt) "
                        "SET r += e.properties"
                    )
                else:
                    # Incremental path: MERGE keeps the pipeline idempotent.
                    query = (
                        "UNWIND $batch AS e "
                        "MATCH (src:Node {id: e.source_id}) "
                        "MATCH (tgt:Node {id: e.target_id}) "
                        f"MERGE (src)-[r:{edge_type}]->(tgt) "
                        "SET r += e.properties"
                    )
                total_batches = (len(typed_edges) + self._batch_size - 1) // self._batch_size
                for batch_idx, batch in enumerate(self._batches(typed_edges), start=1):
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
                    session.run(query, batch=records).consume()
                    logger.info(
                        "Edge upsert progress [%s] %s",
                        edge_type,
                        _format_progress_bar(batch_idx, total_batches),
                    )

        logger.info("Inserted %d edges into Neo4j", len(edges))

    def clear(self) -> None:
        """Delete all relationships and nodes in small transactions to avoid OOM."""
        driver = self._get_driver()
        total_relationships_deleted = 0
        total_nodes_deleted = 0

        with driver.session() as session:
            while True:
                result = session.run(
                    "MATCH ()-[r]->() WITH r LIMIT $batch_size "
                    "DELETE r RETURN count(r) AS deleted",
                    batch_size=_MAX_BATCH_DELETE,
                )
                deleted = result.single()
                deleted_count = deleted["deleted"] if deleted else 0
                if deleted_count == 0:
                    break
                total_relationships_deleted += deleted_count
                logger.info(
                    "Deleted batch of %d relationships (total: %d)",
                    deleted_count,
                    total_relationships_deleted,
                )

            while True:
                result = session.run(
                    "MATCH (n) WITH n LIMIT $batch_size DELETE n RETURN count(n) AS deleted",
                    batch_size=_MAX_BATCH_DELETE,
                )
                deleted = result.single()
                deleted_count = deleted["deleted"] if deleted else 0
                if deleted_count == 0:
                    break
                total_nodes_deleted += deleted_count

                logger.info(
                    "Deleted batch of %d nodes (total: %d)",
                    deleted_count,
                    total_nodes_deleted,
                )

        logger.info(
            "Cleared all data from Neo4j (total %d relationships, %d nodes)",
            total_relationships_deleted,
            total_nodes_deleted,
        )

    def clear_file(self, file_path: str) -> int:
        """Delete all nodes (and their edges) belonging to a specific file.

        This enables clean incremental re-analysis: before re-analysing a
        file, its stale nodes (including any that were deleted from the
        source) are removed so the graph accurately reflects the current
        state of the code.

        Args:
            file_path: The ``file_path`` property value identifying nodes
                to delete.

        Returns:
            The number of nodes deleted.
        """
        return self.clear_files_batch([file_path])

    def clear_files_batch(self, file_paths: list[str]) -> int:
        """Delete all nodes belonging to a batch of files in a single query.

        Much more efficient than calling :meth:`clear_file` in a loop for
        large projects: all file paths are passed as a single ``IN`` list so
        only one (or a few) round-trips to Neo4j are needed.

        Args:
            file_paths: List of ``file_path`` property values to delete.

        Returns:
            Total number of nodes deleted.
        """
        if not file_paths:
            return 0

        driver = self._get_driver()
        total_deleted = 0
        # Chunk the path list to avoid parameter payload limits (~65k items)
        _path_chunk = 5000
        for i in range(0, len(file_paths), _path_chunk):
            paths_chunk = file_paths[i : i + _path_chunk]
            with driver.session() as session:
                while True:
                    result = session.run(
                        "MATCH (n:Node) WHERE n.file_path IN $paths "
                        f"WITH n LIMIT {_MAX_BATCH_DELETE} "
                        "DETACH DELETE n RETURN count(*) AS deleted",
                        paths=paths_chunk,
                    )
                    deleted = result.single()
                    deleted_count = deleted["deleted"] if deleted else 0
                    total_deleted += deleted_count
                    if deleted_count == 0:
                        break

        if total_deleted > 0:
            logger.info("Cleared %d nodes across %d files", total_deleted, len(file_paths))
        return total_deleted

    def query(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a Cypher query and return results as a list of dicts.

        Args:
            query_string: A Cypher read query.
            **params: Named parameters to bind into the query.

        Returns:
            A list of dictionaries, one per result row.
        """
        driver = self._get_driver()
        with driver.session() as session:
            result = session.run(query_string, **params)
            return [dict(record) for record in result]

    # ── Private helpers ───────────────────────────────────────────────────

    def _get_driver(self) -> neo4j.Driver:
        """Return the active driver, raising if not connected."""
        if self._driver is None:
            raise RuntimeError("Not connected to Neo4j. Call connect() first.")
        return self._driver

    def _ensure_constraints(self) -> None:
        """Create indexes and constraints for efficient graph queries.

        * Uniqueness on ``Node.id`` (primary key).
        * Composite index on ``Node.name`` for fast method/class lookups.
        * Index on ``Node.file_path`` for file-level queries.
        """
        driver = self._get_driver()
        with driver.session() as session:
            session.run(
                "CREATE CONSTRAINT node_id_unique IF NOT EXISTS "
                "FOR (n:Node) REQUIRE n.id IS UNIQUE"
            )
            for _name, statement in _SECONDARY_INDEX_STATEMENTS:
                session.run(statement)
        logger.info("Ensured uniqueness constraint and indexes on Node")

    def drop_secondary_indexes(self) -> None:
        """Drop non-constraint indexes before a full bulk load.

        Maintaining indexes during a 20M-node MERGE import slows writes
        significantly. Call this *before* bulk insert, then call
        :meth:`rebuild_secondary_indexes` afterwards.
        The uniqueness constraint on ``Node.id`` is intentionally kept so
        that MERGE can still find duplicates efficiently.
        """
        driver = self._get_driver()
        indexes_to_drop = [name for name, _statement in _SECONDARY_INDEX_STATEMENTS]
        with driver.session() as session:
            for idx in indexes_to_drop:
                try:
                    session.run(f"DROP INDEX {idx} IF EXISTS")
                    logger.info("Dropped index %s for bulk load", idx)
                except ClientError as exc:
                    logger.debug("Could not drop index %s: %s", idx, exc)

    def rebuild_secondary_indexes(self) -> None:
        """Recreate secondary indexes after a bulk load completes."""
        driver = self._get_driver()
        with driver.session() as session:
            for _name, statement in _SECONDARY_INDEX_STATEMENTS:
                session.run(statement)
        logger.info("Rebuilt secondary indexes after bulk load")

    def ensure_architectural_indexes(self) -> None:
        """Create indexes optimised for architectural-level queries.

        These indexes accelerate Cypher queries that navigate the skeleton
        graph produced by ``AnalysisLevel.ARCHITECTURAL``.  Call this
        method after :meth:`connect` when operating in architectural mode.
        """
        driver = self._get_driver()
        index_statements = [
            "CREATE INDEX idx_class_name IF NOT EXISTS FOR (n:Class) ON (n.name)",
            "CREATE INDEX idx_method_name IF NOT EXISTS FOR (n:Method) ON (n.name)",
            "CREATE INDEX idx_module_file IF NOT EXISTS FOR (n:Module) ON (n.file_path)",
            # Semantic-label indexes (from _SEMANTIC_LABEL_MAP).
            "CREATE INDEX idx_function_name IF NOT EXISTS FOR (n:Function) ON (n.name)",
            # CallSite.code can be large; use full-text instead of RANGE.
            (
                "CREATE FULLTEXT INDEX idx_callsite_code_fulltext IF NOT EXISTS "
                "FOR (n:CallSite) ON EACH [n.code]"
            ),
            (
                "CREATE FULLTEXT INDEX idx_source_code IF NOT EXISTS "
                "FOR (n:Method) ON EACH [n.source_code]"
            ),
            # P1 enrichment property indexes.
            "CREATE INDEX idx_node_layer IF NOT EXISTS FOR (n:Node) ON (n.layer)",
            "CREATE INDEX idx_node_role IF NOT EXISTS FOR (n:Node) ON (n.role)",
            "CREATE INDEX idx_method_complexity IF NOT EXISTS FOR (n:Method) ON (n.complexity)",
            (
                "CREATE INDEX idx_function_return_type IF NOT EXISTS "
                "FOR (n:Function) ON (n.return_type)"
            ),
        ]
        with driver.session() as session:
            # Backward compatibility: old RANGE index on CallSite.code fails on long values.
            try:
                session.run("DROP INDEX idx_callsite_code IF EXISTS")
            except ClientError:
                logger.debug("Legacy index drop skipped: idx_callsite_code")
            for stmt in index_statements:
                try:
                    session.run(stmt)
                except ClientError:
                    logger.debug("Index creation skipped (may already exist): %s", stmt)
        logger.info("Ensured architectural-mode indexes")

    def ensure_fulltext_indexes(self) -> None:
        """Create the code full-text search index used by ``search_code``.

        Indexes human-meaningful text on the principal skeleton labels so
        AI agents can recall code by keyword without knowing exact names.
        Idempotent: safe to call on every run.
        """
        driver = self._get_driver()
        statement = (
            "CREATE FULLTEXT INDEX code_fulltext IF NOT EXISTS "
            "FOR (n:Method|Class|Interface|Field) "
            "ON EACH [n.name, n.fqn, n.source_code]"
        )
        with driver.session() as session:
            try:
                session.run(statement)
            except ClientError as exc:
                logger.debug("Full-text index creation skipped: %s", exc)
        logger.info("Ensured code full-text index 'code_fulltext'")

    def _batches(self, items: list[_T]) -> Iterator[list[_T]]:
        """Yield successive chunks of ``_batch_size`` from *items*."""
        for i in range(0, len(items), self._batch_size):
            yield items[i : i + self._batch_size]

    def _apply_labels(
        self,
        session: neo4j.Session,
        records: list[dict[str, Any]],
        add_labels_query: str,
    ) -> None:
        """Apply labels, falling back when a schema constraint rejects a label."""
        if not records:
            return

        try:
            session.run(add_labels_query, batch=records).consume()
            return
        except ClientError as exc:
            if _is_callsite_code_index_too_large(exc):
                logger.warning(
                    "CallSite.code value exceeded RANGE index limit; dropping "
                    "legacy idx_callsite_code and retrying batch",
                    exc_info=True,
                )
                session.run("DROP INDEX idx_callsite_code IF EXISTS").consume()
                session.run(add_labels_query, batch=records).consume()
                return
            if not _is_label_conflict(exc):
                raise
            logger.warning(
                "Batch label application hit a constraint conflict; retrying per label",
                exc_info=True,
            )

        single_label_query = (
            "MATCH (node:Node {id: $id}) "
            "CALL apoc.create.addLabels(node, [$label]) YIELD node AS _ "
            "RETURN count(*)"
        )
        for record in records:
            node_id = str(record["id"])
            for label in record["labels"]:
                try:
                    session.run(single_label_query, id=node_id, label=label).consume()
                except ClientError as exc:
                    if not _is_label_conflict(exc):
                        raise
                    logger.warning(
                        "Skipping conflicting label %s for node %s",
                        label,
                        node_id,
                    )


# ── Module-level helpers ─────────────────────────────────────────────────
def _is_label_conflict(exc: ClientError) -> bool:
    """Return True when a label-addition failure is caused by a uniqueness conflict."""
    message = str(exc)
    return "IndexEntryConflictException" in message or "already exists with label" in message


def _is_callsite_code_index_too_large(exc: ClientError) -> bool:
    """Return True when CallSite.code exceeds legacy RANGE index value limits."""
    message = str(exc).lower()
    return (
        "apoc.create.addlabels" in message
        and "property value is too large to index" in message
        and "idx_callsite_code" in message
    )
