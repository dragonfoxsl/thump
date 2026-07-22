"""Redis-specific behavior the parametrized conformance suite cannot express.

The probe lease only has to *mean* anything under Redis: it is the mechanism
that stops every replica from probing the same endpoint. SQLite is single
process and grants unconditionally, so contention semantics live here.
"""

import fakeredis.aioredis

from thump.store.redis import RedisStore


async def _connected() -> RedisStore:
    store = RedisStore("redis://localhost:6379/0", client=fakeredis.aioredis.FakeRedis())
    await store.connect()
    return store


async def test_probe_lease_denies_a_second_holder_while_the_first_holds_it():
    store = await _connected()
    try:
        assert await store.acquire_probe_lease("replica-a", ttl=30.0) is True
        # A different replica must NOT also become leader: that is the whole
        # point — one prober, not N.
        assert await store.acquire_probe_lease("replica-b", ttl=30.0) is False
    finally:
        await store.close()


async def test_probe_lease_lets_the_current_holder_renew_it():
    store = await _connected()
    try:
        assert await store.acquire_probe_lease("replica-a", ttl=30.0) is True
        # Re-acquiring under the same holder renews the lease rather than
        # failing, so the leader keeps probing tick after tick.
        assert await store.acquire_probe_lease("replica-a", ttl=30.0) is True
    finally:
        await store.close()
