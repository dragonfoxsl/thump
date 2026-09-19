import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from thump.config import parse_config
from thump.models import CheckType
from thump.scheduler import Scheduler
from thump.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${THUMP_SECRET}}
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
ENV = {"THUMP_SECRET": "test-secret-at-least-16-chars"}
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


async def test_probe_does_not_read_response_body(store):
    class ExplodingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("response body was consumed")
            yield b""  # pragma: no cover

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, stream=ExplodingBody()))
    )
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client, clock=lambda: T0)
    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is True
    await client.aclose()


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


class _DenyingLeaseStore:
    """Delegates to a real store but always refuses the probe lease, standing
    in for a replica that is NOT the elected prober."""

    def __init__(self, inner):
        self._inner = inner

    async def acquire_probe_lease(self, holder, ttl):
        return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _LeaseStore:
    def __init__(self, inner, outcomes):
        self._inner = inner
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def acquire_probe_lease(self, holder, ttl):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _StopLoop(Exception):
    """Breaks out of the otherwise-infinite _loop from an injected sleep."""


async def test_loop_does_not_probe_when_the_lease_is_denied(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(
        cfg, _DenyingLeaseStore(store), client_returning(200),
        clock=lambda: T0, jitter=lambda: 0.0,
    )

    task = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)  # a tick lands, but the lease is refused
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Not the leader -> never reached out -> nothing observed.
    assert (await store.get_state("internal-payments-api")).last_result_ok is None


async def test_lease_loop_renews_independently_and_clears_leadership_on_error(store):
    from thump.store.base import StoreUnavailable

    cfg = parse_config(YAML, ENV)
    lease_store = _LeaseStore(store, [True, StoreUnavailable("down")])
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise _StopLoop

    sched = Scheduler(cfg, lease_store, client_returning(200), lease_ttl=9, sleep=sleep)
    with pytest.raises(_StopLoop):
        await sched._lease_loop()
    assert lease_store.calls == 2
    assert sleeps == [3, 3]
    assert sched._is_leader is False


async def test_expired_local_lease_never_probes(store):
    cfg = parse_config(YAML, ENV)
    sleeps = []

    async def stop_after_tick(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise _StopLoop

    sched = Scheduler(
        cfg,
        store,
        client_returning(200),
        clock=lambda: T0,
        jitter=lambda: 0.0,
        monotonic=lambda: 10.0,
        sleep=stop_after_tick,
    )
    sched._is_leader = True
    sched._lease_deadline = 9.0

    with pytest.raises(_StopLoop):
        await sched._loop(cfg.by_name["internal-payments-api"])

    assert sched._is_leader is False
    assert (await store.get_state("internal-payments-api")).last_result_ok is None


async def test_first_tick_is_delayed_by_jitter(store):
    cfg = parse_config(YAML, ENV)
    sleeps: list[float] = []

    async def capturing_sleep(d):
        sleeps.append(d)
        raise _StopLoop  # stop at the very first sleep: the startup jitter

    sched = Scheduler(
        cfg, store, client_returning(200), clock=lambda: T0,
        jitter=lambda: 0.5, sleep=capturing_sleep,
    )
    with pytest.raises(_StopLoop):
        await sched._loop(cfg.by_name["internal-payments-api"])

    # 60s interval, capped, halved by the jitter draw of 0.5.
    assert sleeps[0] == 0.5 * min(60.0, Scheduler.JITTER_CAP)


async def test_interval_sleep_compensates_for_probe_duration(store):
    cfg = parse_config(YAML, ENV)
    sleeps: list[float] = []

    async def capturing_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 2:  # jitter sleep, then the first real interval sleep
            raise _StopLoop

    ticks = iter([100.0, 110.0, 999.0])  # probe "takes" 10s of monotonic time
    sched = Scheduler(
        cfg, store, client_returning(200), clock=lambda: T0,
        jitter=lambda: 0.0, sleep=capturing_sleep, monotonic=lambda: next(ticks),
    )
    with pytest.raises(_StopLoop):
        await sched._loop(cfg.by_name["internal-payments-api"])

    assert sleeps[0] == 0.0    # jitter draw of 0
    assert sleeps[1] == 50.0   # 60s interval minus 10s spent probing


async def test_run_survives_unexpected_non_store_error_and_still_cancels(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(
        cfg, store, client_returning(200), clock=lambda: T0, jitter=lambda: 0.0
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
    sched = Scheduler(cfg, store, client_returning(200), clock=lambda: T0, jitter=lambda: 0.0)

    task = asyncio.create_task(sched.run())

    async def wait_for_first_probe() -> None:
        while (await store.get_state("internal-payments-api")).last_result_ok is not True:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_first_probe(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

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
