"""LIVE probe against the real Bot Framework OpenID metadata + JWKS.

Not part of the offline test suite (it needs egress). Run manually to confirm
the production key-resolution path works end to end.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.auth.inbound import (
    TO_BOT_FROM_CHANNEL_OPENID_METADATA_URL,
    InboundAuthError,
    JwksCache,
)


async def main() -> int:
    cache = JwksCache()
    try:
        # 1. Force a real fetch of the metadata document and the JWKS by
        #    asking for a kid we know does not exist.
        try:
            await cache.get_key(TO_BOT_FROM_CHANNEL_OPENID_METADATA_URL, "__no_such_kid__")
            print("UNEXPECTED: a bogus kid resolved")
            return 1
        except InboundAuthError as exc:
            print(f"bogus kid -> {exc.reason} (expected: unknown_kid)")
            if exc.reason != "unknown_kid":
                return 1

        cached = cache._cache[TO_BOT_FROM_CHANNEL_OPENID_METADATA_URL]
        kids = sorted(cached.keys_by_kid)
        print(f"live JWKS keys fetched: {len(kids)}")
        print(f"advertised+accepted algorithms: {sorted(cached.algorithms)}")
        print(f"first 3 real kids: {kids[:3]}")

        # 2. Resolve a real kid from the live document.
        real_kid = kids[0]
        key, algs = await cache.get_key(TO_BOT_FROM_CHANNEL_OPENID_METADATA_URL, real_kid)
        print(f"resolved real kid {real_kid!r} -> {type(key.key).__name__}, algs={sorted(algs)}")
        return 0
    finally:
        await cache.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
