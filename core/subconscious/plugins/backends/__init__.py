"""Backend plugin registry for self-hosted LLM integrations.

Backends are registered when their modules are imported.
Import the backend you want to use:

    from core.subconscious.plugins.backends import (
        get_backend, list_backends, SimulatedBackend
    )
"""

from __future__ import annotations
from typing import Any, Dict, Optional, Type

from ..base import (
    BackendPlugin, get_backend_class, list_available_backends,
)

# Import backends to trigger registration
from . import simulated
from . import transformers
from . import llamacpp


def get_backend(name: str, config: Optional[Dict[str, Any]] = None) -> BackendPlugin:
    """Factory: create a backend plugin instance by name.

    Args:
        name: backend name ('simulated', 'transformers', 'llamacpp')
        config: configuration dict to pass to the backend constructor

    Returns:
        A BackendPlugin instance (not yet validated/loaded).

    Raises:
        ValueError: if backend name is not registered.
    """
    cls = get_backend_class(name)
    if cls is None:
        available = ", ".join(list_available_backends())
        raise ValueError(
            f"Unknown backend '{name}'. Available: {available}"
        )
    return cls(config or {})


def list_backends() -> Dict[str, str]:
    """List available backends with their status."""
    return {name: "registered" for name in list_available_backends()}
