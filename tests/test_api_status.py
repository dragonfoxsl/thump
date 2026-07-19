from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from thump.api import build_app
from thump.config import parse_config
from thump.store.base import StoreUnavailable
from thump.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${THUMP_SECRET}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
    grace: 1h
  - name: legacy-etl
    type: heartbeat
    interval: 1h
    enabled: false
"""
ENV = {"THUMP_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
async def ctx(tmp_path):
    cfg = parse_config(YAML, ENV)
    store = SqliteStore(str(tmp_path / "s.db"))
    await store.connect()
    clock = Clock(T0)
    app = build_app(cfg, store, clock=clock)
    with TestClient(app) as client:
        yield cfg, store, client, clock
    await store.close()


async def test_pending_check_is_healthy(ctx):
    # Deliberate: a never-pinged check must not page on every deploy.
    _, _, client, _ = ctx
    r = client.get("/status/nightly-db-backup")
    assert r.status_code == 200
    assert r.text == "up"


async def test_aggregate_is_200_when_nothing_is_down(ctx):
    _, _, client, _ = ctx
    assert client.get("/status").status_code == 200


async def test_stale_heartbeat_is_503(ctx):
    cfg, _, client, clock = ctx
    token = cfg.by_name["nightly-db-backup"].token
    client.post(f"/ping/{token}")

    clock.t = T0 + timedelta(hours=26)  # past interval + grace

    r = client.get("/status/nightly-db-backup")
    assert r.status_code == 503
    assert r.text == "down"


async def test_one_down_check_takes_the_aggregate_down(ctx):
    cfg, _, client, clock = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")
    clock.t = T0 + timedelta(hours=26)
    assert client.get("/status").status_code == 503


async def test_disabled_check_is_excluded_from_the_aggregate(ctx):
    # legacy-etl is paused and never pinged; it must not drag /status down.
    _, _, client, clock = ctx
    clock.t = T0 + timedelta(days=30)
    assert client.get("/status").status_code == 200
    assert client.get("/status/legacy-etl").status_code == 200


async def test_status_body_leaks_nothing(ctx):
    _, _, client, _ = ctx
    body = client.get("/status").text
    assert body in ("up", "down")
    assert "nightly-db-backup" not in body


async def test_unknown_check_name_is_404(ctx):
    _, _, client, _ = ctx
    assert client.get("/status/no-such-check").status_code == 404


async def test_unreadable_store_fails_LOUD_not_open(ctx):
    # The worst possible bug is reporting "all clear" while blind.
    cfg, store, client, _ = ctx

    async def boom(_names):
        raise StoreUnavailable("redis is gone")

    store.get_states = boom  # type: ignore[method-assign]

    assert client.get("/status").status_code == 503
    assert client.get("/status/nightly-db-backup").status_code == 503


async def test_healthz_is_200_when_store_is_reachable(ctx):
    _, _, client, _ = ctx
    assert client.get("/healthz").status_code == 200


async def test_healthz_is_503_when_store_is_gone(ctx):
    _, store, client, _ = ctx

    async def unhealthy():
        return False

    store.healthy = unhealthy  # type: ignore[method-assign]
    assert client.get("/healthz").status_code == 503


async def test_healthz_is_not_status(ctx):
    # A dead backup job must NOT make k8s restart the monitor reporting it.
    cfg, _, client, clock = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")
    clock.t = T0 + timedelta(hours=26)

    assert client.get("/status").status_code == 503
    assert client.get("/healthz").status_code == 200
