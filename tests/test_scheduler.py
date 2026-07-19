import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from tskmon.config import parse_config
from tskmon.models import CheckType
from tskmon.scheduler import Scheduler
from tskmon.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: internal-payments-api
    type: probe
    url: http://payments.internal:8080/healthz
    interval: 60s
    expect_status: 200
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
ENV = {"TSKMON_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def client_returning(status: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(status))
    )


def client_raising(exc: Exception) -> httpx.AsyncClient:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(str(tmp_path / "s.db"))
    await s.connect()
    yield s
    await s.close()


async def test_matching_status_records_success(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(200), clock=lambda: T0)

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is True

    state = await store.get_state("internal-payments-api")
    assert state.last_result_ok is True
    assert state.last_seen == T0
    assert state.consecutive_failures == 0


async def test_wrong_status_records_failure(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(500), clock=lambda: T0)

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is False

    state = await store.get_state("internal-payments-api")
    assert state.last_result_ok is False
    assert state.consecutive_failures == 1


async def test_connection_error_records_failure(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(
        cfg, store, client_raising(httpx.ConnectError("refused")), clock=lambda: T0
    )

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is False
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 1


async def test_timeout_records_failure(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(
        cfg, store, client_raising(httpx.ReadTimeout("slow")), clock=lambda: T0
    )

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is False
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 1


async def test_failures_accumulate_then_reset_on_success(store):
    cfg = parse_config(YAML, ENV)
    check = cfg.by_name["internal-payments-api"]

    failing = Scheduler(cfg, store, client_returning(500), clock=lambda: T0)
    await failing.probe_once(check)
    await failing.probe_once(check)
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 2

    recovering = Scheduler(cfg, store, client_returning(200), clock=lambda: T0)
    await recovering.probe_once(check)
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 0


async def test_probe_records_an_event(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(500), clock=lambda: T0)
    await sched.probe_once(cfg.by_name["internal-payments-api"])

    events = await store.get_events("internal-payments-api", 10)
    assert events[0].kind == "probe_fail"
    assert "500" in events[0].detail


class _ExplodingStore:
    """Wraps a real store but raises a plain (non-StoreUnavailable) exception
    from record_success/record_failure, simulating an unexpected bug in the
    store layer that is NOT the "store is down" case."""

    def __init__(self, inner):
        self._inner = inner

    async def record_success(self, *args, **kwargs):
        raise RuntimeError("boom: unexpected bug, not a StoreUnavailable case")

    async def record_failure(self, *args, **kwargs):
        raise RuntimeError("boom: unexpected bug, not a StoreUnavailable case")

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_run_survives_unexpected_non_store_error_and_still_cancels(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(
        cfg, store, client_returning(200), clock=lambda: T0
    )
    sched._store = _ExplodingStore(store)

    task = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)  # let a tick raise RuntimeError inside _loop

    # The task must still be running: the unexpected error must not have
    # propagated out of _loop and torn down the TaskGroup in run().
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_probes_only_probe_checks_and_stops_on_cancel(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(200), clock=lambda: T0)

    task = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)  # let the first immediate tick land
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await store.get_state("internal-payments-api")).last_result_ok is True
    # The heartbeat check is NOT probed — nothing to reach out to.
    assert (await store.get_state("nightly-db-backup")).last_result_ok is None


async def test_run_rejects_a_probe_with_no_interval():
    # Check.interval is timedelta | None since cron schedules landed, but
    # _loop calls interval.total_seconds(). Config forbids a probe without an
    # interval, so this is unreachable via YAML — fail loudly at startup
    # rather than AttributeError on the first tick if that ever changes.
    cfg = parse_config(YAML, ENV)
    probe = next(c for c in cfg.checks if c.type is CheckType.PROBE)
    broken = replace(probe, interval=None)
    cfg = replace(cfg, checks=(broken,))
    sched = Scheduler(cfg, SqliteStore(":memory:"), client_returning(200))
    with pytest.raises(ValueError, match="interval"):
        await sched.run()
