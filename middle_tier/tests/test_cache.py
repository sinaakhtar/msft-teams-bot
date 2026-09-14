"""Real tests for the per-user token cache. Fake clock, fake token source, no network.

WHAT IS FAKED AND WHY THAT IS HONEST
------------------------------------
Two things are substituted and only two: the clock, and the thing that mints
tokens. Both are injected constructor parameters of the production class, not
monkeypatched internals, so the code path under test is the production code
path. Nothing inside :class:`PerUserTokenCache` is patched, stubbed or
mocked - the locking, the freshness arithmetic, the LRU eviction and the
failure handling are the real implementations.

Faking the clock is the point rather than a shortcut: the behaviour under test
is "refresh fires at 48 minutes into a 60 minute lifetime", and a test that
proved that by sleeping would take an hour.

The concurrency tests use real ``asyncio`` tasks and a minter that actually
awaits, so the stampede is real: without the per-key lock and the re-check
inside it, ``test_concurrent_requests_for_one_user_cause_exactly_one_refresh``
fails with ``refresh calls == 25``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.identity.cache import (  # noqa: E402
    MintedToken,
    PerUserTokenCache,
)
from app.identity.errors import StsTransientError  # noqa: E402

HOUR = 3600.0

USER_A = "entra:00000000-0000-0000-0000-000000000000:33333333-3333-3333-3333-333333333333"
USER_B = "entra:00000000-0000-0000-0000-000000000000:99999999-8888-7777-6666-555555555555"


class FakeClock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingMinter:
    """A token source that counts calls and can be made slow or broken.

    ``delay`` uses ``asyncio.sleep(0)`` yields rather than real time, so a
    concurrent stampede genuinely interleaves without the test being slow.
    """

    def __init__(self, *, prefix: str = "tok", lifetime: float = HOUR, yields: int = 3) -> None:
        self.prefix = prefix
        self.lifetime = lifetime
        self.yields = yields
        self.calls = 0
        self.fail_with: Exception | None = None

    async def __call__(self) -> MintedToken:
        self.calls += 1
        serial = self.calls
        for _ in range(self.yields):
            # Give the event loop a chance to schedule the other waiters.
            # Without the cache's lock this is exactly where they all pile in.
            await asyncio.sleep(0)
        if self.fail_with is not None:
            raise self.fail_with
        return MintedToken(token=f"{self.prefix}-{serial}", lifetime_seconds=self.lifetime)


# --------------------------------------------------------------------------
# Proactive refresh
# --------------------------------------------------------------------------


async def test_token_is_reused_before_the_refresh_point() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, refresh_ratio=0.8)
    minter = CountingMinter()

    first = await cache.get_or_mint(USER_A, minter)
    assert first == "tok-1"
    assert minter.calls == 1

    # 47 minutes in: 78% of the lifetime. Not due yet.
    clock.advance(0.78 * HOUR)
    again = await cache.get_or_mint(USER_A, minter)

    assert again == "tok-1"
    assert minter.calls == 1, "a token inside its refresh window must not be re-minted"


async def test_refresh_fires_proactively_before_expiry() -> None:
    """The whole point: refresh at ~80% of lifetime, NOT on a 401 at 100%."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, refresh_ratio=0.8)
    minter = CountingMinter()

    assert await cache.get_or_mint(USER_A, minter) == "tok-1"

    # 48m36s in: 81% of a 3600s lifetime. 684 seconds of validity REMAIN, so
    # nothing has expired and no downstream call has failed - and yet a
    # refresh must already have happened.
    clock.advance(0.81 * HOUR)
    refreshed = await cache.get_or_mint(USER_A, minter)

    assert refreshed == "tok-2"
    assert minter.calls == 2
    seconds_of_validity_left_on_the_old_token = HOUR - 0.81 * HOUR
    assert seconds_of_validity_left_on_the_old_token > 600, (
        "sanity: this test is only meaningful if the old token was still valid"
    )


async def test_expired_token_is_replaced() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter = CountingMinter()

    await cache.get_or_mint(USER_A, minter)
    clock.advance(2 * HOUR)

    assert await cache.get_or_mint(USER_A, minter) == "tok-2"
    assert minter.calls == 2


async def test_token_with_less_than_the_floor_remaining_is_not_served() -> None:
    """A 30-second-old-enough token is unusable even if refresh_ratio says fine.

    With ratio 1.0 the refresh point is expiry itself, so only the hard floor
    stands between us and handing out a credential that dies mid-request.
    """
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, refresh_ratio=1.0, min_remaining_seconds=60.0)
    minter = CountingMinter()

    await cache.get_or_mint(USER_A, minter)
    clock.advance(HOUR - 30.0)  # 30 seconds of validity left

    assert await cache.get_or_mint(USER_A, minter) == "tok-2"
    assert minter.calls == 2


