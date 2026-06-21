"""
Unified configuration loader for M-Flow × OmniCPG.

Loads settings from `config.yaml` (looking at project root or CWD)
and exposes them as parsed attributes or dictionary layouts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field

# Default path search order
_CONFIG_SEARCH_PATHS = [
    Path(os.getcwd()) / "config.yaml",
    Path(__file__).resolve().parents[2] / "config.yaml",
]

class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str = "password"
    database: str = "neo4j"

class LLMConfig(BaseModel):
    provider: str = "ollama"
    model: str = "llama3"
    api_key: str = "ollama"
    endpoint: str = "http://localhost:11434/v1"

class EmbeddingConfig(BaseModel):
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    api_key: str = "ollama"
    endpoint: str = "http://localhost:11434/v1"

class CPGConfig(BaseModel):
    analysis_level: str = "FULL"
    languages: List[str] = ["python", "java"]
    batch_size: int = 500

class SemanticLevelConfig(BaseModel):
    name: str
    enabled: bool = True
    target_labels: List[str]
    json_output: bool = False
    prompt: str

class SemanticAnalysisConfig(BaseModel):
    enabled: bool = True
    api_base: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "llama3"
    embedding_model: str = "nomic-embed-text"
    levels: List[SemanticLevelConfig] = Field(default_factory=list)

class RerankerSettings(BaseModel):
    enabled: bool = False
    provider: str = "ollama"
    model: str = "bge-reranker-v2-m3"
    endpoint: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    top_n: int = 10

class UnifiedConfig(BaseModel):
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    cpg: CPGConfig = Field(default_factory=CPGConfig)
    semantic_analysis: SemanticAnalysisConfig = Field(default_factory=SemanticAnalysisConfig)
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)



class ConfigManager:
    """Manages parsing and caching of unified project configuration."""
    _instance: Optional[UnifiedConfig] = None
    _config_path: Optional[Path] = None

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> UnifiedConfig:
        """Load configuration from YAML. Caches instance on success."""
        if cls._instance is not None and path is None:
            return cls._instance

        # 1. Resolve path
        resolved_path = None
        if path is not None:
            p = Path(path)
            if p.exists():
                resolved_path = p
        else:
            # Check env var first
            env_path = os.getenv("MFLOW_CPG_CONFIG")
            if env_path:
                p = Path(env_path)
                if p.exists():
                    resolved_path = p
            
            if not resolved_path:
                # Try search paths
                for p in _CONFIG_SEARCH_PATHS:
                    if p.exists():
                        resolved_path = p
                        break

        # If no config file found, load defaults and print warning
        if resolved_path is None:
            print("[Warning] No config.yaml found in search paths. Loading defaults.")
            cls._instance = UnifiedConfig()
            return cls._instance

        cls._config_path = resolved_path
        with open(resolved_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        cls._instance = UnifiedConfig.model_validate(data)
        return cls._instance

    @classmethod
    def get_config(cls) -> UnifiedConfig:
        """Get or load configuration singleton."""
        if cls._instance is None:
            return cls.load()
        return cls._instance


def get_config() -> UnifiedConfig:
    """Convenience getter for unified configuration."""
    return ConfigManager.get_config()
