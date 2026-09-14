"""Agent Runtime session lifecycle for the Bot Middle Tier (ADR 005).

Three unrelated things in this system are called a "session". This package
manages exactly one of them:

  * **Agent Runtime Session** - the conversational history held by the managed
    Sessions service on a reasoning engine. THIS is what this package owns.
  * **Workforce pool ``sessionDuration``** - the lifetime of a federated Google
    credential (3600s in this deployment). Owned by the identity plane; this
    package never reads, refreshes or reasons about it. It only ever receives
    an already-valid bearer token from its caller.
  * **Teams Conversation** - a Microsoft-side thread id. An input to the
    mapping, never a thing with a lifecycle we control.

Never write "the session expired" in this package without naming which of the
three you mean. The 60-minute idle policy in :mod:`.manager` and the 3600s
workforce credential lifetime are the same number by coincidence and are
completely unrelated mechanisms.
"""

from __future__ import annotations

from .client import (
    Session,
    SessionsRestClient,
    SessionsClient,
    SESSIONS_API_VERSION,
)
from .manager import (
    AgentRuntimeSessionManager,
    GroupConversationNotSupported,
    InvalidUserKey,
    SessionManagerError,
    IDLE_TIMEOUT_SECONDS,
)
from .store import InMemorySessionStore, SessionRecord, SessionStore

__all__ = [
    "Session",
    "SessionsClient",
    "SessionsRestClient",
    "SESSIONS_API_VERSION",
    "AgentRuntimeSessionManager",
    "GroupConversationNotSupported",
    "InvalidUserKey",
    "SessionManagerError",
    "IDLE_TIMEOUT_SECONDS",
    "InMemorySessionStore",
    "SessionRecord",
    "SessionStore",
]
