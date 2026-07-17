"""Multi-replica store. All replicas share state, so a cron's ping and the
vendor's poll agree regardless of which pod they land on. No PVC needed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from tskmon.models import CheckState, Event
from tskmon.store.base import StoreUnavailable
from tskmon.store.serde import iso, parse_dt


class RedisStore:
    def __init__(self, dsn: str, client: aioredis.Redis | None = None) -> None:
        self._dsn = dsn
        self._client = client

    def _key_state(self, name: str) -> str:
        return f"tskmon:state:{name}"

    def _key_events(self, name: str) -> str:
        return f"tskmon:events:{name}"

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

    async def _push_event(self, pipe: aioredis.client.Pipeline, name: str, event: Event, history: int) -> None:
        pipe.lpush(
            self._key_events(name),
            json.dumps({"at": iso(event.at), "kind": event.kind, "detail": event.detail}),
        )
        pipe.ltrim(self._key_events(name), 0, history - 1)

    async def record_success(self, name: str, at: datetime, event: Event, history: int) -> None:
        try:
            pipe = self._db().pipeline()
            pipe.hset(
                self._key_state(name),
                mapping={"last_seen": iso(at), "last_result_ok": "1", "consecutive_failures": 0},
            )
            await self._push_event(pipe, name, event, history)
            await pipe.execute()
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

    async def record_failure(self, name: str, at: datetime, event: Event, history: int) -> None:
        try:
            pipe = self._db().pipeline()
            # last_seen deliberately untouched: a failure is not a sighting.
            pipe.hset(self._key_state(name), "last_result_ok", "0")
            pipe.hincrby(self._key_state(name), "consecutive_failures", 1)
            await self._push_event(pipe, name, event, history)
            await pipe.execute()
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
