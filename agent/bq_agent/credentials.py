"""Per-invocation user credential threading. THE SECURITY CORE OF THIS AGENT.

READ THIS BEFORE CHANGING ANYTHING IN THIS FILE.
================================================================================

One deployed Agent Runtime instance serves every Teams user. The agent's own
PROCESS runs under the runtime's service identity. That identity is NEVER a
valid Tool Identity for user-scoped BigQuery data (ADR 002). Every BigQuery
call must carry the *signed-in user's* Workforce Principal access token, and a
Tool Identity that outlives the turn that created it is a cross-user data leak,
not a bug.

THE PERSISTENCE CONSTRAINT (the single hardest rule here)
--------------------------------------------------------------------------------
A live bearer token must NEVER be written into durable conversation history.
Agent Runtime's managed Sessions service PERSISTS session state, so an ordinary
state key (``user:token``, ``bq_token``, ...) holding an access token would be
written to storage and replayed on every later turn. That is unacceptable and
is forbidden here.

There is exactly ONE exception, and this agent depends on it:

  ADK reserves the ``temp:`` state prefix for values that live for a single
  invocation and are stripped before the event is persisted.

  Verified by reading the installed source, not assumed:
    * google/adk/sessions/state.py                -> ``TEMP_PREFIX = "temp:"``
    * google/adk/sessions/base_session_service.py -> ``append_event`` calls
      ``_apply_temp_state`` (puts ``temp:`` keys into the in-memory session so
      the invocation can read them) and ``_trim_temp_delta_state`` (removes
      them from ``event.actions.state_delta``).
    * google/adk/sessions/vertex_ai_session_service.py -> ``append_event``
      calls ``super().append_event(...)`` FIRST and only afterwards builds the
      REST payload from ``event.actions.state_delta``. By then the ``temp:``
      keys are gone, so they are never sent to the Sessions service.
    * vertexai/agent_engines/templates/adk.py -> ``streaming_agent_run_with_events``
      maps each entry of the request's ``authorizations`` map to
      ``state_delta["temp:<auth_id>"] = auth.access_token``.
  ``agent/test_persistence.py`` asserts this end to end against a fake Vertex
  API client, so the claim is executable rather than a code-reading.

CONSEQUENCE: ``temp:``-prefixed keys are the ONLY state channel this module
will read a token from. :func:`_token_from_state` refuses any other key, and
:func:`assert_no_persisted_token` exists so a reviewer can assert the rule.

THE MECHANISM
--------------------------------------------------------------------------------
Primary: a :class:`contextvars.ContextVar` holding the caller's credential,
set at the invocation boundary and reset when the invocation ends. An async
``header_provider`` reads it at TOOL-CALL time. This is the approach the Layer 3
concurrency spike verified live (6/6 under 6-way concurrency, 54 invocations
total with zero cross-user leaks). contextvars are copied per ``asyncio.Task``,
so concurrent invocations cannot see each other's value.

Backstop: if the contextvar is empty, the provider falls back to the ``temp:``
key on the LIVE invocation context handed to it by ADK. In the deployed path
that value is per-request by construction, so the backstop is correct even in
the (untested-in-cloud) case where the contextvar set by the plugin does not
propagate into the tool-call task.

Never a fallback: ambient / default credentials. If neither source yields a
token, :class:`MissingUserCredential` is raised and the turn fails closed.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import dataclasses
import logging
import os
import time
from typing import Any, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Wire constants
# --------------------------------------------------------------------------

#: The authorization ID the middle tier must use in the
#: ``authorizations`` map of a ``streaming_agent_run_with_events`` request.
#: The runtime turns ``authorizations["bigquery_user"]`` into the session-state
#: key ``temp:bigquery_user``, which is what :func:`_token_from_state` reads.
#: Changing this string is a breaking contract change with the middle tier.
AUTHORIZATION_ID = os.environ.get("BQ_AGENT_AUTHORIZATION_ID", "bigquery_user")

#: The state key derived from AUTHORIZATION_ID. Must keep the ``temp:`` prefix.
TEMP_STATE_KEY = f"temp:{AUTHORIZATION_ID}"

#: Billing/quota project. Workforce principals have no project of their own, so
#: BigQuery cannot infer one to bill and rejects the call without this header.
#: This is why the pool principal needs roles/serviceusage.serviceUsageConsumer.
USER_PROJECT = os.environ.get("BQ_AGENT_USER_PROJECT", "")

#: Header name for the above. Mandatory for workforce principals.
USER_PROJECT_HEADER = "X-Goog-User-Project"

_TEMP_PREFIX = "temp:"


class MissingUserCredential(RuntimeError):
    """No per-user credential was available at tool-call time.

    ADR 002 says the correct behaviour here is to FAIL, loudly. Falling back to
    the runtime's own service identity would answer a user's question with an
    authority they do not have, which is precisely the leak this design exists
    to prevent. Callers must not catch this and retry unauthenticated.
    """


@dataclasses.dataclass(frozen=True)
class UserCredential:
    """A single user's request-scoped BigQuery credential.

    :param access_token: Google OAuth access token for the user's Workforce
        Principal (obtained by the middle tier via STS token exchange). Held in
        memory for the duration of one invocation only.
    :param user_project: project to bill/quota the call against.
    :param subject: OPTIONAL, non-secret, for logging and test assertions only,
        e.g. the workforce subject or Entra object id. Never sent as a header.
    """

    access_token: str
    user_project: str = USER_PROJECT
    subject: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.access_token or not self.access_token.strip():
            raise ValueError("access_token must be a non-empty string")

    def headers(self) -> dict[str, str]:
        """The MINIMAL header set for an MCP call as this user.

        Deliberately minimal. ADK pools MCP sessions on a hash of the merged
        headers, so every field that varies multiplies the pool. Only the two
        headers that change the *identity* or the *billing project* of the call
        belong here.
        """
        return {
            "Authorization": f"Bearer {self.access_token}",
            USER_PROJECT_HEADER: self.user_project,
        }

    def __repr__(self) -> str:  # never let a token reach a log line
        return (
            f"UserCredential(subject={self.subject!r}, "
            f"user_project={self.user_project!r}, access_token=<redacted>)"
        )

    __str__ = __repr__


# --------------------------------------------------------------------------
# The contextvar
# --------------------------------------------------------------------------

#: Request-scoped. Default None so an unset context fails closed rather than
#: inheriting somebody else's credential.
_CURRENT_CREDENTIAL: contextvars.ContextVar[Optional[UserCredential]] = (
    contextvars.ContextVar("bq_agent_current_credential", default=None)
)


def set_user_credential(credential: UserCredential) -> contextvars.Token:
    """Bind ``credential`` to the current context. Returns a reset token."""
    if not isinstance(credential, UserCredential):
        raise TypeError(f"expected UserCredential, got {type(credential)!r}")
    return _CURRENT_CREDENTIAL.set(credential)


def reset_user_credential(token: contextvars.Token) -> None:
    """Undo a :func:`set_user_credential`. Safe to call twice."""
    try:
        _CURRENT_CREDENTIAL.reset(token)
    except ValueError:
        # Token created in a different Context (e.g. the invocation was torn
        # down on another task). Clearing is the fail-closed outcome.
        _CURRENT_CREDENTIAL.set(None)


def current_user_credential() -> Optional[UserCredential]:
    """The credential bound to this context, or None."""
    return _CURRENT_CREDENTIAL.get()


@contextlib.contextmanager
def bind_user_credential(credential: UserCredential) -> Iterator[UserCredential]:
    """Scope a credential to a block. The local harness uses this per user."""
    token = set_user_credential(credential)
    try:
        yield credential
    finally:
        reset_user_credential(token)


# --------------------------------------------------------------------------
# Reading the token out of request-scoped state
# --------------------------------------------------------------------------


def _token_from_state(state: Mapping[str, Any] | None) -> Optional[str]:
    """Pull the access token out of ``temp:`` state. Refuses persisted keys.

    Only :data:`TEMP_STATE_KEY` is honoured. If a token turns up under a
    non-``temp:`` key we log a loud warning and IGNORE it: using it would mean
    reading a credential that the Sessions service has already written to
    durable storage, and silently accepting that would hide the bug.
    """
    if not state:
        return None

    value = state.get(TEMP_STATE_KEY)
    if isinstance(value, str) and value.strip():
        return value

    if not TEMP_STATE_KEY.startswith(_TEMP_PREFIX):  # pragma: no cover - guard
        raise RuntimeError(
            f"TEMP_STATE_KEY {TEMP_STATE_KEY!r} lost its 'temp:' prefix; "
            "that would read a credential from PERSISTED session state."
        )

    # Diagnostic only: catch a middle tier that sent the token the wrong way.
    stray = [
        key
        for key in state
        if not key.startswith(_TEMP_PREFIX)
        and (
            AUTHORIZATION_ID in key
            or key in {"access_token", "bearer_token", "bq_token"}
        )
    ]
    if stray:
        logger.error(
            "Refusing to read a credential from PERSISTED session state key(s) "
            "%s. Send the token in the request's `authorizations` map so the "
            "runtime forwards it as %s.",
            stray,
            TEMP_STATE_KEY,
        )
    return None


def assert_no_persisted_token(state: Mapping[str, Any] | None) -> None:
    """Raise if any non-``temp:`` state key looks like it holds a bearer token.

    Exists so tests and reviewers can assert the persistence constraint instead
    of trusting a comment. Heuristic by nature: it catches the mistakes that
    are actually made (a token under an obvious key name, or a value that looks
    like a JWT / Google access token), not every conceivable encoding.
    """
    if not state:
        return
    offenders = []
    for key, value in state.items():
        if key.startswith(_TEMP_PREFIX):
            continue
        if not isinstance(value, str):
            continue
        looks_like_token = (
            value.startswith(("ya29.", "Bearer ", "eyJ"))
            or (len(value) > 128 and value.count(".") == 2)
        )
        if looks_like_token or key in {"access_token", "bearer_token", "bq_token"}:
            offenders.append(key)
    if offenders:
        raise AssertionError(
            "Bearer-token-like values found under PERSISTED session state keys "
            f"{offenders}. Tokens must only travel as {TEMP_STATE_KEY}."
        )


def credential_from_context(readonly_context: Any) -> Optional[UserCredential]:
    """Best-effort credential for a live ADK invocation context.

    Precedence:
      1. the contextvar (set by :class:`UserCredentialPlugin` or the harness),
      2. the ``temp:`` state on the live invocation context.

    Returns None rather than raising so the caller decides the failure mode.
    """
    credential = current_user_credential()
    if credential is not None:
        return credential

    state = getattr(readonly_context, "state", None)
    token = _token_from_state(state)
    if token:
        return UserCredential(
            access_token=token,
            user_project=USER_PROJECT,
            subject=_subject_hint(state),
        )
    return None


def _subject_hint(state: Mapping[str, Any] | None) -> Optional[str]:
    """A NON-SECRET identifier for logs, if the middle tier supplied one."""
    if not state:
        return None
    for key in ("temp:user_subject", "user:subject", "user_subject"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# --------------------------------------------------------------------------
# The header provider
# --------------------------------------------------------------------------


async def header_provider(readonly_context: Any) -> dict[str, str]:
    """Async ``header_provider`` for :class:`McpToolset`.

    ADK 2.8.0 calls this at TOOL-CALL time with
    ``ReadonlyContext(tool_context._invocation_context)`` (see
    ``google/adk/tools/mcp_tool/mcp_tool.py``), which is why per-user
    credentials work at all: the headers are not frozen at construction.

    :raises MissingUserCredential: no request-scoped credential. Fails closed;
        there is deliberately no service-account path out of this function.
    """
    credential = credential_from_context(readonly_context)
    if credential is None:
        invocation_id = getattr(readonly_context, "invocation_id", "<unknown>")
        raise MissingUserCredential(
            "No per-user BigQuery credential is bound to this invocation "
            f"(invocation_id={invocation_id}). The middle tier must send the "
            f"user's access token as authorizations[{AUTHORIZATION_ID!r}] on "
            "the streaming_agent_run_with_events request. Refusing to fall "
            "back to the runtime service identity (ADR 002)."
        )

    headers = credential.headers()
    SESSION_POOL_GUARD.note(headers)
    return headers


# --------------------------------------------------------------------------
# Bounded session pool
# --------------------------------------------------------------------------


class SessionPoolGuard:
    """Backstop against unbounded MCP session growth from token rotation.

    ADK pools MCP sessions on a hash of the merged headers. The header set
    contains the access token, so EVERY token refresh for the same user mints a
    new pool entry. In a long-lived runtime instance that grows without bound.

    ADK 2.8.0 already mitigates most of this, verified in the installed source:
      * ``mcp_session_manager.py``: ``_SESSION_IDLE_TTL_SECONDS = 900`` with an
        idle sweep that skips sessions holding an in-flight call.
      * ``mcp_toolset.py``: ``_MAX_TOOL_LIST_CACHE_ENTRIES = 64`` on the
        tools/list cache.

    So this class is a WATCHDOG, not the primary mechanism. It tracks distinct
    identities cheaply (a salted digest, never the token) and escalates through
    the log when churn outruns the upstream sweep, which is the signal that the
    idle TTL needs lowering or the middle tier is refreshing too eagerly. It
    deliberately does not reach into ADK's private pool: closing a transport
    ADK believes is live is a worse failure than the leak it would fix.
    """

    def __init__(self, max_tracked: int = 256, warn_at: int = 128) -> None:
        self._max_tracked = max_tracked
        self._warn_at = warn_at
        self._seen: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._warned = False

    @staticmethod
    def _digest(headers: Mapping[str, str]) -> str:
        import hashlib

        material = "|".join(f"{k}={headers[k]}" for k in sorted(headers))
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def note(self, headers: Mapping[str, str]) -> None:
        key = self._digest(headers)
        now = time.monotonic()
        if key in self._seen:
            self._seen.move_to_end(key)
        self._seen[key] = now
        while len(self._seen) > self._max_tracked:
            self._seen.popitem(last=False)
        if len(self._seen) >= self._warn_at and not self._warned:
            self._warned = True
            logger.warning(
                "MCP session pool watchdog: %d distinct credential header sets "
                "seen in this instance. ADK sweeps sessions idle for >%ds, but "
                "if that is outpaced by token refresh, lower the TTL or reduce "
                "middle-tier refresh frequency.",
                len(self._seen),
                900,
            )

    @property
    def distinct_identities(self) -> int:
        return len(self._seen)

    def reset(self) -> None:
        self._seen.clear()
        self._warned = False


SESSION_POOL_GUARD = SessionPoolGuard()


# --------------------------------------------------------------------------
# The per-invocation set/reset hook
# --------------------------------------------------------------------------

try:  # ADK is a hard dependency at runtime; keep import errors legible.
    from google.adk.plugins.base_plugin import BasePlugin
except Exception as exc:  # pragma: no cover
    raise ImportError(
        "google-adk is required (pinned to 2.8.0 in requirements.txt)"
    ) from exc


class UserCredentialPlugin(BasePlugin):
    """Binds the per-user credential for exactly the length of one invocation.

    ``before_run_callback`` fires once per invocation, before any agent or tool
    runs, and ``after_run_callback`` fires once when it ends. That is the
    narrowest boundary ADK exposes, so it is where the contextvar is set and
    reset. Anything wider would let a Tool Identity outlive its turn.

    The token is read from ``temp:`` state, which the runtime populated from the
    request's ``authorizations`` map. See the module docstring for why that is
    the only state channel allowed to carry it.

    ``after_run_callback`` also deletes the ``temp:`` key from the in-memory
    session dict. ADK already strips it from the persisted event; this just
    shortens the window in which a live token sits in a reachable object.
    """

    def __init__(self, name: str = "user_credential_plugin") -> None:
        super().__init__(name=name)
        self._tokens: dict[str, contextvars.Token] = {}

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        state = None
        session = getattr(invocation_context, "session", None)
        if session is not None:
            state = getattr(session, "state", None)

        token_value = _token_from_state(state)
        if not token_value:
            # Do NOT raise here. A turn that needs no tool (a greeting, a
            # follow-up question) is still legitimate. The failure belongs at
            # the tool boundary, where `header_provider` raises.
            logger.info(
                "No %s in invocation state; tool calls this turn will fail "
                "closed unless a credential is bound another way.",
                TEMP_STATE_KEY,
            )
            return

        credential = UserCredential(
            access_token=token_value,
            user_project=USER_PROJECT,
            subject=_subject_hint(state),
        )
        self._tokens[invocation_context.invocation_id] = set_user_credential(
            credential
        )
        logger.debug(
            "Bound user credential for invocation %s (subject=%s)",
            invocation_context.invocation_id,
            credential.subject,
        )

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        token = self._tokens.pop(
            getattr(invocation_context, "invocation_id", ""), None
        )
        if token is not None:
            reset_user_credential(token)
        else:
            _CURRENT_CREDENTIAL.set(None)

        session = getattr(invocation_context, "session", None)
        state = getattr(session, "state", None)
        if isinstance(state, dict):
            state.pop(TEMP_STATE_KEY, None)
