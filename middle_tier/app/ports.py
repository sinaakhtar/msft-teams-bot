"""The seams. Four Protocols that let four people build in parallel.

These are CONTRACTS, not conveniences. Each one is the boundary between the
Bot Middle Tier (this package) and a component owned by someone else. Keep them
small; every method added here is a method four implementations have to agree
on.

Ownership map:

  ==========================  =====================================  ==========
  Protocol                    Implementation                         Status here
  ==========================  =====================================  ==========
  IdentityBroker              app/identity/ ChainedIdentityBroker    BUILT
  SessionManager              app/sessions/ AgentRuntimeSessionMgr   BUILT
  AgentRuntimeClient          app/runtime/ ReasoningEngineRuntime..  BUILT
  StreamingRenderer           app/streaming/ TeamsStreamingRenderer  BUILT
  StreamingRendererFactory    app/composition.py TeamsRendererFact.  BUILT
  ==========================  =====================================  ==========

All four are constructed by :func:`app.composition.build_dependencies`, which
is the only place that assembles them, and which raises rather than returning a
``Dependencies`` with a ``None`` in it. Three of the four are reached through
thin adapters in that module, for reasons documented there; the adapters add no
behaviour, they reconcile shapes and exception hierarchies.

This table said "NOT built" for all four until the integration pass. Three of
those four claims were false at the time they were read: only
``AgentRuntimeClient`` was genuinely missing. If you are about to write an
implementation of anything above, check ``app/composition.py`` first -- the
thing you are about to build almost certainly already exists.

FAILURE MODES ARE PART OF THE CONTRACT. Every method below documents what it
raises and what the middle tier will do about it, because ADR 004 (fail closed)
only works if the middle tier can tell "the user is not allowed" apart from
"the backend is having a bad day". Those get different user-facing messages and
different retry behaviour, so an implementation that collapses them into a bare
``Exception`` breaks the ADR.

Nothing in here is allowed to accept a Google service account as a substitute
for a user credential. ADR 002: a service account authenticates the SERVICE,
never the USER.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable


# --------------------------------------------------------------------------
# Shared error taxonomy
# --------------------------------------------------------------------------


class PortError(Exception):
    """Base class for all seam failures."""


class AuthorizationDenied(PortError):
    """The USER is not permitted to do this. Terminal for the turn.

    ADR 004: the middle tier renders a templated denial that NAMES the refused
    resource, and does NOT retry, does NOT fall back to a service account, and
    does NOT hand the raw error to the model to explain.

    :param resource: human-meaningful name of what was refused, e.g.
        ``bigquery.tables.get on example-project.sales.orders``. Required - a denial
        with no resource name is unactionable for the user.
    """

    def __init__(self, resource: str, detail: str = "") -> None:
        self.resource = resource
        self.detail = detail
        super().__init__(f"denied: {resource}" + (f" ({detail})" if detail else ""))


class IdentityUnavailable(PortError):
    """We could not obtain a user credential at all. Terminal for the turn.

    Distinct from :class:`AuthorizationDenied`: the user has not consented, the
    SSO token exchange failed, or consent was revoked. The middle tier responds
    with the identity-failure message plus a sign-in card.
    """


class TransientBackendError(PortError):
    """The backend failed in a way that may succeed on retry.

    5xx, timeout, quota. The middle tier MAY retry with backoff. It must never
    convert this into a denial message, because telling a user they lack
    permission when the service was merely down is worse than an outage.
    """


class SessionNotFound(PortError):
    """The named session does not exist on the reasoning engine."""


# --------------------------------------------------------------------------
# Data carried across the seams
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRef:
    """A handle to an Agent Runtime session.

    :param name: full resource name,
        ``projects/{p}/locations/{l}/reasoningEngines/{e}/sessions/{s}``.
    :param user_id: ``entra:{tid}:{oid}``. ADR 003. Never a Teams MRI.
    """

    name: str
    user_id: str
    session_id: str
    create_time: str | None = None


@dataclass(frozen=True)
class AgentEvent:
    """One event read back from a session, or streamed from an invocation.

    Intentionally loose (`payload` is the raw runtime dict) because the middle
    tier does not interpret content - it relays it. ADR: no prompt logic here.
    """

    author: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str | None = None
    partial: bool = False


# --------------------------------------------------------------------------
# IdentityBroker
# --------------------------------------------------------------------------


@runtime_checkable
class IdentityBroker(Protocol):
    """Turns a Teams SSO token into a Google access token for the SAME human.

    ADR 002, Tool Identity plane. Owned by the OBO/STS component; NOT
    implemented in this package. This Protocol is the whole of what the middle
    tier will call.

    Implementations MUST NOT return a service-account token under any
    circumstance, including as a fallback when the exchange fails. Returning
    ambient credentials here silently converts a per-user authorization model
    into a shared one and there is no downstream check that would notice.
    """

    async def get_google_access_token(self, user_key: str, teams_sso_token: str) -> str:
        """Exchange a Teams SSO assertion for a Google OAuth access token.

        :param user_key: ``entra:{tid}:{oid}``. Used for cache partitioning and
            for asserting that the returned token belongs to the expected
            subject. Implementations SHOULD verify the exchanged token's
            subject matches and raise if it does not.
        :param teams_sso_token: the Entra token obtained from the Teams SSO
            ``invoke`` flow. Treat as a credential: never log it.
        :returns: a bearer access token usable against Google APIs AS THE USER.

        :raises IdentityUnavailable: no consent, expired/invalid assertion,
            federation misconfigured, or the exchange endpoint rejected us.
            Middle tier renders the sign-in card.
        :raises AuthorizationDenied: the exchange succeeded technically but the
            user is barred from the target audience/scope. ``resource`` should
            name the scope or audience refused.
        :raises TransientBackendError: STS 5xx or timeout. Retryable.
        """
        ...

    async def invalidate(self, user_key: str) -> None:
        """Drop any cached credential for `user_key`.

        Called after a downstream 401 so the next turn re-exchanges rather than
        replaying a revoked token. MUST be idempotent and MUST NOT raise for an
        unknown key.
        """
        ...


# --------------------------------------------------------------------------
# SessionManager
# --------------------------------------------------------------------------


@runtime_checkable
class SessionManager(Protocol):
    """Owns the Agent Runtime ``sessions`` subresource lifecycle. ADR 005.

    HARD CONSTRAINT: the middle tier READS history and CREATES/DELETES
    sessions. It NEVER appends events. The runtime appends events as a
    consequence of invocation. An implementation that writes events will
    produce a history that disagrees with what the agent actually saw.
    """

    async def get_or_create(
        self, user_key: str, *, conversation_id: str, access_token: str
    ) -> SessionRef:
        """Return the current session for `user_key`, creating one if needed.

        :param user_key: ``entra:{tid}:{oid}``. This is the runtime's
            ``user_id``. Passing a Teams MRI here is an ADR 003 violation.
        :param conversation_id: the Teams conversation. ADR 005 keeps one
            Agent Runtime session per (user, conversation) pair, so a person
            talking to the bot in two places does not get one tangled history.
        :param access_token: the USER's Google access token, from
            :class:`IdentityBroker`. The sessions subresource is called as the
            human, not as the service.

        :raises AuthorizationDenied: the caller cannot create sessions on this
            reasoning engine. ``resource`` names the engine.
        :raises TransientBackendError: runtime 5xx/timeout.

        ``access_token`` and ``conversation_id`` were added during integration.
        The original signature took ``user_key`` alone, which no correct
        implementation could satisfy: with no user token it could only have
        called the sessions API under the service identity, which is precisely
        what ADR 002 forbids. The built implementation had the right shape all
        along and the contract was wrong.
        """
        ...

    async def reset(
        self, user_key: str, *, conversation_id: str, access_token: str
    ) -> SessionRef:
        """Start a fresh session, abandoning the previous one.

        Backs the ``/new`` Conversation Reset command. Implementations SHOULD
        create the new session before discarding the old handle, so a failure
        leaves the user with a working conversation rather than none.

        :raises TransientBackendError: runtime 5xx/timeout.
        """
        ...

    async def list_events(
        self, session: SessionRef, *, page_size: int = 100
    ) -> Sequence[AgentEvent]:
        """Read session history. READ ONLY - never appends.

        :raises SessionNotFound: session was deleted or never existed.
        :raises AuthorizationDenied: caller cannot read this session.
        :raises TransientBackendError: runtime 5xx/timeout.
        """
        ...

    async def delete(self, session: SessionRef) -> None:
        """Delete a session. Idempotent; a missing session is not an error."""
        ...


# --------------------------------------------------------------------------
# AgentRuntimeClient
# --------------------------------------------------------------------------


@runtime_checkable
class AgentRuntimeClient(Protocol):
    """Invokes the ADK agent on the reasoning engine. ADR 001: direct, not
    via the Gemini Enterprise assistant / streamAssist.

    Two credentials are in play per call and they are NOT the same thing
    (ADR 002):

      * Invocation Identity - the SERVICE's credential, authorizing this
        deployment to call the reasoning engine at all.
      * Tool Identity - the USER's Google access token, forwarded so the agent's
        tools (BigQuery MCP) act as the human.
    """

    async def stream_query(
        self,
        *,
        session: SessionRef,
        message: str,
        user_access_token: str,
        request_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Invoke the agent and yield events as they arrive.

        :param user_access_token: the Tool Identity token from
            :class:`IdentityBroker`. Implementations MUST fail rather than
            invoke without it; an invocation with no user token will run tools
            under the service identity, which is the exact failure ADR 002
            exists to prevent.
        :param request_id: idempotency/correlation handle. Should end up in the
            runtime's request logs so a user complaint can be traced.

        :raises AuthorizationDenied: the runtime or a tool refused. ``resource``
            MUST name what was refused (dataset, table, engine) so the middle
            tier's denial template can quote it.
        :raises IdentityUnavailable: the user token was rejected as expired or
            revoked. Middle tier calls ``IdentityBroker.invalidate`` and
            re-prompts sign-in ONCE; a second failure is terminal.
        :raises TransientBackendError: runtime 5xx/timeout/quota.

        Partial-stream failures: implementations SHOULD raise mid-iteration
        rather than truncating silently. A truncated answer that looks complete
        is worse than a visible error.
        """
        ...


