"""Redis-specific behavior the parametrized conformance suite cannot express.

The probe lease only has to *mean* anything under Redis: it is the mechanism
that stops every replica from probing the same endpoint. SQLite is single
process and grants unconditionally, so contention semantics live here.
"""

import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis

from thump.models import Event
from thump.store.redis import RedisStore

T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


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


async def test_legacy_state_uses_newest_event_as_observation_boundary():
    store = await _connected()
    newer = T0 + timedelta(minutes=1)
    try:
        db = store._db()
        await db.hset(
            store._key_state("c"),
            mapping={
                "last_seen": T0.isoformat(),
                "last_result_ok": "0",
                "consecutive_failures": "1",
            },
        )
        await db.lpush(
            store._key_events("c"),
            json.dumps({"at": newer.isoformat(), "kind": "probe_fail", "detail": ""}),
        )

        await store.record_success("c", T0, Event(at=T0, kind="stale"), 100)

        state = await store.get_state("c")
        assert state.last_result_ok is False
        assert state.consecutive_failures == 1
        assert [event.kind for event in await store.get_events("c", 10)] == [
            "stale",
            "probe_fail",
        ]
        marker = await db.hget(store._key_state("c"), "observation_at_us")
        assert marker is not None
    finally:
        await store.close()
