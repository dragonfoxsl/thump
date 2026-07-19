"""Outbound prober. Reaches endpoints on the private network because IT LIVES
THERE — this is the half of the system the external uptime vendor cannot do.

The scheduler only WRITES state. It never decides up/down; that is the
evaluator's job, computed at read time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from thump.config import Config
from thump.models import Check, CheckType, Event
from thump.store.base import Store, StoreUnavailable

log = logging.getLogger("thump.scheduler")

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(
        self,
        config: Config,
        store: Store,
        client: httpx.AsyncClient,
        clock: Clock = _utcnow,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client
        self._clock = clock

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
        while True:
            try:
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
            await asyncio.sleep(check.interval.total_seconds())

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
