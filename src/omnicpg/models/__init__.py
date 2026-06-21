"""Core domain models for OmniCPG."""

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode

__all__ = ["AnalysisLevel", "CPGEdge", "CPGNode", "EdgeType"]