# --------------------------------------------------------------------------
# StreamingRenderer
# --------------------------------------------------------------------------


@runtime_checkable
class StreamingRenderer(Protocol):
    """Turns a stream of :class:`AgentEvent` into Teams messages.

    Owned by the streaming/rendering component. The middle tier hands it events
    and a reply address; it decides on typing indicators, message updates,
    chunking and final card layout.

    It must NOT decide anything about content semantics beyond formatting -
    no summarizing, no rewriting, no model calls. The middle tier and its
    renderer are a relay.
    """

    async def begin(self, conversation_ref: Mapping[str, Any]) -> None:
        """Signal the start of a turn (typing indicator / placeholder message).

        Failures here are non-fatal: implementations SHOULD swallow transport
        errors and log, because failing a turn over a typing indicator is
        absurd.
        """
        ...

    async def push(self, event: AgentEvent) -> None:
        """Render one event. May coalesce; may no-op for events it ignores.

        :raises TransientBackendError: the Bot Connector rejected the update in
            a retryable way. The caller MAY continue consuming the stream and
            let ``finish`` deliver the complete text.
        """
        ...

    async def finish(self, *, error: Exception | None = None) -> None:
        """Finalize the turn.

        :param error: if the upstream stream raised, it is passed here so the
            renderer can replace the in-progress message with the ADR 004
            template rather than leaving a half-written answer on screen.

        MUST NOT raise. This is the last-chance cleanup path.
        """
        ...


