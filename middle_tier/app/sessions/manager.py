"""Map a Teams conversation to an Agent Runtime Session, and own its lifecycle.

THE CONTRACT
------------
Two methods, both returning a bare Agent Runtime Session id, both raising typed
errors, neither ever returning ``None``::

    async def resolve(*, user_key, conversation_id, access_token) -> str
    async def reset(*, user_key, conversation_id, access_token) -> str

``resolve`` is called at the top of every turn. ``reset`` is called by the
``/new`` command.

THE LIFECYCLE
-------------
* **First turn.** No mapping -> create a session owned by ``user_key`` and
  remember it.
* **Subsequent turns within 60 minutes.** Reuse the mapped session and bump its
  last-activity stamp.
* **Conversation Reset (``/new``).** Create a NEW session and point the mapping
  at it. The abandoned session is **NOT deleted**: a reset discards CONTEXT, it
  does not destroy HISTORY. The old session stays retrievable by id, which is
  what makes "I asked it something an hour ago, what did it say" answerable
  after a reset. Nothing in this package can delete a session; the client does
  not expose the verb.
* **60-minute idle expiry.** If more than 60 minutes have passed since the last
  turn, the next turn transparently creates a new session. No error, no
  message: the user asks a question and gets an answer, just without the older
  context.

Those two - the explicit reset and the idle expiry, whichever comes first - are
the user's ONLY controls over how long the context (and therefore the per-turn
token cost) grows. That is a deliberate product decision, and it is the reason
the idle window is enforced here in the middle tier rather than left to the
service's own session TTL, which has a 24-hour minimum and would let a chatty
conversation accumulate a day of context.

NAMING HAZARD
-------------
The idle window is 3600 seconds. The workforce pool ``sessionDuration`` is also
3600 seconds. They are unrelated: one is our policy about conversational
context, the other is how long a federated Google credential stays valid. This
module knows nothing about credential lifetime - it receives a token per call
and uses it. If a token has expired the sessions call fails with
``IdentityUnavailable`` and the identity plane deals with it. Do not "fix" a
credential problem by touching the idle window, and never write "the session
expired" without saying which of the three sessions you mean.

SCOPE: ONE-TO-ONE CHATS ONLY
----------------------------
Group chats and channels are rejected, explicitly, with a typed error. The
reason is not effort: a group conversation has ONE conversation id and MANY
users, so a single mapped session would collect several people's turns under
one ``user_id``. That is a privacy failure (A's question becomes part of the
context B's answer is generated from) and an ADR 003 violation (the ``user_id``
would be whoever happened to speak first). Per-user sessions inside a shared
thread are a coherent design, but they need a product decision about what
"the conversation" even means when the bot can see other people's messages, so
the scope is closed until someone makes it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Callable, Protocol, runtime_checkable

from ..ports import PortError
from .client import SessionsClient, conversation_label
from .store import InMemorySessionStore, SessionRecord, SessionStore, mapping_key

logger = logging.getLogger(__name__)

#: 60 minutes. See the naming hazard above before changing it.
IDLE_TIMEOUT_SECONDS = 3600.0

#: ADR 003: ``entra:{tenant guid}:{object guid}``. Anything else is refused.
#: The GUID shape is enforced (not just "non-empty") because the user key ends
#: up as an immutable ``userId`` on a durable resource; a malformed one is not
#: repairable after the fact.
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_USER_KEY_RE = re.compile(rf"^entra:{_GUID}:{_GUID}$")

#: Teams conversation ids for group chats and channels are thread-shaped:
#: ``19:...@thread.v2``, ``19:...@thread.skype``, ``19:...@thread.tacv2``.
#: One-to-one chats are ``a:1<opaque>``. This is a BACKSTOP - callers should
#: pass ``conversation_type`` from ``activity.conversation.conversationType``,
#: which is authoritative.
_THREAD_CONVERSATION_RE = re.compile(r"(^19:)|(@thread\.)", re.IGNORECASE)

#: The only conversation type we serve.
_PERSONAL = "personal"


class SessionManagerError(PortError):
    """Base for session-manager refusals. All fail closed."""


class InvalidUserKey(SessionManagerError):
    """The user key is not ``entra:{tid}:{oid}`` (ADR 003).

    Raised in particular when the Entra object id is absent. There is no
    fallback to the Teams MRI: an MRI-keyed session is a second, permanent,
    unfederated identity for a person who already has one, their history
    splits, and nothing after the fact can map an MRI back to an object id to
    reconcile the two. Refusing the turn is recoverable; a split identity is
    not.
    """


class GroupConversationNotSupported(SessionManagerError):
    """A group chat or channel. Out of scope - see the module docstring."""


@runtime_checkable
class SessionManager(Protocol):
    """The interface other components code against."""

    async def resolve(
        self, *, user_key: str, conversation_id: str, access_token: str
    ) -> str: ...

    async def reset(
        self, *, user_key: str, conversation_id: str, access_token: str
    ) -> str: ...


def assert_one_to_one(conversation_id: str, conversation_type: str | None) -> None:
    """Refuse anything that is not a one-to-one chat.

    ``conversation_type`` wins when supplied: it comes straight from the
    activity and is authoritative. When it is absent we fall back to the shape
    of the id, which catches every group chat and channel Teams currently
    produces but is a heuristic and is documented as one.
    """
    if not conversation_id:
        raise GroupConversationNotSupported(
            "activity carried no conversation id, so the conversation cannot be "
            "confirmed to be one-to-one"
        )
    if conversation_type is not None:
        if conversation_type.lower() != _PERSONAL:
            raise GroupConversationNotSupported(
                f"conversationType={conversation_type!r} is out of scope; the bot "
                "serves one-to-one chats only, because one shared session for a "
                "multi-user thread would file several people's turns under one "
                "user_id"
            )
        return
    if _THREAD_CONVERSATION_RE.search(conversation_id):
        raise GroupConversationNotSupported(
            f"conversation id {conversation_id!r} is thread-shaped (group chat or "
            "channel); the bot serves one-to-one chats only"
        )


def assert_valid_user_key(user_key: str) -> None:
    """ADR 003 fail-closed check on the session ``user_id``."""
    if not user_key:
        raise InvalidUserKey("no user key: the turn has no Entra identity")
    if not _USER_KEY_RE.match(user_key):
        # Do NOT echo the value: a malformed key is frequently a Teams MRI, and
        # logging it invites someone to "just use it".
        raise InvalidUserKey(
            "user key is not 'entra:{tenant-guid}:{object-guid}' - most often "
            "this means the activity had no aadObjectId. Refusing the turn "
            "rather than keying a session on anything else (ADR 003)."
        )


class AgentRuntimeSessionManager:
    """The default :class:`SessionManager`.

    :param client: anything satisfying
        :class:`~app.sessions.client.SessionsClient`. In production the REST
        client; in tests a fake. There is deliberately no way to hand this a
        client that can delete or append.
    :param store: the mapping store. Defaults to the in-memory one, which is
        single-instance only - read :mod:`.store` before deploying.
    :param clock: seconds-valued monotonic clock, injectable so the idle policy
        is testable without sleeping for an hour. Defaults to
        :func:`time.monotonic`, which cannot jump backwards on an NTP
        correction the way wall time can. A monotonic clock is per-process, so
        a persistent store implementation must swap in a wall clock (and store
        UTC timestamps) rather than persisting these values as they are.
    :param idle_timeout_seconds: the 60-minute window.
    """

    def __init__(
        self,
        *,
        client: SessionsClient,
        store: SessionStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        idle_timeout_seconds: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._store: SessionStore = store or InMemorySessionStore()
        self._clock = clock
        self._idle_timeout = idle_timeout_seconds
        # One lock per (user, conversation). Without it, two turns arriving
        # together - a fast double-send, or Teams retrying a delivery - both
        # see an empty mapping, both create a session, and the second write
        # wins. The user then has an orphan session and a context that lost the
        # first message. The lock is per key, not global, so unrelated
        # conversations never wait on each other.
        self._locks: dict[str, asyncio.Lock] = {}
        # Guards creation of the per-key locks themselves. Cheap: held for a
        # dict lookup, never across an await that does I/O.
        self._locks_guard = asyncio.Lock()

    # -- public contract ---------------------------------------------------

    async def resolve(
        self, *, user_key: str, conversation_id: str, access_token: str
    ) -> str:
        """Return the session id for this turn, creating one if needed.

        :raises InvalidUserKey: no/malformed Entra object id (ADR 003).
        :raises GroupConversationNotSupported: group chat or channel.
        :raises IdentityUnavailable: the sessions API rejected the credential.
        :raises AuthorizationDenied: the sessions API refused the principal.
        :raises TransientBackendError: 5xx/timeout/quota. Retryable.
        """
        return await self._resolve(
            user_key=user_key,
            conversation_id=conversation_id,
            access_token=access_token,
            force_new=False,
        )

    async def reset(
        self, *, user_key: str, conversation_id: str, access_token: str
    ) -> str:
        """Conversation Reset: start a new session, abandon (never delete) the old.

        Raises the same set as :meth:`resolve`. If creation of the replacement
        fails, the OLD mapping is left intact and the error propagates: the
        user sees a failed ``/new`` and keeps their context, which is strictly
        better than losing the mapping to a session that still exists and then
        being unable to name it.
        """
        return await self._resolve(
            user_key=user_key,
            conversation_id=conversation_id,
            access_token=access_token,
            force_new=True,
        )

    # -- internals ---------------------------------------------------------

    async def _resolve(
        self,
        *,
        user_key: str,
        conversation_id: str,
        access_token: str,
        force_new: bool,
        conversation_type: str | None = None,
    ) -> str:
        assert_valid_user_key(user_key)
        assert_one_to_one(conversation_id, conversation_type)
        if not access_token:
            # ADR 002: the user's token or nothing. Checked here as well as in
            # the client so a fake client in a test cannot mask its absence.
            from ..ports import IdentityUnavailable  # local: avoid cycle noise

            raise IdentityUnavailable(
                "no user access token for the sessions call; the middle tier "
                "never falls back to a service account (ADR 002)"
            )

        key = mapping_key(user_key, conversation_id)
        async with await self._lock_for(key):
            now = self._clock()
            existing = await self._store.get(key)

            if existing is not None and not force_new:
                idle = now - existing.last_activity
                if idle < self._idle_timeout:
                    await self._store.touch(key, now)
                    return existing.session_id
                logger.info(
                    "agent runtime session %s idle for %.0fs (>= %.0fs); starting a "
                    "new one. The old session is retained, not deleted.",
                    existing.session_id,
                    idle,
                    self._idle_timeout,
                )

            session = await self._client.create_session(
                user_id=user_key,
                access_token=access_token,
                display_name=f"teams:{conversation_id}",
                labels={"teams_conversation": conversation_label(conversation_id)},
            )
            await self._store.put(
                key,
                SessionRecord(
                    user_key=user_key,
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    session_name=session.name,
                    created_at=now,
                    last_activity=now,
                ),
            )
            if existing is not None:
                logger.info(
                    "conversation now on agent runtime session %s; previous session "
                    "%s abandoned and RETAINED (reset=%s)",
                    session.session_id,
                    existing.session_id,
                    force_new,
                )
            return session.session_id

    async def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is not None:
            return lock
        async with self._locks_guard:
            # Re-check: another coroutine may have created it while we awaited
            # the guard.
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    # NOTE: there is deliberately no ``append_event``, no ``add_message`` and
    # no ``delete`` on this class.
    #
    #   * No append: ADR 005 gives history writes to the runtime. The runtime
    #     appends the user content, tool calls, tool results and model output
    #     with the invocation ids the ADK expects. A middle tier that also
    #     appends produces duplicated turns and events with invocation ids
    #     nothing issued - history that silently stops replaying correctly,
    #     with the damage showing up turns later. The absence of the method is
    #     the enforcement.
    #   * No delete: a Conversation Reset abandons a session, it does not
    #     destroy it. The old session must stay retrievable by id.
    #
    # If you need either, that is an ADR amendment, not a patch.


__all__ = [
    "AgentRuntimeSessionManager",
    "GroupConversationNotSupported",
    "IDLE_TIMEOUT_SECONDS",
    "InvalidUserKey",
    "SessionManager",
    "SessionManagerError",
    "assert_one_to_one",
    "assert_valid_user_key",
]
