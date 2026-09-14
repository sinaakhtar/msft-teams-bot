"""Per-user Google-token cache: single-flight refresh, proactive, in memory only.

KEYED ON THE ENTRA OBJECT ID. NOT THE CONVERSATION. NOT THE TEAMS MRI.
-----------------------------------------------------------------------
The cache key is derived strictly from ``entra:{tid}:{oid}`` (ADR 003). The two
tempting alternatives are both wrong in ways that only show up in production:

* **Conversation id** - one person in five conversations gets five token
  chains and five times the STS traffic; worse, a group chat is ONE
  conversation with MANY people in it, so a conversation-keyed cache hands
  Bob's credential to Alice. That is the cross-user leak the whole design
  exists to prevent.
* **Teams MRI** (``from.id``, ``29:1a2b...``) - opaque, per-channel, and not
  the identifier Google ends up authorizing. Two identifiers for one human
  means two cache entries, two refresh timers, and a debugging session that
  starts by asking which of the two is real.

The tenant id is kept in the key as well. Object ids are unique inside a
tenant; qualifying by tenant means a future multi-tenant deployment cannot
produce a collision that would be, again, a cross-user token leak.

PROACTIVE REFRESH, NOT REFRESH-ON-401
--------------------------------------
Refresh fires at ``refresh_ratio`` (default 0.8) of the token's lifetime -
about 48 minutes into a ~1 hour Workforce credential. Waiting for a 401 means
every user's first request after expiry is a failed downstream call, which is
both a latency spike and a log full of authorization errors that look like a
permissions problem and are not.

ONE REFRESH PER USER, NOT N
----------------------------
A per-key ``asyncio.Lock`` plus a re-check inside the lock. Ten concurrent
turns for one user produce exactly one OBO+STS round trip; the other nine
wait on the lock, re-read the freshly stored entry and return it. Without the
re-check the lock would only serialize the stampede rather than collapse it -
ten sequential refreshes instead of ten parallel ones, which is worse.

NEVER PERSISTED
---------------
In-process memory only. Not written to disk, not logged, and above all NOT
placed in Agent Runtime Session state: that state is PERSISTED, so a token
put there becomes a live bearer credential sitting in durable conversation
history with whatever retention the platform applies. The cache dies with the
process, which is correct - a Cloud Run instance restart should cost one STS
round trip per active user, nothing more.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..logging_utils import fingerprint, log_event
from .errors import IdentityAcquisitionError

_log = logging.getLogger(__name__)

#: Injectable monotonic clock. Tests pass a fake; production passes
#: ``time.monotonic``. Deliberately NOT ``time.time``: a wall-clock step (NTP
#: correction, VM live-migration) must not make a cached token look valid for
#: another hour.
Clock = Callable[[], float]

#: Mints a brand new credential for one user. Returns (token, lifetime_seconds).
TokenMinter = Callable[[], Awaitable["MintedToken"]]

#: Below this many seconds remaining, a token is treated as unusable: a
#: downstream call could plausibly take longer than that to be authorized.
DEFAULT_MIN_REMAINING_SECONDS = 60.0

DEFAULT_MAX_ENTRIES = 5000
DEFAULT_REFRESH_RATIO = 0.8


@dataclass(frozen=True)
class MintedToken:
    """What a token source returns. ``lifetime_seconds`` is STS ``expires_in``."""

    token: str
    lifetime_seconds: float

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # An accidental repr() of a cache entry in a traceback must not leak
        # the credential.
        return f"MintedToken(token=[REDACTED:{fingerprint(self.token)}], lifetime_seconds={self.lifetime_seconds})"


@dataclass
class _Entry:
    token: str
    issued_at: float
    expires_at: float
    refresh_at: float

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"_Entry(token=[REDACTED:{fingerprint(self.token)}], "
            f"issued_at={self.issued_at}, expires_at={self.expires_at}, "
            f"refresh_at={self.refresh_at})"
        )


@dataclass
class CacheStats:
    """Counters for the health endpoint. No token material, ever."""

    hits: int = 0
    #: Served from cache while a refresh was already due but the entry was
    #: still comfortably valid - should be ~0 if refresh is working.
    misses: int = 0
    refreshes: int = 0
    #: Callers that waited on another coroutine's refresh and then found a
    #: fresh entry. This is the number the single-flight design exists to
    #: make large.
    coalesced: int = 0
    evictions: int = 0
    #: Refresh failed but the existing credential was still valid, so the
    #: user's own unexpired token was served rather than failing the turn.
    stale_served: int = 0


class _KeyLock:
    """An ``asyncio.Lock`` with a waiter count, so it can be reaped safely.

    Leaving one Lock object per user in a dict forever is a slow leak in a
    long-lived Cloud Run instance. Deleting it the moment it is released races
    with a coroutine that is about to await it and would let two refreshes
    through. The refcount closes that window without touching Lock internals.
    """

    __slots__ = ("lock", "waiters")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.waiters = 0


class PerUserTokenCache:
    """Bounded, single-flight, proactively refreshed per-user token cache.

    :param clock: monotonic seconds source. Injected so tests can advance
        time instantly instead of sleeping.
    :param max_entries: LRU bound. At 5000 entries of a ~1.5KB token this is
        single-digit megabytes; the bound exists so a burst of one-off users
        cannot grow the process without limit.
    :param refresh_ratio: fraction of lifetime after which a refresh is due.
    :param min_remaining_seconds: hard floor. An entry with less than this
        left is unusable regardless of ``refresh_ratio``.
    :param serve_stale_on_refresh_failure: if a refresh fails while the
        CURRENT token is still genuinely valid, serve the current token. This
        is not a fallback credential and does not weaken ADR 004: it is the
        same user's own unexpired token, minted by the same chain, still
        inside its own validity window. When there is no valid token the
        error propagates and the turn is refused.
    """

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        refresh_ratio: float = DEFAULT_REFRESH_RATIO,
        min_remaining_seconds: float = DEFAULT_MIN_REMAINING_SECONDS,
        serve_stale_on_refresh_failure: bool = True,
    ) -> None:
        if not 0.0 < refresh_ratio <= 1.0:
            raise ValueError("refresh_ratio must be in (0, 1]")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._clock = clock
        self._max_entries = max_entries
        self._refresh_ratio = refresh_ratio
        self._min_remaining = min_remaining_seconds
        self._serve_stale = serve_stale_on_refresh_failure
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._locks: dict[str, _KeyLock] = {}
        self.stats = CacheStats()

    # -- public API ------------------------------------------------------

    async def get_or_mint(self, key: str, mint: TokenMinter) -> str:
        """Return a usable token for ``key``, minting or refreshing if needed.

        Exactly one ``mint`` call happens per key per refresh, no matter how
        many coroutines call this concurrently.

        :raises IdentityAcquisitionError: propagated verbatim from ``mint``
            when no usable cached credential exists. Never swallowed, never
            replaced with a fallback.
        """
        now = self._clock()

        # Fast path: no lock, no await. The overwhelming majority of turns.
        entry = self._entries.get(key)
        if entry is not None and self._is_fresh(entry, now):
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return entry.token

        keylock = self._acquire_keylock(key)
        try:
            async with keylock.lock:
                now = self._clock()
                # Re-check: while we were queued, the coroutine ahead of us
                # very likely did the refresh already. This is what turns N
                # refreshes into 1.
                entry = self._entries.get(key)
                if entry is not None and self._is_fresh(entry, now):
                    self._entries.move_to_end(key)
                    self.stats.coalesced += 1
                    return entry.token

                self.stats.misses += 1
                try:
                    minted = await mint()
                except IdentityAcquisitionError as exc:
                    survivor = self._usable_but_stale(key, self._clock())
                    if survivor is not None and self._serve_stale:
                        # The user's own token, still inside its validity
                        # window. Not a fallback identity - the same identity.
                        self.stats.stale_served += 1
                        # `as_log_fields()` already carries `user_key`, so
                        # merge rather than splat both in.
                        fields = dict(exc.as_log_fields())
                        fields.update(
                            user_key=key,
                            token_fp=fingerprint(survivor.token),
                            seconds_remaining=round(survivor.expires_at - self._clock(), 1),
                        )
                        log_event(
                            _log,
                            logging.WARNING,
                            "identity.cache.refresh_failed_serving_valid_token",
                            **fields,
                        )
                        return survivor.token
                    # Nothing usable. Fail closed: the turn is refused.
                    self._entries.pop(key, None)
                    raise

                stored = self._store(key, minted, self._clock())
                self.stats.refreshes += 1
                log_event(
                    _log,
                    logging.INFO,
                    "identity.cache.minted",
                    user_key=key,
                    token_fp=fingerprint(stored.token),
                    lifetime_seconds=round(stored.expires_at - stored.issued_at, 1),
                    refresh_in_seconds=round(stored.refresh_at - stored.issued_at, 1),
                    entries=len(self._entries),
                )
                return stored.token
        finally:
            self._release_keylock(key, keylock)

    def invalidate(self, key: str) -> bool:
        """Drop one user's cached token. Used when a downstream 401 proves the
        credential is dead earlier than its ``exp`` claimed."""
        return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        """Drop everything. The only supported way to 'export' the cache."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def snapshot(self) -> list[dict[str, object]]:
        """Debug view: fingerprints and timings only. No tokens.

        Deliberately the only read path other than :meth:`get_or_mint`, so
        there is no accessor a well-meaning future handler can call to dump
        credentials into a diagnostics endpoint.
        """
        now = self._clock()
        return [
            {
                "user_key": key,
                "token_fp": fingerprint(entry.token),
                "seconds_remaining": round(entry.expires_at - now, 1),
                "refresh_due_in": round(entry.refresh_at - now, 1),
            }
            for key, entry in self._entries.items()
        ]

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"PerUserTokenCache(entries={len(self._entries)}, max={self._max_entries})"

    # -- internals -------------------------------------------------------

    def _is_fresh(self, entry: _Entry, now: float) -> bool:
        """Fresh = refresh not yet due AND comfortably inside validity."""
        return now < entry.refresh_at and (entry.expires_at - now) > self._min_remaining

    def _usable_but_stale(self, key: str, now: float) -> _Entry | None:
        """An entry past its refresh point but still genuinely valid."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if (entry.expires_at - now) > self._min_remaining:
            return entry
        return None

    def _store(self, key: str, minted: MintedToken, now: float) -> _Entry:
        lifetime = max(float(minted.lifetime_seconds), 0.0)
        entry = _Entry(
            token=minted.token,
            issued_at=now,
            expires_at=now + lifetime,
            refresh_at=now + lifetime * self._refresh_ratio,
        )
        self._entries[key] = entry
        self._entries.move_to_end(key)
        self._evict_if_needed()
        return entry

    def _evict_if_needed(self) -> None:
        """LRU eviction. Evicting a live token costs one STS round trip on
        that user's next turn; it does not break anything."""
        while len(self._entries) > self._max_entries:
            evicted_key, _ = self._entries.popitem(last=False)
            self.stats.evictions += 1
            log_event(
                _log,
                logging.INFO,
                "identity.cache.evicted",
                user_key=evicted_key,
                entries=len(self._entries),
            )

    def _acquire_keylock(self, key: str) -> _KeyLock:
        keylock = self._locks.get(key)
        if keylock is None:
            keylock = _KeyLock()
            self._locks[key] = keylock
        keylock.waiters += 1
        return keylock

    def _release_keylock(self, key: str, keylock: _KeyLock) -> None:
        keylock.waiters -= 1
        if keylock.waiters <= 0 and self._locks.get(key) is keylock:
            del self._locks[key]


__all__ = [
    "PerUserTokenCache",
    "MintedToken",
    "CacheStats",
    "Clock",
    "TokenMinter",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_REFRESH_RATIO",
    "DEFAULT_MIN_REMAINING_SECONDS",
]
