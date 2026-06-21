"""Abstract interfaces for OmniCPG."""

from omnicpg.interfaces.graph_db_adapter import GraphDBAdapter
from omnicpg.interfaces.language_plugin import LanguagePlugin

__all__ = ["GraphDBAdapter", "LanguagePlugin"]
