"""SQLite-specific behavior the parametrized conformance suite cannot express."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from thump.models import Event
from thump.store.base import StoreUnavailable
from thump.store.sqlite import SqliteStore

T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


async def test_connect_wraps_backend_errors_as_store_unavailable(tmp_path):
    # A DSN under a nonexistent directory cannot be opened by sqlite3;
    # the failure must surface as StoreUnavailable, not a raw sqlite3.Error.
    bad_dsn = tmp_path / "no-such-dir" / "state.db"
    store = SqliteStore(str(bad_dsn))
    with pytest.raises(StoreUnavailable):
        await store.connect()


async def test_probe_lease_is_granted_unconditionally(tmp_path):
    # SQLite is single-replica by contract, so the sole process is always the
    # leader. Even a second, different holder gets True: there is no one to
    # coordinate with, and inventing a lock would only add a failure mode.
    store = SqliteStore(str(tmp_path / "state.db"))
    await store.connect()
    try:
        assert await store.acquire_probe_lease("whoever", ttl=30.0) is True
        assert await store.acquire_probe_lease("someone-else", ttl=30.0) is True
    finally:
        await store.close()


async def test_connect_migrates_existing_state_table_idempotently(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """CREATE TABLE check_state (
            name TEXT PRIMARY KEY, last_seen TEXT, last_result_ok INTEGER,
            consecutive_failures INTEGER NOT NULL DEFAULT 0)"""
        )

    store = SqliteStore(str(path))
    await store.connect()
    await store.connect()
    with closing(sqlite3.connect(path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(check_state)")}
    assert "observation_at" in columns


async def test_migration_uses_latest_event_to_protect_failed_state(tmp_path):
    path = tmp_path / "old-with-failure.db"
    newer = T0 + timedelta(minutes=1)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE check_state (
                name TEXT PRIMARY KEY, last_seen TEXT, last_result_ok INTEGER,
                consecutive_failures INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT INTO check_state VALUES (?, ?, 0, 1)",
            ("c", T0.isoformat()),
        )
        conn.execute(
            "INSERT INTO events (name, at, kind) VALUES (?, ?, ?)",
            ("c", newer.isoformat(), "probe_fail"),
        )
        conn.commit()

    store = SqliteStore(str(path))
    await store.connect()
    await store.record_success("c", T0, Event(at=T0, kind="stale"), 100)
    state = await store.get_state("c")
    assert state.last_result_ok is False
    assert state.consecutive_failures == 1


async def test_migration_retries_backfill_when_column_already_exists(tmp_path):
    path = tmp_path / "interrupted-migration.db"
    newer = T0 + timedelta(minutes=1)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE check_state (
                name TEXT PRIMARY KEY, last_seen TEXT, observation_at TEXT,
                last_result_ok INTEGER, consecutive_failures INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT INTO check_state VALUES (?, ?, NULL, 0, 1)",
            ("c", T0.isoformat()),
        )
        conn.execute(
            "INSERT INTO events (name, at, kind) VALUES (?, ?, ?)",
            ("c", newer.isoformat(), "probe_fail"),
        )
        conn.commit()

    store = SqliteStore(str(path))
    await store.connect()
    await store.record_success("c", T0, Event(at=T0, kind="stale"), 100)
    state = await store.get_state("c")
    assert state.last_result_ok is False
    assert state.consecutive_failures == 1


async def test_operations_run_without_a_persistent_shared_connection(tmp_path):
    # After connect(), each operation opens its own connection; a plain
    # record + read round-trips against the same on-disk file.
    store = SqliteStore(str(tmp_path / "state.db"))
    await store.connect()
    try:
        assert await store.healthy() is True
        state = await store.get_state("never-seen")
        assert state.last_result_ok is None
    finally:
        await store.close()
