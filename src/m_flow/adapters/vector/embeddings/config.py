"""
Embedding model configuration.

Settings for vector embedding generation including provider,
model selection, dimensions, and API credentials.
"""

from __future__ import annotations

from functools import lru_cache

from m_flow.config.env_compat import MflowSettings, SettingsConfigDict


class EmbeddingConfig(MflowSettings):
    """
    Vector embedding configuration.

    Attributes:
        embedding_provider: Backend (openai, ollama, fastembed).
        embedding_model: Model identifier.
        embedding_dimensions: Output vector size.
        embedding_endpoint: Custom API endpoint.
        embedding_api_key: Authentication key.
        embedding_api_version: API version string.
        embedding_max_completion_tokens: Token limit per request.
        embedding_batch_size: Items per batch.
        huggingface_tokenizer: HF tokenizer for counting.
    """

    embedding_provider: str | None = "openai"
    embedding_model: str | None = "openai/text-embedding-3-large"
    embedding_dimensions: int | None = 3072
    embedding_endpoint: str | None = None
    embedding_api_key: str | None = None
    embedding_api_version: str | None = None
    embedding_max_completion_tokens: int | None = 8191
    embedding_batch_size: int | None = None
    huggingface_tokenizer: str | None = None

    model_config = SettingsConfigDict(env_prefix="MFLOW_", env_file=".env", extra="allow")

    def model_post_init(self, __context) -> None:
        if self.embedding_batch_size is None:
            self.embedding_batch_size = 36

    def to_dict(self) -> dict:
        """Serialize config to dictionary."""
        return {
            k: getattr(self, k)
            for k in [
                "embedding_provider",
                "embedding_model",
                "embedding_dimensions",
                "embedding_endpoint",
                "embedding_api_key",
                "embedding_api_version",
                "embedding_max_completion_tokens",
                "huggingface_tokenizer",
            ]
        }


@lru_cache
def get_embedding_config() -> EmbeddingConfig:
    """Cached config singleton."""
    from m_flow.shared.config_registry import get_global_config
    unified_cfg = get_global_config()
    if unified_cfg is not None:
        # nomic-embed-text uses 768 dimensions, while others (like OpenAI) use 1536/3072.
        dims = 768 if "nomic" in unified_cfg.embedding.model.lower() or unified_cfg.embedding.provider.lower() == "ollama" else 1536
        return EmbeddingConfig(
            embedding_provider=unified_cfg.embedding.provider,
            embedding_model=unified_cfg.embedding.model,
            embedding_endpoint=unified_cfg.embedding.endpoint,
            embedding_api_key=unified_cfg.embedding.api_key,
            embedding_dimensions=dims,
        )
    return EmbeddingConfig()
