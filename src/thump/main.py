"""Wiring. Config is read once at boot; invalid config exits non-zero."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import socket
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from thump.api import build_app
from thump.config import Config, ConfigError, load_config
from thump.logconfig import build_formatter
from thump.scheduler import Scheduler
from thump.store.base import Store
from thump.store.redis import RedisStore
from thump.store.sqlite import SqliteStore

log = logging.getLogger("thump")

DEFAULT_CONFIG_PATH = "/etc/thump/config.yaml"


def build_store(cfg: Config) -> Store:
    if cfg.store.driver == "redis":
        return RedisStore(cfg.store.dsn)
    return SqliteStore(cfg.store.dsn)


def create_app(config_path: str | None = None, env: Mapping[str, str] | None = None) -> FastAPI:
    path = config_path or os.environ.get("THUMP_CONFIG", DEFAULT_CONFIG_PATH)
    config = load_config(path, env)
    store = build_store(config)
    # How long a probe leader holds the lease before it must renew (seconds).
    # Longer = fewer Redis round-trips but slower failover if the leader dies.
    lease_ttl_raw = os.environ.get("THUMP_LEASE_TTL", "60")
    try:
        lease_ttl = float(lease_ttl_raw)
    except ValueError as e:
        raise ConfigError(["THUMP_LEASE_TTL must be a positive finite number"]) from e
    if not math.isfinite(lease_ttl) or lease_ttl <= 0:
        raise ConfigError(["THUMP_LEASE_TTL must be a positive finite number"])
    # Identity recorded as the probe-lease holder. Defaults to the hostname so
    # `redis-cli get thump:probe-leader` names the replica that is probing;
    # override with THUMP_HOLDER when hostnames aren't distinct.
    holder = os.environ.get("THUMP_HOLDER") or socket.gethostname()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await store.connect()
        client = httpx.AsyncClient(
            # A probe that silently followed a 302 to a login page would report
            # a dead service as "up". The endpoint must answer for itself.
            follow_redirects=False,
            # Bound the pool so a burst of probes can't exhaust sockets. Each
            # request still carries its own per-check timeout in probe_once.
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            timeout=httpx.Timeout(10.0),
        )
        scheduler = Scheduler(config, store, client, holder=holder, lease_ttl=lease_ttl)
        task = asyncio.create_task(scheduler.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await client.aclose()
            await store.close()

    app = build_app(config, store)
    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(build_formatter(os.environ.get("THUMP_LOG_FORMAT", "text")))
    logging.basicConfig(
        level=os.environ.get("THUMP_LOG_LEVEL", "INFO"),
        handlers=[handler],
    )
    try:
        app = create_app()
    except ConfigError as e:
        # Refuse to start. A half-configured monitor silently fails to watch
        # the thing you thought it was watching.
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from e

    listen: str = app.state.config.server.listen
    host, _, port = listen.rpartition(":")
    uvicorn.run(app, host=host or "0.0.0.0", port=int(port), access_log=False)


if __name__ == "__main__":
    main()
