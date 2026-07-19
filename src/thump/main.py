"""Wiring. Config is read once at boot; invalid config exits non-zero."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from thump.api import build_app
from thump.config import Config, ConfigError, load_config
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.connect()
        client = httpx.AsyncClient()
        scheduler = Scheduler(config, store, client)
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
    logging.basicConfig(
        level=os.environ.get("THUMP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
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
