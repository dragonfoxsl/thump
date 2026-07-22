"""HTTP surface. The route grouping is a security boundary, not an
organizational one:

  /ping/*    ingest    — the token IS the credential
  /status/*  status    — UNAUTHENTICATED by necessity, therefore LEAKS NOTHING
  /checks,/metrics     — bearer token; this is where internal topology lives
  /healthz   liveness  — is THIS process serving? NOT /status, NOT the store.
  /readyz    readiness — is the store reachable? gates traffic, never restarts.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from thump.config import Config
from thump.evaluator import evaluate
from thump.metrics import render_metrics
from thump.models import Check, Event, State
from thump.store.base import Store, StoreUnavailable

MAX_BODY_BYTES = 4096

try:
    _VERSION = _pkg_version("thump")
except PackageNotFoundError:  # pragma: no cover - only when running uninstalled
    _VERSION = "unknown"

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_app(config: Config, store: Store, clock: Clock = _utcnow) -> FastAPI:
    app = FastAPI(title="thump", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.store = store
    app.state.clock = clock
    app.state.unknown_pings = 0

    async def _detail(request: Request) -> str:
        # Bounded read: never buffer more than the cap (plus one transport
        # chunk), so a huge or endless POST body cannot exhaust memory. The
        # body is only diagnostic detail on the event — the ping itself is the
        # signal — so we truncate rather than reject: a verbose job must still
        # be able to check in.
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) >= MAX_BODY_BYTES:
                del buf[MAX_BODY_BYTES:]
                break
        return buf.decode("utf-8", errors="replace")

    def _lookup(token: str) -> Check:
        check = config.by_token.get(token)
        if check is None:
            # A real operational signal: a cron job believes it is monitored
            # when it is not. 404, never 401.
            app.state.unknown_pings += 1
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
        # LIVENESS: is this process alive and serving? Deliberately NOT /status
        # (a dead backup job must never restart the monitor) and deliberately
        # NOT the store either — a Redis blip is fixed by waiting, not by
        # restarting us into a CrashLoopBackOff. Point k8s livenessProbe here.
        return Response(content="ok", media_type="text/plain")

    @app.get("/readyz")
    async def readyz() -> Response:
        # READINESS: can we answer correctly right now? If the store is gone we
        # cannot, so report not-ready and let k8s pull us from the Service's
        # endpoints until it returns — without a restart. Point readinessProbe
        # here.
        ok = await store.healthy()
        return Response(
            content="ready" if ok else "store unavailable",
            status_code=200 if ok else 503,
            media_type="text/plain",
        )

    tz = config.server.timezone

    def _render_dt(dt: datetime | None) -> str | None:
        # Display only. Evaluation is always UTC duration arithmetic.
        return dt.astimezone(tz).isoformat() if dt is not None else None

    def _require_admin(authorization: str | None) -> None:
        if config.server.admin_token is None:
            # Fail CLOSED: unconfigured means DISABLED, not open.
            raise HTTPException(status_code=404, detail="not found")
        expected = f"Bearer {config.server.admin_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/checks")
    async def list_checks(authorization: str | None = Header(default=None)) -> JSONResponse:
        _require_admin(authorization)
        names = [c.name for c in config.checks]
        states = await store.get_states(names)
        now = clock()
        return JSONResponse(
            [
                {
                    "name": c.name,
                    "type": str(c.type),
                    "state": str(evaluate(c, states[c.name], now)),
                    "last_seen": _render_dt(states[c.name].last_seen),
                    "consecutive_failures": states[c.name].consecutive_failures,
                }
                for c in config.checks
            ]
        )

    @app.get("/checks/{name}")
    async def check_detail(
        name: str, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        _require_admin(authorization)
        check = config.by_name.get(name)
        if check is None:
            raise HTTPException(status_code=404, detail="not found")
        state = await store.get_state(name)
        events = await store.get_events(name, check.history)
        return JSONResponse(
            {
                "name": check.name,
                "type": str(check.type),
                "state": str(evaluate(check, state, clock())),
                "last_seen": _render_dt(state.last_seen),
                "consecutive_failures": state.consecutive_failures,
                "events": [
                    {"at": _render_dt(e.at), "kind": e.kind, "detail": e.detail}
                    for e in events
                ],
            }
        )

    @app.get("/metrics")
    async def metrics(authorization: str | None = Header(default=None)) -> PlainTextResponse:
        _require_admin(authorization)
        states = await store.get_states([c.name for c in config.checks])
        body = render_metrics(config, states, clock(), app.state.unknown_pings, version=_VERSION)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    return app
