"""Outbound prober. Reaches endpoints on the private network because IT LIVES
THERE — this is the half of the system the external uptime vendor cannot do.

The scheduler only WRITES state. It never decides up/down; that is the
evaluator's job, computed at read time.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from thump.config import Config
from thump.models import Check, CheckType, Event
from thump.store.base import Store, StoreUnavailable

log = logging.getLogger("thump.scheduler")

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]
Jitter = Callable[[], float]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    # Ceiling on the random startup delay. A long-interval check should not
    # wait its whole interval before the first probe; a few seconds is plenty
    # to decorrelate replicas and stagger checks that boot on the same instant.
    JITTER_CAP = 5.0

    def __init__(
        self,
        config: Config,
        store: Store,
        client: httpx.AsyncClient,
        clock: Clock = _utcnow,
        *,
        holder: str | None = None,
        lease_ttl: float = 60.0,
        sleep: Sleep = asyncio.sleep,
        monotonic: Monotonic = time.monotonic,
        jitter: Jitter = random.random,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client
        self._clock = clock
        # Identity for the probe lease: this replica, unique per process.
        self._holder = holder or uuid.uuid4().hex
        self._lease_ttl = lease_ttl
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter

    async def probe_once(self, check: Check) -> bool:
        assert check.url is not None  # guaranteed by config validation
        now = self._clock()
        try:
            response = await self._client.get(
                check.url, timeout=check.timeout.total_seconds()
            )
            ok = response.status_code == check.expect_status
            detail = f"HTTP {response.status_code}"
        except httpx.HTTPError as e:
            ok = False
            detail = f"{type(e).__name__}: {e}"

        event = Event(
            at=now, kind="probe_ok" if ok else "probe_fail", detail=detail
        )
        if ok:
            await self._store.record_success(check.name, now, event, check.history)
        else:
            await self._store.record_failure(check.name, now, event, check.history)
        return ok

    async def _loop(self, check: Check) -> None:
        assert check.interval is not None  # guaranteed by run()'s guard
        interval = check.interval.total_seconds()
        # Stagger the first probe so multiple replicas — and many checks that
        # boot on the same instant — do not all strike the same endpoint at once.
        await self._sleep(self._jitter() * min(interval, self.JITTER_CAP))
        while True:
            start = self._monotonic()
            try:
                # Only the lease holder probes. With several replicas sharing a
                # store, this is what keeps one endpoint from being polled N
                # times a tick. SQLite grants unconditionally (single replica).
                if await self._store.acquire_probe_lease(self._holder, self._lease_ttl):
                    await self.probe_once(check)
            except StoreUnavailable as e:
                # Do not kill the loop: /healthz already reports the store, and
                # k8s will restart us. Keep trying.
                log.error("probe %s: store unavailable: %s", check.name, e)
            except Exception:
                # Never let one check's unexpected failure tear down the whole
                # TaskGroup (and thus every other probe loop) via run(). Log and
                # keep retrying on the next tick instead of failing open.
                log.exception("probe %s: unexpected error", check.name)
            # Fixed-RATE, not fixed-delay: the next tick is scheduled relative
            # to this one's start, so a slow probe does not drag the cadence out.
            elapsed = self._monotonic() - start
            await self._sleep(max(0.0, interval - elapsed))

    async def run(self) -> None:
        probes = [
            c
            for c in self._config.checks
            if c.type is CheckType.PROBE and c.enabled
        ]
        for check in probes:
            if check.interval is None:
                # Check.interval is optional since cron schedules landed, but
                # _loop sleeps on it. Config already forbids a probe without an
                # interval, so this is unreachable via YAML — fail at startup
                # rather than AttributeError on the first tick if that changes.
                raise ValueError(f"probe {check.name!r} has no interval")

        if not probes:
            await asyncio.Event().wait()  # nothing to do; block until cancelled
        async with asyncio.TaskGroup() as tg:
            for check in probes:
                tg.create_task(self._loop(check))
