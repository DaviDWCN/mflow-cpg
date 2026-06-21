"""CPGNode — immutable node in the Code Property Graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class CPGNode:
    """An immutable node in the Code Property Graph.

    Attributes:
        id: Globally unique identifier (UUID string).
        labels: Node type labels (e.g. ``("Node", "Method")``).
        properties: Arbitrary key-value properties associated with the node.
    """

    id: str
    labels: tuple[str, ...]
    properties: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Validate node invariants."""
        if not self.id:
            raise ValueError("CPGNode.id must be a non-empty string")
        if not self.labels:
            raise ValueError("CPGNode.labels must contain at least one label")
        # Coerce a plain dict into MappingProxyType for true immutability.
        if isinstance(self.properties, dict):
            object.__setattr__(self, "properties", MappingProxyType(self.properties))

    # ── Convenience helpers ───────────────────────────────────────────────

    def with_properties(self, **kwargs: Any) -> CPGNode:
        """Return a new node with additional / overridden properties.

        The original node is not mutated.
        """
        merged = {**self.properties, **kwargs}
        return CPGNode(id=self.id, labels=self.labels, properties=MappingProxyType(merged))

    def has_label(self, label: str) -> bool:
        """Check whether the node carries a specific label."""
        return label in self.labels
