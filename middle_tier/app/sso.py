"""State that has to live between two separate Teams activities.

Teams SSO is not one request. The user's assertion arrives on an ``invoke``
(``signin/tokenExchange``); the turn that needs it is a ``message`` that
arrived earlier and will not be sent again. Something has to hold the gap, and
that something is this module.

Three separate things are remembered, each with its own lifetime:

``assertions``
    The Teams SSO token itself, keyed on the ADR 003 user key. This is what
    :class:`app.ports.IdentityBroker` consumes as the OBO user assertion.

``pending_turns``
    The message the user actually typed, parked when we discovered there was
    no token and replayed once there is one. Without this the first thing
    anybody says to the bot is silently swallowed: they type a question, Teams
    performs a sign-in they never see, and nothing answers.

``exchanges``
    Exchange ids already redeemed. Teams sends the same ``signin/tokenExchange``
    to every active instance of a bot, and an assertion is single-use, so
    without this two concurrent redemptions race and the loser gets a replay
    error that looks exactly like a broken configuration.


THE SCALING CONSTRAINT, STATED PLAINLY
--------------------------------------
This is in-memory and therefore per-instance. If the ``invoke`` lands on one
Cloud Run instance and the following ``message`` lands on another, the second
instance sees no assertion and prompts for sign-in again, which from the
user's seat is an infinite sign-in loop.

The deployment pins ``--max-instances=1`` for exactly this reason. That is a
demo-grade answer, not an architectural one, and it is a deliberate trade: the
honest alternative is a shared store (Firestore, Redis) and that is a larger
change than the bug being fixed here.

Note that :class:`app.identity.cache.PerUserTokenCache` already had this same
property before this module existed. Pinning the instance count fixes both at
once; unpinning it reintroduces both.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Generic, Mapping, TypeVar

T = TypeVar("T")

#: Teams assertions are short-lived. Holding one much beyond an hour is
#: pointless: it will be rejected downstream and the refusal path will ask
#: for a fresh one anyway.
DEFAULT_ASSERTION_TTL_SECONDS = 50 * 60.0

#: A parked turn is only interesting for as long as the user is still waiting
#: for it. Replaying a question from an hour ago, unprompted, is worse than
#: dropping it.
DEFAULT_PENDING_TTL_SECONDS = 5 * 60.0

#: Long enough to cover the concurrent redemptions Teams fans out, short
#: enough that the set cannot grow without bound.
DEFAULT_EXCHANGE_TTL_SECONDS = 10 * 60.0

#: A ceiling so a hostile or looping client cannot exhaust memory. Eviction is
#: oldest-first, which is the right bias: the newest assertion is the one a
#: user is actively waiting on.
DEFAULT_MAX_ENTRIES = 10_000


class TtlStore(Generic[T]):
    """A small in-memory map with per-entry expiry and a bounded size.

    Deliberately not an LRU: entries expire on wall time because the thing
    being stored (a token, a parked turn) has a real-world lifetime that has
    nothing to do with how recently it was read.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: dict[str, tuple[float, T]] = {}

    def put(self, key: str, value: T) -> None:
        self._evict_expired()
        if len(self._entries) >= self._max and key not in self._entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
        self._entries[key] = (self._clock() + self._ttl, value)

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def take(self, key: str) -> T | None:
        """Read and remove. For values that must be consumed exactly once."""
        value = self.get(key)
        if value is not None:
            self._entries.pop(key, None)
        return value

    def forget(self, key: str) -> None:
        self._entries.pop(key, None)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        self._evict_expired()
        return len(self._entries)

    def _evict_expired(self) -> None:
        now = self._clock()
        for key in [k for k, (exp, _) in self._entries.items() if now >= exp]:
            del self._entries[key]


class SsoState:
    """The three things the SSO handshake has to remember. See module docs."""

    def __init__(
        self,
        *,
        assertion_ttl_seconds: float = DEFAULT_ASSERTION_TTL_SECONDS,
        pending_ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS,
        exchange_ttl_seconds: float = DEFAULT_EXCHANGE_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._assertions: TtlStore[str] = TtlStore(
            ttl_seconds=assertion_ttl_seconds, max_entries=max_entries, clock=clock
        )
        self._pending: TtlStore[Mapping[str, Any]] = TtlStore(
            ttl_seconds=pending_ttl_seconds, max_entries=max_entries, clock=clock
        )
        self._exchanges: TtlStore[bool] = TtlStore(
            ttl_seconds=exchange_ttl_seconds, max_entries=max_entries, clock=clock
        )

    # --- the user's Teams assertion ----------------------------------------

    def remember_assertion(self, user_key: str, assertion: str) -> None:
        if not user_key:
            raise ValueError("refusing to key an assertion on an empty user_key")
        self._assertions.put(user_key, assertion)

    def assertion_for(self, user_key: str) -> str | None:
        return self._assertions.get(user_key)

    def forget_assertion(self, user_key: str) -> None:
        """Drop a rejected assertion so the next turn re-prompts.

        Called when a downstream system refuses the derived credential. Keeping
        it would replay a known-bad token on every subsequent turn.
        """
        self._assertions.forget(user_key)

    # --- the turn the user is still waiting for ----------------------------

    def park_turn(self, user_key: str, activity: Mapping[str, Any]) -> None:
        self._pending.put(user_key, dict(activity))

    def take_parked_turn(self, user_key: str) -> Mapping[str, Any] | None:
        """Consume the parked turn. Exactly once: a replay loop is worse than
        a dropped message, so this can never hand the same turn out twice."""
        return self._pending.take(user_key)

    # --- de-duplication of the exchange itself -----------------------------

    def claim_exchange(self, exchange_id: str) -> bool:
        """Claim an exchange id. True if we won it, False if already redeemed.

        The caller must treat False as "another worker is handling this",
        NOT as an error: the correct response to a duplicate exchange is a
        quiet 200, because the exchange really is being dealt with.
        """
        if not exchange_id:
            # No id to dedup on. Let it through rather than refuse a turn.
            return True
        if exchange_id in self._exchanges:
            return False
        self._exchanges.put(exchange_id, True)
        return True


__all__ = [
    "SsoState",
    "TtlStore",
    "DEFAULT_ASSERTION_TTL_SECONDS",
    "DEFAULT_PENDING_TTL_SECONDS",
    "DEFAULT_EXCHANGE_TTL_SECONDS",
    "DEFAULT_MAX_ENTRIES",
]
