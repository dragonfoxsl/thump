"""One suite, run against every Store implementation.

This is the only thing keeping the abstraction honest rather than quietly
SQLite-shaped.
"""

from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

from thump.models import Event
from thump.store.redis import RedisStore
from thump.store.sqlite import SqliteStore

T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", "redis"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStore(str(tmp_path / "state.db"))
    else:
        s = RedisStore("redis://localhost:6379/0", client=fakeredis.aioredis.FakeRedis())
    await s.connect()
    yield s
    await s.close()


async def test_unknown_check_returns_unobserved_state(store):
    s = await store.get_state("never-heard-of-it")
    assert s.last_seen is None
    assert s.last_result_ok is None
    assert s.consecutive_failures == 0


async def test_record_success_sets_last_seen_and_clears_failures(store):
    await store.record_failure("c", T0, Event(at=T0, kind="probe_fail"), 100)
    await store.record_success("c", T0, Event(at=T0, kind="probe_ok"), 100)
    s = await store.get_state("c")
    assert s.last_seen == T0
    assert s.last_result_ok is True
    assert s.consecutive_failures == 0


async def test_record_failure_increments_and_preserves_last_seen(store):
    await store.record_success("c", T0, Event(at=T0, kind="ping"), 100)
    t1 = T0 + timedelta(minutes=1)
    await store.record_failure("c", t1, Event(at=t1, kind="probe_fail"), 100)
    t2 = T0 + timedelta(minutes=2)
    await store.record_failure("c", t2, Event(at=t2, kind="probe_fail"), 100)

    s = await store.get_state("c")
    assert s.consecutive_failures == 2
    assert s.last_result_ok is False
    assert s.last_seen == T0  # unchanged by failures


async def test_timestamps_round_trip_as_utc_aware(store):
    await store.record_success("c", T0, Event(at=T0, kind="ping"), 100)
    s = await store.get_state("c")
    assert s.last_seen is not None
    assert s.last_seen.tzinfo is not None
    assert s.last_seen == T0


async def test_events_are_newest_first(store):
    for i in range(3):
        t = T0 + timedelta(minutes=i)
        await store.record_success("c", t, Event(at=t, kind="ping", detail=f"e{i}"), 100)
    events = await store.get_events("c", 10)
    assert [e.detail for e in events] == ["e2", "e1", "e0"]


async def test_event_ring_buffer_is_trimmed_to_history(store):
    for i in range(10):
        t = T0 + timedelta(minutes=i)
        await store.record_success("c", t, Event(at=t, kind="ping", detail=f"e{i}"), history=3)
    events = await store.get_events("c", 100)
    assert [e.detail for e in events] == ["e9", "e8", "e7"]


async def test_events_are_isolated_per_check(store):
    await store.record_success("a", T0, Event(at=T0, kind="ping", detail="for-a"), 100)
    await store.record_success("b", T0, Event(at=T0, kind="ping", detail="for-b"), 100)
    assert [e.detail for e in await store.get_events("a", 10)] == ["for-a"]
    assert [e.detail for e in await store.get_events("b", 10)] == ["for-b"]


async def test_get_states_batches(store):
    await store.record_success("a", T0, Event(at=T0, kind="ping"), 100)
    states = await store.get_states(["a", "b"])
    assert states["a"].last_result_ok is True
    assert states["b"].last_result_ok is None  # unknown -> default, never raises


async def test_healthy_is_true_when_connected(store):
    assert await store.healthy() is True
