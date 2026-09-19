from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from thump.api import build_app
from thump.config import parse_config
from thump.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server:
  listen: ":8080"
  secret: ${THUMP_SECRET}
  admin_token: ${THUMP_ADMIN_TOKEN}
  timezone: Asia/Kolkata
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
NO_ADMIN_YAML = YAML.replace("  admin_token: ${THUMP_ADMIN_TOKEN}\n", "")
ENV = {"THUMP_SECRET": "test-secret-at-least-16-chars", "THUMP_ADMIN_TOKEN": "admin-token-at-least-16-chars"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
AUTH = {"Authorization": "Bearer admin-token-at-least-16-chars"}


@pytest.fixture
async def ctx(tmp_path):
    cfg = parse_config(YAML, ENV)
    store = SqliteStore(str(tmp_path / "s.db"))
    await store.connect()
    app = build_app(cfg, store, clock=lambda: T0)
    with TestClient(app) as client:
        yield cfg, store, client
    await store.close()


async def test_checks_requires_auth(ctx):
    _, _, client = ctx
    assert client.get("/checks").status_code == 401
    assert client.get("/checks", headers={"Authorization": "Bearer wrong"}).status_code == 401


async def test_metrics_requires_auth(ctx):
    _, _, client = ctx
    assert client.get("/metrics").status_code == 401


async def test_checks_returns_state_with_auth(ctx):
    cfg, _, client = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")

    r = client.get("/checks", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "nightly-db-backup"
    assert body[0]["state"] == "up"


async def test_check_detail_includes_the_event_ring_buffer(ctx):
    cfg, _, client = ctx
    token = cfg.by_name["nightly-db-backup"].token
    client.post(f"/ping/{token}/fail", content=b"pg_dump: connection refused")

    r = client.get("/checks/nightly-db-backup", headers=AUTH)
    assert r.status_code == 200
    events = r.json()["events"]
    assert events[0]["kind"] == "fail"
    assert events[0]["detail"] == "pg_dump: connection refused"


async def test_timestamps_render_in_the_configured_timezone(ctx):
    # Display only. The engineer debugging at 3am should not do UTC math.
    cfg, _, client = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")

    r = client.get("/checks/nightly-db-backup", headers=AUTH)
    # T0 = 02:00 UTC -> 07:30 in Asia/Kolkata (UTC+5:30)
    assert r.json()["last_seen"].startswith("2026-07-15T07:30:00")


async def test_unknown_check_detail_is_404(ctx):
    _, _, client = ctx
    assert client.get("/checks/no-such-check", headers=AUTH).status_code == 404


async def test_metrics_counts_unknown_pings(ctx):
    _, _, client = ctx
    client.post("/ping/deadbeefdeadbeefdeadbeefdeadbeef")
    client.post("/ping/deadbeefdeadbeefdeadbeefdeadbeef")

    body = client.get("/metrics", headers=AUTH).text
    assert "thump_unknown_ping_total 2" in body


async def test_endpoints_are_DISABLED_when_no_admin_token_configured(tmp_path):
    # Fail CLOSED. An accidentally-public /checks hands out the internal topology.
    cfg = parse_config(NO_ADMIN_YAML, ENV)
    store = SqliteStore(str(tmp_path / "s2.db"))
    await store.connect()
    with TestClient(build_app(cfg, store, clock=lambda: T0)) as client:
        assert client.get("/checks").status_code == 404
        assert client.get("/metrics").status_code == 404
        assert client.get("/checks", headers=AUTH).status_code == 404
    await store.close()
