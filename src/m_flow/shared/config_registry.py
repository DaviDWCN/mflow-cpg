"""
Configuration registry for dependency inversion.
Allows higher-level components (such as mflow_cpg) to register
a process-level configuration provider callback, which core modules
can access without reverse-importing the integration layer.
"""

from __future__ import annotations

from typing import Any, Callable

_config_provider: Callable[[], Any] | None = None


def register_config_provider(provider_fn: Callable[[], Any]) -> None:
    """Register a callback that returns the unified configuration object."""
    global _config_provider
    _config_provider = provider_fn


def get_global_config() -> Any:
    """Retrieve the unified configuration object from the registered provider."""
    if _config_provider is not None:
        return _config_provider()
    return None
