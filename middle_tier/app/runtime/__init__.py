"""Agent Runtime invocation (ADR 001, ADR 005).

The only collaborator named in ``app/ports.py`` that had no implementation
anywhere in the tree. Everything else -- the identity broker, the session
manager, the streaming renderer -- was already built and tested; this was the
hole between them.

    from app.runtime import ReasoningEngineRuntimeClient
"""

from __future__ import annotations

from .client import (
    CLASS_METHOD,
    DEFAULT_AUTHORIZATION_ID,
    ReasoningEngineRuntimeClient,
    RuntimeClientError,
)

__all__ = [
    "CLASS_METHOD",
    "DEFAULT_AUTHORIZATION_ID",
    "ReasoningEngineRuntimeClient",
    "RuntimeClientError",
]
