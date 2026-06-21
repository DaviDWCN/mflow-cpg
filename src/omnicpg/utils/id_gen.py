"""ID generation utilities.

Provides both random and **deterministic** ID generators.  Deterministic
IDs are derived from stable code-entity attributes (file path, node type,
name, line number) so that re-analysing the same source file always
produces the same node IDs — enabling idempotent ``MERGE`` in Neo4j.

Collision risk
--------------
Deterministic IDs are 128-bit (32 hex-char) truncations of SHA-256.  The
birthday-collision probability for *n* nodes is approximately
``n**2 / (2 * 2**128)``.  For a 10 million-node codebase this is ~3e-26
— negligible for all practical purposes.
"""

from __future__ import annotations

import hashlib
import uuid


def generate_id() -> str:
    """Generate a globally unique identifier (UUID v4).

    Returns:
        A UUID v4 string (e.g. ``'a1b2c3d4-...'``).
    """
    return str(uuid.uuid4())


def generate_deterministic_id(
    file_path: str,
    node_type: str,
    name: str,
    line_start: int,
    col_start: int = 0,
) -> str:
    """Generate a stable, deterministic ID for a code entity.

    The ID is a 32-character lowercase hex digest derived from the SHA-256
    hash of the combination of *file_path*, *node_type*, *name*,
    *line_start* and *col_start*.  Identical inputs always produce the
    same ID, which makes Neo4j ``MERGE`` operations truly idempotent
    across repeated analyses.

    Args:
        file_path: Absolute or relative path to the source file.
        node_type: Tree-sitter node type (e.g. ``'function_definition'``).
        name: Human-readable name of the entity (class name, method name,
              or the ``code`` snippet for unnamed nodes).
        line_start: 1-indexed starting line number.
        col_start: 0-indexed starting column offset (default ``0``).

    Returns:
        A 32-character hex string (128-bit).
    """
    key = f"{file_path}:{node_type}:{name}:{line_start}:{col_start}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def generate_deterministic_id_from_key(key: str) -> str:
    """Generate a stable, deterministic ID from an arbitrary string key.

    Useful for synthetic nodes (e.g. CFG Entry/Exit) whose identity is
    derived from a parent node's ID plus a suffix.

    Args:
        key: An arbitrary string that uniquely identifies the entity.

    Returns:
        A 32-character hex string (128-bit).
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
