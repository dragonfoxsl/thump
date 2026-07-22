"""Integration tests against a REAL Redis, not fakeredis.

The probe lease leans on Redis's own `SET NX PX` expiry semantics — precisely
the thing an emulator can get subtly wrong. These run only when REDIS_URL is
set (CI provides a service container; locally, point it at a throwaway Redis
DB). They flush the DB, so use a dedicated one, e.g. redis://localhost:6379/15.
"""

import asyncio
import os
from datetime import datetime, timezone

import pytest

from thump.models import Event
from thump.store.redis import RedisStore

REDIS_URL = os.environ.get("REDIS_URL")

pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason="set REDIS_URL to run real-Redis integration tests"
)

T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
async def store():
    s = RedisStore(REDIS_URL)
    await s.connect()
    await s._db().flushdb()  # clean slate; REDIS_URL must be a throwaway DB
    yield s
    await s.close()


async def test_probe_lease_expires_and_transfers_on_real_redis(store):
    # The exact distributed contract: one holder at a time, and after the TTL
    # lapses a different replica can take over. fakeredis can't prove this.
    assert await store.acquire_probe_lease("replica-a", ttl=1.0) is True
    assert await store.acquire_probe_lease("replica-b", ttl=1.0) is False

    await asyncio.sleep(1.2)  # let the PX expiry lapse on the server

    assert await store.acquire_probe_lease("replica-b", ttl=1.0) is True


async def test_owner_renewal_extends_the_real_lease(store):
    assert await store.acquire_probe_lease("replica-a", ttl=2.0) is True
    await asyncio.sleep(1.0)
    # Renew before expiry: still ours, expiry pushed out.
    assert await store.acquire_probe_lease("replica-a", ttl=2.0) is True
    # A rival is still locked out after the original TTL would have lapsed.
    await asyncio.sleep(1.2)
    assert await store.acquire_probe_lease("replica-b", ttl=2.0) is False


async def test_state_round_trips_on_real_redis(store):
    await store.record_failure("c", T0, Event(at=T0, kind="probe_fail"), 100)
    await store.record_success("c", T0, Event(at=T0, kind="probe_ok"), 100)
    s = await store.get_state("c")
    assert s.last_result_ok is True
    assert s.last_seen == T0
    assert s.consecutive_failures == 0
