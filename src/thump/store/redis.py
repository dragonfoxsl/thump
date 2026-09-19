"""Multi-replica store. All replicas share state, so a cron's ping and the
vendor's poll agree regardless of which pod they land on. No PVC needed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

import redis.asyncio as aioredis
from redis.exceptions import RedisError, WatchError

from thump.models import CheckState, Event
from thump.store.base import StoreUnavailable
from thump.store.serde import iso, parse_dt

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _timestamp_us(value: datetime) -> int:
    """Return an exact, consistently comparable UTC timestamp."""
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class RedisStore:
    def __init__(self, dsn: str, client: aioredis.Redis | None = None) -> None:
        self._dsn = dsn
        self._client = client

    def _key_state(self, name: str) -> str:
        return f"thump:state:{name}"

    def _key_events(self, name: str) -> str:
        return f"thump:events:{name}"

    def _db(self) -> aioredis.Redis:
        if self._client is None:
            raise StoreUnavailable("redis store is not connected")
        return self._client

    async def connect(self) -> None:
        if self._client is None:
            self._client = aioredis.from_url(self._dsn, decode_responses=True)
        try:
            await self._client.ping()
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthy(self) -> bool:
        try:
            await self._db().ping()
            return True
        except (RedisError, StoreUnavailable):
            return False

    async def get_state(self, name: str) -> CheckState:
        return (await self.get_states([name]))[name]

    async def get_states(self, names: Sequence[str]) -> dict[str, CheckState]:
        try:
            pipe = self._db().pipeline()
            for n in names:
                pipe.hgetall(self._key_state(n))
            raws = await pipe.execute()
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

        out: dict[str, CheckState] = {}
        for name, raw in zip(names, raws, strict=True):
            raw = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in (raw or {}).items()
            }
            if not raw:
                out[name] = CheckState()
                continue
            ok_raw = raw.get("last_result_ok")
            out[name] = CheckState(
                last_seen=parse_dt(raw.get("last_seen") or None),
                last_result_ok=None if ok_raw in (None, "") else ok_raw == "1",
                consecutive_failures=int(raw.get("consecutive_failures", 0)),
            )
        return out

    async def _record(
        self, name: str, at: datetime, event: Event, history: int, *, ok: bool
    ) -> None:
        state_key = self._key_state(name)
        events_key = self._key_events(name)
        observed_us = _timestamp_us(at)
        payload = json.dumps({"at": iso(event.at), "kind": event.kind, "detail": event.detail})
        try:
            while True:
                async with self._db().pipeline() as pipe:
                    try:
                        # Watch both structures: legacy state has no observation
                        # marker, so its newest event supplies the upgrade
                        # boundary without racing a concurrent writer.
                        await pipe.watch(state_key, events_key)
                        previous = await pipe.hget(state_key, "observation_at_us")
                        if isinstance(previous, bytes):
                            previous = previous.decode()
                        if previous in (None, ""):
                            newest = await pipe.lindex(events_key, 0)
                            if isinstance(newest, bytes):
                                newest = newest.decode()
                            previous_at = None
                            if newest:
                                previous_at = parse_dt(json.loads(newest)["at"])
                            if previous_at is None:
                                last_seen = await pipe.hget(state_key, "last_seen")
                                if isinstance(last_seen, bytes):
                                    last_seen = last_seen.decode()
                                previous_at = parse_dt(last_seen or None)
                            if previous_at is not None:
                                previous = str(_timestamp_us(previous_at))
                        if previous not in (None, "") and observed_us < int(previous):
                            # Preserve the observation in history while leaving
                            # current state untouched. Also persist a lazily
                            # derived legacy marker before this older event
                            # becomes the list head.
                            pipe.multi()  # type: ignore[no-untyped-call]
                            pipe.hset(state_key, "observation_at_us", int(previous))
                            pipe.lpush(events_key, payload)
                            pipe.ltrim(events_key, 0, history - 1)
                            await pipe.execute()
                            return

                        pipe.multi()  # type: ignore[no-untyped-call]
                        if ok:
                            pipe.hset(
                                state_key,
                                mapping={
                                    "last_seen": iso(at),
                                    "observation_at_us": observed_us,
                                    "last_result_ok": "1",
                                    "consecutive_failures": 0,
                                },
                            )
                        else:
                            # last_seen deliberately remains untouched: a failure is not a sighting.
                            pipe.hset(
                                state_key,
                                mapping={"observation_at_us": observed_us, "last_result_ok": "0"},
                            )
                            pipe.hincrby(state_key, "consecutive_failures", 1)
                        pipe.lpush(events_key, payload)
                        pipe.ltrim(events_key, 0, history - 1)
                        await pipe.execute()
                        return
                    except WatchError:
                        # Another observation won the race. Re-read its timestamp
                        # and either apply this observation or discard it as stale.
                        continue
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

    async def record_success(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=True)

    async def record_failure(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=False)

    _LEASE_KEY = "thump:probe-leader"

    async def acquire_probe_lease(self, holder: str, ttl: float) -> bool:
        """Acquire or renew the process-wide lease without a read/write race.

        WATCH makes expiry or takeover between the ownership check and renewal
        invalidate the transaction. A stale holder can therefore never extend
        a lease that belongs to another replica.
        """
        px = max(1, int(ttl * 1000))
        try:
            while True:
                async with self._db().pipeline() as pipe:
                    try:
                        await pipe.watch(self._LEASE_KEY)
                        current = await pipe.get(self._LEASE_KEY)
                        if isinstance(current, bytes):
                            current = current.decode()
                        if current not in (None, holder):
                            await pipe.unwatch()  # type: ignore[no-untyped-call]
                            return False

                        pipe.multi()  # type: ignore[no-untyped-call]
                        if current is None:
                            pipe.set(self._LEASE_KEY, holder, px=px)
                        else:
                            pipe.pexpire(self._LEASE_KEY, px)
                        await pipe.execute()
                        return True
                    except WatchError:
                        continue
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

    async def get_events(self, name: str, limit: int) -> list[Event]:
        try:
            raws = await self._db().lrange(self._key_events(name), 0, limit - 1)
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e
        out: list[Event] = []
        for raw in raws:
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            at = parse_dt(d["at"])
            if at is None:
                raise StoreUnavailable(f"stored event for {name!r} has no timestamp")
            out.append(Event(at=at, kind=d["kind"], detail=d.get("detail", "")))
        return out
