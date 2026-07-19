from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from thump.api import build_app
from thump.config import parse_config
from thump.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${THUMP_SECRET}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
ENV = {"THUMP_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
async def ctx(tmp_path):
    cfg = parse_config(YAML, ENV)
    store = SqliteStore(str(tmp_path / "s.db"))
    await store.connect()
    app = build_app(cfg, store, clock=lambda: T0)
    with TestClient(app) as client:
        yield cfg, store, client
    await store.close()


async def test_post_ping_records_a_sighting(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    r = client.post(f"/ping/{token}")
    assert r.status_code == 200

    state = await store.get_state("nightly-db-backup")
    assert state.last_seen == T0
    assert state.last_result_ok is True


async def test_get_ping_also_works_for_legacy_wget(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token
    assert client.get(f"/ping/{token}").status_code == 200
    assert (await store.get_state("nightly-db-backup")).last_result_ok is True


async def test_unknown_token_is_404_not_401(ctx):
    # An attacker probing the token space must not learn what exists.
    _, _, client = ctx
    r = client.post("/ping/deadbeefdeadbeefdeadbeefdeadbeef")
    assert r.status_code == 404


async def test_fail_endpoint_marks_failure_without_a_sighting(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    client.post(f"/ping/{token}")            # ran fine once
    r = client.post(f"/ping/{token}/fail")   # then ran and failed
    assert r.status_code == 200

    state = await store.get_state("nightly-db-backup")
    assert state.last_result_ok is False
    assert state.last_seen == T0  # a failure is not a sighting


async def test_body_is_stored_on_the_event(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    client.post(f"/ping/{token}/fail", content=b"pg_dump: connection refused")
    events = await store.get_events("nightly-db-backup", 10)
    assert events[0].kind == "fail"
    assert events[0].detail == "pg_dump: connection refused"


async def test_oversized_body_is_truncated_not_rejected(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    client.post(f"/ping/{token}", content=b"x" * 10_000)
    events = await store.get_events("nightly-db-backup", 10)
    assert len(events[0].detail) == 4096