@runtime_checkable
class StreamingRendererFactory(Protocol):
    """Produces one :class:`StreamingRenderer` per turn.

    Added during integration. :class:`StreamingRenderer` is inherently
    single-turn: ``begin``/``push``/``finish`` describe one message bubble with
    one sequence counter and one accumulating text buffer. But
    :class:`app.routing.Dependencies` lives for the life of the process and is
    shared by every concurrent turn, so holding a single renderer there would
    interleave two users' answers into one bubble under any real load. The
    dependency is therefore the factory, and the router asks it for a renderer
    per turn.
    """

    def for_turn(
        self,
        conversation_ref: Mapping[str, Any],
        *,
        request_id: str | None = None,
        user_display: str | None = None,
    ) -> StreamingRenderer:
        """Build a renderer bound to one conversation and one turn.

        :raises Exception: if the conversation cannot be replied to at all --
            for instance an untrusted ``serviceUrl``. The router turns that
            into a transient-failure body rather than a 500, because a 500
            makes Azure Bot Service retry an activity that will fail the same
            way every time.
        """
        ...


__all__ = [
    "AgentEvent",
    "AgentRuntimeClient",
    "AuthorizationDenied",
    "IdentityBroker",
    "IdentityUnavailable",
    "PortError",
    "SessionManager",
    "SessionNotFound",
    "SessionRef",
    "StreamingRenderer",
    "StreamingRendererFactory",
    "TransientBackendError",
]
