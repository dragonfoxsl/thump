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
from tskmon.models import Event
from tskmon.store.base import Store

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

    return app
