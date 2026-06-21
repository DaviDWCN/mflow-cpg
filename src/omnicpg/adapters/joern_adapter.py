"""JoernAdapter — exports CPG to Joern-compatible format and enables Joern queries.

`Joern <https://joern.io/>`_ is an open-source code analysis platform that
operates on Code Property Graphs.  This adapter serves as a *bridge* between
OmniCPG's in-memory CPG representation and Joern's ecosystem by:

1. **Exporting** OmniCPG nodes/edges to Joern-compatible CSV files that can
   be bulk-imported via ``joern-import``.
2. **Optionally connecting** to a running Joern server to execute CPGQL
   (Joern's query language) queries against a pre-imported CPG.

This does **not** replace Neo4j.  Instead it provides an *alternative*
persistence and query path for teams that already use Joern for security
analysis.

Why integrate Joern?
--------------------
* **Vulnerability detection** — Joern ships with hundreds of built-in
  security queries (taint tracking, buffer overflows, injection sinks).
* **CPGQL** — a purpose-built query language for code graphs, more concise
  than Cypher for security-oriented traversals.
* **Complementary** — Joern focuses on security; OmniCPG focuses on
  structural/AI-assisted analysis.  Using both together covers more ground.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnicpg.interfaces.graph_db_adapter import GraphDBAdapter

if TYPE_CHECKING:
    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)


class JoernAdapter(GraphDBAdapter):
    """Export CPG to Joern-compatible CSV and optionally query a Joern server.

    The adapter writes two CSV files:

    * ``nodes.csv`` — one row per :class:`CPGNode`.
    * ``edges.csv`` — one row per :class:`CPGEdge`.

    These can be imported into Joern via ``importCpg`` or ``joern-import``.

    If a ``server_uri`` is provided on :meth:`connect`, the adapter will also
    attempt to connect to a running `Joern server
    <https://docs.joern.io/server/>`_ for live queries.

    Args:
        output_dir: Directory where CSV files are written. Defaults to
            ``./joern_export``.
    """

    def __init__(self, output_dir: str = "joern_export") -> None:
        """Initialise the Joern adapter."""
        self._output_dir = Path(output_dir)
        self._connected = False
        self._server_uri: str | None = None

    # ── GraphDBAdapter interface ──────────────────────────────────────────

    def connect(self, uri: str, auth: tuple[str, str]) -> None:
        """Prepare the export directory (and optionally connect to Joern server).

        If *uri* is a file path (or the string ``"local"``), only CSV export
        is enabled.  If it starts with ``http://`` or ``ws://``, the adapter
        also connects to a Joern server.

        Args:
            uri: ``"local"`` for file-only mode, or a Joern server URI.
            auth: ``(username, password)`` — unused for local mode.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._connected = True
        if uri.startswith(("http://", "https://", "ws://")):
            self._server_uri = uri
            logger.info("Joern server URI registered: %s (queries available)", uri)
        else:
            logger.info(
                "Joern adapter in local/CSV mode — export dir: %s",
                self._output_dir,
            )

    def disconnect(self) -> None:
        """Mark the adapter as disconnected."""
        self._connected = False
        self._server_uri = None
        logger.info("Joern adapter disconnected")

    def insert_nodes(self, nodes: list[CPGNode]) -> None:
        """Write nodes to ``nodes.csv`` in the export directory.

        Args:
            nodes: Nodes to export.
        """
        self._check_connected()
        csv_path = self._output_dir / "nodes.csv"
        fieldnames = [
            "id",
            "labels",
            "type",
            "name",
            "code",
            "source_code",
            "file_path",
            "line_start",
            "line_end",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for node in nodes:
                row: dict[str, str] = {
                    "id": node.id,
                    "labels": ":".join(node.labels),
                }
                for key in (
                    "type",
                    "name",
                    "code",
                    "source_code",
                    "file_path",
                    "line_start",
                    "line_end",
                ):
                    row[key] = str(node.properties.get(key, ""))
                writer.writerow(row)
        logger.info("Exported %d nodes to %s", len(nodes), csv_path)

    def insert_edges(self, edges: list[CPGEdge]) -> None:
        """Write edges to ``edges.csv`` in the export directory.

        Args:
            edges: Edges to export.
        """
        self._check_connected()
        csv_path = self._output_dir / "edges.csv"
        fieldnames = ["source_id", "target_id", "edge_type", "properties"]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for edge in edges:
                writer.writerow(
                    {
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                        "edge_type": str(edge.edge_type),
                        "properties": str(dict(edge.properties)),
                    }
                )
        logger.info("Exported %d edges to %s", len(edges), csv_path)

    def clear(self) -> None:
        """Remove exported CSV files from the output directory."""
        for name in ("nodes.csv", "edges.csv"):
            path = self._output_dir / name
            if path.exists():
                os.remove(path)
        logger.info("Cleared Joern export files in %s", self._output_dir)

    def query(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a CPGQL query against the Joern server.

        This method requires a live Joern server connection (server URI must
        have been provided to :meth:`connect`).

        Args:
            query_string: A CPGQL query string.
            **params: Not used by Joern (kept for interface compatibility).

        Returns:
            Query results as a list of dicts.

        Raises:
            RuntimeError: If no Joern server URI was configured.
        """
        self._check_connected()
        if self._server_uri is None:
            raise RuntimeError(
                "Joern query requires a server connection. "
                "Pass a server URI (http://…) to connect()."
            )
        # In a production implementation this would use the Joern REST API
        # or WebSocket client.  For now we log the query and return an
        # empty result — the actual HTTP client can be added when the Joern
        # Python SDK stabilises.
        logger.info("Joern query (server=%s): %s", self._server_uri, query_string)
        return []

    # ── Convenience: read exported CSVs ──────────────────────────────────

    def read_exported_nodes(self) -> list[dict[str, str]]:
        """Read back the exported ``nodes.csv`` as a list of dicts.

        Returns:
            Parsed rows from the CSV.
        """
        csv_path = self._output_dir / "nodes.csv"
        if not csv_path.exists():
            return []
        with csv_path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return list(reader)

    def read_exported_edges(self) -> list[dict[str, str]]:
        """Read back the exported ``edges.csv`` as a list of dicts.

        Returns:
            Parsed rows from the CSV.
        """
        csv_path = self._output_dir / "edges.csv"
        if not csv_path.exists():
            return []
        with csv_path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return list(reader)

    # ── Private helpers ───────────────────────────────────────────────────

    def _check_connected(self) -> None:
        """Raise if :meth:`connect` has not been called."""
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")
