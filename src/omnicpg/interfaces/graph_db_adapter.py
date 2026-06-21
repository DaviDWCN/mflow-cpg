"""Abstract base class for graph database adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnicpg.models.edge import CPGEdge
    from omnicpg.models.node import CPGNode


class GraphDBAdapter(ABC):
    """Interface for persisting a CPG to a graph database.

    Concrete implementations (e.g. Neo4j, Memgraph) must implement every
    abstract method defined here.
    """

    @abstractmethod
    def connect(self, uri: str, auth: tuple[str, str]) -> None:
        """Establish a connection to the graph database.

        Args:
            uri: Connection URI (e.g. ``bolt://localhost:7687``).
            auth: ``(username, password)`` credentials.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Close the database connection and release resources."""

    @abstractmethod
    def insert_nodes(self, nodes: list[CPGNode]) -> None:
        """Persist a batch of CPG nodes.

        Args:
            nodes: Nodes to insert.
        """

    @abstractmethod
    def insert_edges(self, edges: list[CPGEdge]) -> None:
        """Persist a batch of CPG edges.

        Args:
            edges: Edges to insert.
        """

    @abstractmethod
    def clear(self) -> None:
        """Remove **all** nodes and edges from the database.

        .. warning:: This is a destructive operation intended for testing only.
        """

    @abstractmethod
    def query(self, query_string: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a graph query and return results.

        This enables downstream consumers (e.g. AI agents, analysis tools)
        to run arbitrary read-only queries against the persisted CPG.

        Args:
            query_string: A query in the adapter's native language
                (e.g. Cypher for Neo4j, Gremlin for JanusGraph).
            **params: Named parameters to bind into the query.

        Returns:
            A list of dictionaries, one per result row.
        """
