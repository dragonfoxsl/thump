import pytest
from fastapi.testclient import TestClient

from tskmon.config import parse_config
from tskmon.main import build_store
from tskmon.store.redis import RedisStore
from tskmon.store.sqlite import SqliteStore

BASE = """
store: {{driver: {driver}, dsn: '{dsn}'}}
server: {{listen: ":8080", secret: ${{TSKMON_SECRET}}}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
ENV = {"TSKMON_SECRET": "s3cret"}


def test_build_store_selects_sqlite():
    cfg = parse_config(BASE.format(driver="sqlite", dsn="./s.db"), ENV)
    assert isinstance(build_store(cfg), SqliteStore)


def test_build_store_selects_redis():
    cfg = parse_config(BASE.format(driver="redis", dsn="redis://localhost:6379/0"), ENV)
    assert isinstance(build_store(cfg), RedisStore)


def test_app_boots_end_to_end_and_serves_a_ping(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(BASE.format(driver="sqlite", dsn=str(tmp_path / "s.db")))

    from tskmon.main import create_app

    app = create_app(str(path), env=ENV)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/status").status_code == 200  # pending == healthy

        token = app.state.config.by_name["nightly-db-backup"].token
        assert client.post(f"/ping/{token}").status_code == 200
        assert client.get("/status/nightly-db-backup").text == "up"