# --------------------------------------------------------------------------
# Single-flight
# --------------------------------------------------------------------------


async def test_concurrent_requests_for_one_user_cause_exactly_one_refresh() -> None:
    """25 simultaneous cold-cache turns for one human => ONE OBO+STS chain.

    This is the test the per-key lock exists for. Remove the lock and this
    reports 25; remove only the re-check inside the lock and it still reports
    25, just serially.
    """
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter = CountingMinter()

    results = await asyncio.gather(*[cache.get_or_mint(USER_A, minter) for _ in range(25)])

    assert minter.calls == 1, f"expected exactly 1 refresh, got {minter.calls}"
    assert set(results) == {"tok-1"}, "every caller must get the same token"
    assert cache.stats.refreshes == 1
    assert cache.stats.coalesced == 24, "24 callers should have waited and reused"


async def test_concurrent_refresh_at_the_refresh_point_is_also_single_flight() -> None:
    """The stampede that actually happens in production is a warm one."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, refresh_ratio=0.8)
    minter = CountingMinter()

    await cache.get_or_mint(USER_A, minter)
    assert minter.calls == 1

    clock.advance(0.85 * HOUR)  # refresh due, token still valid
    results = await asyncio.gather(*[cache.get_or_mint(USER_A, minter) for _ in range(12)])

    assert minter.calls == 2, f"one refresh expected, got {minter.calls - 1}"
    assert set(results) == {"tok-2"}


async def test_concurrent_requests_for_different_users_each_refresh_once() -> None:
    """Single-flight must be per user, not global. Serializing all users
    behind one lock would be correct and unusably slow."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter_a = CountingMinter(prefix="a")
    minter_b = CountingMinter(prefix="b")

    await asyncio.gather(
        *[cache.get_or_mint(USER_A, minter_a) for _ in range(8)],
        *[cache.get_or_mint(USER_B, minter_b) for _ in range(8)],
    )

    assert minter_a.calls == 1
    assert minter_b.calls == 1


async def test_lock_table_does_not_leak() -> None:
    """Per-key locks are reaped, or a long-lived instance grows forever."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter = CountingMinter()

    await asyncio.gather(*[cache.get_or_mint(USER_A, minter) for _ in range(10)])
    await cache.get_or_mint(USER_B, CountingMinter(prefix="b"))

    assert cache._locks == {}, "locks must be released once no coroutine holds or awaits them"


# --------------------------------------------------------------------------
# Isolation between users
# --------------------------------------------------------------------------


async def test_different_users_never_share_a_token() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter_a = CountingMinter(prefix="alice")
    minter_b = CountingMinter(prefix="bob")

    token_a = await cache.get_or_mint(USER_A, minter_a)
    token_b = await cache.get_or_mint(USER_B, minter_b)

    assert token_a == "alice-1"
    assert token_b == "bob-1"
    assert token_a != token_b

    # And re-reads stay pinned to the right human.
    assert await cache.get_or_mint(USER_A, minter_a) == "alice-1"
    assert await cache.get_or_mint(USER_B, minter_b) == "bob-1"
    assert minter_a.calls == 1
    assert minter_b.calls == 1


async def test_one_users_refresh_does_not_disturb_another() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, refresh_ratio=0.8)
    minter_a = CountingMinter(prefix="alice")
    minter_b = CountingMinter(prefix="bob", lifetime=2 * HOUR)

    await cache.get_or_mint(USER_A, minter_a)
    await cache.get_or_mint(USER_B, minter_b)

    clock.advance(0.9 * HOUR)  # A is due, B (2h lifetime) is not

    assert await cache.get_or_mint(USER_A, minter_a) == "alice-2"
    assert await cache.get_or_mint(USER_B, minter_b) == "bob-1"
    assert minter_b.calls == 1


async def test_invalidate_affects_only_the_named_user() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter_a = CountingMinter(prefix="alice")
    minter_b = CountingMinter(prefix="bob")

    await cache.get_or_mint(USER_A, minter_a)
    await cache.get_or_mint(USER_B, minter_b)

    assert cache.invalidate(USER_A) is True
    assert USER_A not in cache
    assert USER_B in cache

    assert await cache.get_or_mint(USER_A, minter_a) == "alice-2"
    assert await cache.get_or_mint(USER_B, minter_b) == "bob-1"


# --------------------------------------------------------------------------
# Bounded size / eviction
# --------------------------------------------------------------------------


async def test_cache_is_bounded_and_evicts_least_recently_used() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, max_entries=3)

    keys = [f"entra:00000000-0000-0000-0000-000000000000:0000000{n}-0000-0000-0000-000000000000" for n in range(4)]
    minters = [CountingMinter(prefix=f"u{n}") for n in range(4)]

    for key, minter in zip(keys[:3], minters[:3]):
        await cache.get_or_mint(key, minter)
    assert len(cache) == 3

    # Touch key 0 so key 1 becomes the least recently used.
    await cache.get_or_mint(keys[0], minters[0])

    await cache.get_or_mint(keys[3], minters[3])

    assert len(cache) == 3, "the bound must hold"
    assert keys[1] not in cache, "least recently used entry should have gone"
    assert keys[0] in cache and keys[2] in cache and keys[3] in cache
    assert cache.stats.evictions == 1


async def test_evicted_user_simply_re_mints() -> None:
    """Eviction must be a performance event, never a correctness one."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, max_entries=1)
    minter_a = CountingMinter(prefix="alice")
    minter_b = CountingMinter(prefix="bob")

    await cache.get_or_mint(USER_A, minter_a)
    await cache.get_or_mint(USER_B, minter_b)  # evicts A
    assert USER_A not in cache

    assert await cache.get_or_mint(USER_A, minter_a) == "alice-2"
    assert minter_a.calls == 2


