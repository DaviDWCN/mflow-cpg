"""
Retrieval and search configuration for M-Flow.

Defines parameters for hybrid retrieval, BM25 sparse search,
and Reciprocal Rank Fusion (RRF).
"""

from __future__ import annotations

from functools import lru_cache
from pydantic import Field
from m_flow.config.env_compat import MflowSettings, SettingsConfigDict


class RetrievalConfig(MflowSettings):
    """
    Search and retrieval configuration parameters.

    Exposes controls for dense-sparse hybrid search, lexical term weighting,
    and rank merging logic. Can be overridden via environment variables
    prefixed with 'MFLOW_'.
    """

    enable_hybrid_search: bool = Field(
        default=True,
        description=(
            "Whether to enable Dense + Sparse Hybrid Search. "
            "When True, combines BM25 lexical keyword matching with dense vector similarity. "
            "Enabling this significantly improves retrieval of exact identifiers (like class names, "
            "method names, and database tables) without hurting semantic query comprehension."
        ),
    )

    bm25_k1: float = Field(
        default=1.5,
        description=(
            "BM25 parameter k1 controls term frequency scaling. "
            "This parameter calibrates the scaling of term frequency (TF). "
            "Higher values (e.g., 2.0) increase the score contribution of repeated occurrences of the "
            "same query token within a single document. Lower values (e.g., 1.2) cause the score benefit "
            "to saturate more quickly."
        ),
    )

    bm25_b: float = Field(
        default=0.75,
        description=(
            "BM25 parameter b controls document length normalization. "
            "This parameter regulates how much document length penalizes term matches. "
            "Value b=1.0 scales the penalty fully with document length, while b=0.0 turns off "
            "normalization entirely. SOTA default is 0.75, which balances penalizing longer documents "
            "with allowing matches in dense text."
        ),
    )

    rrf_k: int = Field(
        default=60,
        description=(
            "Reciprocal Rank Fusion (RRF) constant scaling factor. "
            "Determines the damping effect for low-ranking documents when merging results from "
            "vector and BM25 search. A higher value (e.g., 60) decreases the penalty difference "
            "between adjacent ranks (e.g., rank 1 vs rank 2), smoothing the fusion. A lower value "
            "makes the ranking highly sensitive to top positions in both lists."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="MFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )

    def to_dict(self) -> dict:
        """Export configuration parameters as a dictionary."""
        return {
            "enable_hybrid_search": self.enable_hybrid_search,
            "bm25_k1": self.bm25_k1,
            "bm25_b": self.bm25_b,
            "rrf_k": self.rrf_k,
        }


@lru_cache(maxsize=1)
def get_retrieval_config() -> RetrievalConfig:
    """Retrieve the singleton retrieval configuration instance."""
    return RetrievalConfig()
