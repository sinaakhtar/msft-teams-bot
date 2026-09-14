"""The (user, conversation) -> Agent Runtime Session mapping.

WHAT THIS IS
------------
One row per active Teams conversation:

    (user_key, conversation_id)  ->  session id + last activity timestamp

That is the entire state the middle tier keeps between turns. Everything else
- the conversation history itself - lives in the managed Sessions service,
which is the point of ADR 005.

THE LIMITATION, STATED PLAINLY
------------------------------
The default implementation here is IN-MEMORY, and Cloud Run runs N instances.
Two consequences, neither of them theoretical:

  * **Instance fan-out.** Turn 1 lands on instance A and creates session S.
    Turn 2 lands on instance B, which has never heard of S, so it creates
    session T. The user's next question is answered with no memory of the
    previous one, intermittently, in a way that looks like the model
    forgetting rather than like a bug. With min-instances=1 and
    max-instances=1 this cannot happen; with any autoscaling it will.
  * **Cold start / redeploy.** A new revision starts with an empty map, so
    every conversation silently begins a new session. The abandoned sessions
    are not deleted (nothing here deletes), so nothing is lost - but context
    is dropped without the user asking for it, which is exactly what the
    60-minute idle policy is supposed to be the only unrequested cause of.

So: **the in-memory store is correct for a single-instance demo and wrong for
production.** It is the default because it has no infrastructure dependency and
because the interface below is the part that matters; swapping the
implementation is a one-line change at construction.

PRODUCTION OPTIONS
------------------
1. **Firestore (Native mode).** One document per ``(user_key,
   conversation_id)``, keyed by a hash of the pair, holding the session id and
   the last-activity timestamp. Serverless, no VPC connector, no capacity to
   size, ~single-digit-ms reads in-region, and a TTL policy can expire rows
   automatically so the map does not grow forever. Costs one read and one
   write per turn, which is noise next to a model call.
2. **Redis (Memorystore).** Fastest, and ``SET key value NX`` gives a
   distributed lock so two instances cannot create two sessions for one
   conversation at the same instant - the multi-instance version of the
   per-key async lock in :mod:`.manager`. Costs a always-on instance and a
   Serverless VPC Access connector, which is real money and real setup for a
   bot whose traffic is bursty and small.
3. **Reconstruct by listing sessions.** Keep nothing. On each turn call
   ``sessions.list`` filtered by ``user_id="entra:{tid}:{oid}"`` and
   ``labels.teams_conversation="<hash>"`` (the client sets that label on
   create, precisely so this option stays open), take the most recently
   updated session, and use it. No storage at all, and it self-heals across
   restarts. But it adds a list call to the front of every turn, it cannot
   express "this session was reset" without a second signal, and it races: two
   simultaneous first turns both list nothing and both create.

**Recommendation: Firestore.** Option 3 is a genuinely appealing "no state"
story until you try to express a reset in it, and option 2 buys latency we do
not need at the price of infrastructure we would otherwise not run. Firestore
is the only one of the three that is both durable and free of standing cost.
Take option 2 only if a distributed lock turns out to be needed for real
concurrent traffic, and use option 3 as a fallback path for reconstructing a
mapping Firestore has lost rather than as the primary store.

Until then, the honest deployment posture is ``--min-instances=1
--max-instances=1``, and it should be written into the deploy config rather
than remembered.

WHAT A REPLACEMENT MUST PRESERVE
--------------------------------
  * ``get`` returning ``None`` for an unknown key, never raising.
  * ``put`` being last-writer-wins on the whole record.
  * ``touch`` being cheap: it is called on every single turn.
  * ``discard`` removing the MAPPING ONLY. It must never delete the Agent
    Runtime Session, which stays retrievable by id forever (see the reset
    semantics in :mod:`.manager`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable


def mapping_key(user_key: str, conversation_id: str) -> str:
    """The composite key.

    ``user_key`` is ``entra:{tid}:{oid}`` and both components are validated
    GUIDs upstream, so neither can contain the ``|`` separator and no pair of
    distinct inputs can collide on one key.
    """
    return f"{user_key}|{conversation_id}"


@dataclass(frozen=True)
class SessionRecord:
    """One mapping row.

    :param session_id: the bare Agent Runtime Session id (last path component).
    :param session_name: the full resource name, kept so a replacement store
        does not have to re-derive it from configuration that may have changed.
    :param last_activity: monotonic-ish seconds from the manager's clock, used
        only for the 60-minute idle policy. It is NOT the service-side session
        expiry (minimum 24h, owned by the Sessions service) and NOT the
        workforce credential lifetime.
    """

    user_key: str
    conversation_id: str
    session_id: str
    session_name: str
    created_at: float
    last_activity: float

    def with_activity(self, now: float) -> "SessionRecord":
        return replace(self, last_activity=now)


@runtime_checkable
class SessionStore(Protocol):
    """The mapping store seam. Deliberately four methods."""

    async def get(self, key: str) -> SessionRecord | None: ...

    async def put(self, key: str, record: SessionRecord) -> None: ...

    async def touch(self, key: str, now: float) -> None: ...

    async def discard(self, key: str) -> None: ...


class InMemorySessionStore:
    """Process-local dict. Correct on one instance, broken on N. See above.

    No lock: every mutation is a single dict operation, and the manager already
    serialises per key. ``asyncio`` gives us atomicity between awaits, and
    there are none here.
    """

    def __init__(self) -> None:
        self._rows: dict[str, SessionRecord] = {}

    async def get(self, key: str) -> SessionRecord | None:
        return self._rows.get(key)

    async def put(self, key: str, record: SessionRecord) -> None:
        self._rows[key] = record

    async def touch(self, key: str, now: float) -> None:
        row = self._rows.get(key)
        if row is not None:
            self._rows[key] = row.with_activity(now)

    async def discard(self, key: str) -> None:
        """Forget the MAPPING. Does not touch the Agent Runtime Session."""
        self._rows.pop(key, None)

    # Introspection for tests and for a /debug endpoint. Not part of the
    # Protocol, so no replacement store is obliged to implement it.
    def snapshot(self) -> dict[str, SessionRecord]:
        return dict(self._rows)


__all__ = [
    "InMemorySessionStore",
    "SessionRecord",
    "SessionStore",
    "mapping_key",
]
