"""CPGEdge — immutable edge in the Code Property Graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class EdgeType(StrEnum):
    """Legal edge types in a Code Property Graph.

    The first four types represent the classic CPG relationships (AST,
    CFG, DFG, call-graph).  The remaining types support the
    *macro-skeleton* and *semantic-ontology* layers that keep the
    persisted graph compact while enabling fast architectural queries.

    Macro-skeleton edges:

    * ``CONTAINS`` — hierarchical containment
      (``File → Class → Method``).
    * ``DEPENDS_ON`` — module-level import / dependency relationships.

    Semantic-ontology edges:

    * ``IMPLEMENTS_CONCEPT`` — links a code entity to a
      :class:`BusinessConcept` node, bridging the code world and the
      business domain.

    Inheritance and testing edges:

    * ``IMPLEMENTS`` — links a concrete class to its base class or
      interface (``Class → Class``).
    * ``TESTS`` — links a test function to the function it tests
      (``Method → Method``), derived from naming conventions and imports.
    """

    PARENT_OF = "PARENT_OF"
    FLOWS_TO = "FLOWS_TO"
    REACHES = "REACHES"
    CALLS = "CALLS"
    CONTAINS = "CONTAINS"
    DEPENDS_ON = "DEPENDS_ON"
    IMPLEMENTS_CONCEPT = "IMPLEMENTS_CONCEPT"
    IMPLEMENTS = "IMPLEMENTS"
    TESTS = "TESTS"


@dataclass(frozen=True, slots=True)
class CPGEdge:
    """An immutable directed edge in the Code Property Graph.

    Attributes:
        source_id: UUID of the source node.
        target_id: UUID of the target node.
        edge_type: Relationship type (must be a valid ``EdgeType``).
        properties: Arbitrary key-value properties associated with the edge.
    """

    source_id: str
    target_id: str
    edge_type: EdgeType
    properties: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Validate edge invariants."""
        if not self.source_id:
            raise ValueError("CPGEdge.source_id must be a non-empty string")
        if not self.target_id:
            raise ValueError("CPGEdge.target_id must be a non-empty string")
        if not isinstance(self.edge_type, EdgeType):
            raise TypeError(
                f"CPGEdge.edge_type must be an EdgeType, got {type(self.edge_type).__name__}"
            )
        # Coerce a plain dict into MappingProxyType for true immutability.
        if isinstance(self.properties, dict):
            object.__setattr__(self, "properties", MappingProxyType(self.properties))
