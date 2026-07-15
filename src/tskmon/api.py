"""HTTP surface. The route grouping is a security boundary, not an
organizational one:

  /ping/*    ingest    — the token IS the credential
  /status/*  status    — UNAUTHENTICATED by necessity, therefore LEAKS NOTHING
  /checks,/metrics     — bearer token; this is where internal topology lives
  /healthz   liveness  — the check on the checker. NOT /status.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response

from tskmon.config import Config
from tskmon.evaluator import evaluate
from tskmon.models import Event, State
from tskmon.store.base import Store, StoreUnavailable

MAX_BODY_BYTES = 4096

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_app(config: Config, store: Store, clock: Clock = _utcnow) -> FastAPI:
    app = FastAPI(title="tskmon", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.store = store
    app.state.clock = clock

    async def _detail(request: Request) -> str:
        body = await request.body()
        return body[:MAX_BODY_BYTES].decode("utf-8", errors="replace")

    def _lookup(token: str):
        check = config.by_token.get(token)
        if check is None:
            # 404, never 401: "wrong token" must be indistinguishable from
            # "no such check".
            raise HTTPException(status_code=404, detail="not found")
        return check

    @app.api_route("/ping/{token}", methods=["GET", "POST"])
    async def ping(token: str, request: Request) -> Response:
        check = _lookup(token)
        now = clock()
        await store.record_success(
            check.name, now, Event(at=now, kind="ping", detail=await _detail(request)), check.history
        )
        return Response(content="ok", media_type="text/plain")

    @app.api_route("/ping/{token}/fail", methods=["GET", "POST"])
    async def ping_fail(token: str, request: Request) -> Response:
        check = _lookup(token)
        now = clock()
        await store.record_failure(
            check.name, now, Event(at=now, kind="fail", detail=await _detail(request)), check.history
        )
        return Response(content="ok", media_type="text/plain")

    def _plain(state: State) -> Response:
        # Unauthenticated. Leaks NOTHING: no JSON, no names, no timestamps.
        healthy = state is not State.DOWN
        return Response(
            content="up" if healthy else "down",
            status_code=200 if healthy else 503,
            media_type="text/plain",
        )

    @app.get("/status/{name}")
    async def status_one(name: str) -> Response:
        check = config.by_name.get(name)
        if check is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            state = await store.get_state(name)
        except StoreUnavailable:
            # Fail LOUD. Reporting "all clear" while blind is the worst bug
            # this system can have.
            return Response(content="down", status_code=503, media_type="text/plain")
        return _plain(evaluate(check, state, clock()))

    @app.get("/status")
    async def status_all() -> Response:
        names = [c.name for c in config.checks]
        try:
            states = await store.get_states(names)
        except StoreUnavailable:
            return Response(content="down", status_code=503, media_type="text/plain")

        now = clock()
        down = any(
            evaluate(c, states[c.name], now) is State.DOWN for c in config.checks
        )
        return _plain(State.DOWN if down else State.UP)

    @app.get("/healthz")
    async def healthz() -> Response:
        # The check ON THE CHECKER. Deliberately NOT /status: a genuinely-dead
        # backup job must never cause k8s to kill the monitor reporting it.
        ok = await store.healthy()
        return Response(
            content="ok" if ok else "store unavailable",
            status_code=200 if ok else 503,
            media_type="text/plain",
        )

    return app