# --------------------------------------------------------------------------
# Failure behaviour
# --------------------------------------------------------------------------


async def test_mint_failure_on_a_cold_cache_propagates() -> None:
    """No cached credential, minting failed => the error escapes. Fail closed."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter = CountingMinter()
    minter.fail_with = StsTransientError("STS returned 503")

    with pytest.raises(StsTransientError):
        await cache.get_or_mint(USER_A, minter)

    assert USER_A not in cache


async def test_refresh_failure_serves_the_users_own_still_valid_token() -> None:
    """Not a fallback identity: the same human's own unexpired credential."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock, refresh_ratio=0.8)
    minter = CountingMinter()

    assert await cache.get_or_mint(USER_A, minter) == "tok-1"

    clock.advance(0.85 * HOUR)  # refresh due; ~540s of validity left
    minter.fail_with = StsTransientError("STS returned 503")

    assert await cache.get_or_mint(USER_A, minter) == "tok-1"
    assert cache.stats.stale_served == 1


async def test_refresh_failure_past_expiry_raises_rather_than_serving_a_dead_token() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter = CountingMinter()

    await cache.get_or_mint(USER_A, minter)
    clock.advance(2 * HOUR)
    minter.fail_with = StsTransientError("STS returned 503")

    with pytest.raises(StsTransientError):
        await cache.get_or_mint(USER_A, minter)
    assert USER_A not in cache, "an unusable entry must not linger"


async def test_concurrent_callers_all_see_the_failure() -> None:
    """A failed single-flight refresh must fail every waiter, not just one."""
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    minter = CountingMinter()
    minter.fail_with = StsTransientError("STS returned 503")

    results = await asyncio.gather(
        *[cache.get_or_mint(USER_A, minter) for _ in range(5)], return_exceptions=True
    )

    assert all(isinstance(r, StsTransientError) for r in results), results
    # Each waiter retries the mint once it holds the lock, because there is
    # nothing cached to coalesce onto. That is correct: the alternative is
    # caching a failure and refusing a user whose consent just succeeded.
    assert minter.calls == 5


# --------------------------------------------------------------------------
# No leakage through the debug surfaces
# --------------------------------------------------------------------------


async def test_snapshot_and_repr_never_expose_a_token() -> None:
    clock = FakeClock()
    cache = PerUserTokenCache(clock=clock)
    secret = "super-secret-google-access-token"

    async def mint() -> MintedToken:
        return MintedToken(token=secret, lifetime_seconds=HOUR)

    await cache.get_or_mint(USER_A, mint)

    snapshot = cache.snapshot()
    assert secret not in repr(snapshot)
    assert secret not in repr(cache)
    assert secret not in repr(cache._entries[USER_A])
    assert secret not in repr(MintedToken(token=secret, lifetime_seconds=HOUR))
    assert snapshot[0]["user_key"] == USER_A
    assert snapshot[0]["token_fp"] != secret
