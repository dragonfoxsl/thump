"""Default store: one file, stdlib, zero dependencies.

CAVEATS (documented, not bugs):
  - Restarts lose state unless the DB is on a PersistentVolume.
  - Multiple replicas SILENTLY BREAK CORRECTNESS: a cron's ping lands on replica
    A, the vendor's poll lands on replica B, which never heard of it. Use Redis.
  - An in-memory DSN (":memory:") is NOT supported: each operation opens its own
    short-lived connection, so an in-memory database would be empty every call.
    Use a file path (SQLite already needs a PersistentVolume to be useful here).

CONCURRENCY: every method offloads its blocking sqlite3 work to a worker thread
via asyncio.to_thread, opening a fresh connection there and closing it before
returning. The event loop is never blocked, and no sqlite3.Connection is ever
shared across threads. WAL mode lets readers and a single writer proceed
concurrently; busy_timeout absorbs brief write contention.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from thump.models import CheckState, Event
from thump.store.base import StoreUnavailable
from thump.store.serde import iso, parse_dt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_state (
    name                 TEXT PRIMARY KEY,
    last_seen            TEXT,
    last_result_ok       INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    at     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_by_check ON events (name, id DESC);
"""


class SqliteStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _open(self) -> sqlite3.Connection:
        # Runs inside a worker thread. WAL persists on the file, so re-declaring
        # it per connection is idempotent. busy_timeout waits out a concurrent
        # writer instead of raising SQLITE_BUSY immediately.
        conn = sqlite3.connect(self._dsn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def connect(self) -> None:
        def _init() -> None:
            conn = self._open()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

        try:
            await asyncio.to_thread(_init)
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

    async def close(self) -> None:
        # Nothing to close: connections are per-operation and short-lived.
        return None

    async def healthy(self) -> bool:
        def _check() -> bool:
            conn = self._open()
            try:
                conn.execute("SELECT 1").fetchone()
                return True
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_check)
        except sqlite3.Error:
            return False

    async def get_state(self, name: str) -> CheckState:
        return (await self.get_states([name]))[name]

    async def get_states(self, names: Sequence[str]) -> dict[str, CheckState]:
        def _read() -> dict[str, CheckState]:
            conn = self._open()
            try:
                rows = conn.execute("SELECT * FROM check_state").fetchall()
            finally:
                conn.close()
            return {
                r["name"]: CheckState(
                    last_seen=parse_dt(r["last_seen"]),
                    last_result_ok=None
                    if r["last_result_ok"] is None
                    else bool(r["last_result_ok"]),
                    consecutive_failures=r["consecutive_failures"],
                )
                for r in rows
            }

        try:
            found = await asyncio.to_thread(_read)
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e
        return {n: found.get(n, CheckState()) for n in names}

    async def _record(
        self, name: str, at: datetime, event: Event, history: int, *, ok: bool
    ) -> None:
        def _write() -> None:
            conn = self._open()
            try:
                with conn:  # commit on success, rollback on exception
                    if ok:
                        conn.execute(
                            """
                            INSERT INTO check_state (name, last_seen, last_result_ok, consecutive_failures)
                            VALUES (?, ?, 1, 0)
                            ON CONFLICT(name) DO UPDATE SET
                                last_seen = excluded.last_seen,
                                last_result_ok = 1,
                                consecutive_failures = 0
                            """,
                            (name, iso(at)),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO check_state (name, last_seen, last_result_ok, consecutive_failures)
                            VALUES (?, NULL, 0, 1)
                            ON CONFLICT(name) DO UPDATE SET
                                last_result_ok = 0,
                                consecutive_failures = check_state.consecutive_failures + 1
                            """,
                            (name,),
                        )
                    conn.execute(
                        "INSERT INTO events (name, at, kind, detail) VALUES (?, ?, ?, ?)",
                        (name, iso(event.at), event.kind, event.detail),
                    )
                    conn.execute(
                        """
                        DELETE FROM events
                         WHERE name = ?
                           AND id NOT IN (
                               SELECT id FROM events WHERE name = ? ORDER BY id DESC LIMIT ?
                           )
                        """,
                        (name, name, history),
                    )
            finally:
                conn.close()

        try:
            await asyncio.to_thread(_write)
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

    async def record_success(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=True)

    async def record_failure(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=False)

    async def get_events(self, name: str, limit: int) -> list[Event]:
        def _read() -> list[Event]:
            conn = self._open()
            try:
                rows = conn.execute(
                    "SELECT at, kind, detail FROM events WHERE name = ? ORDER BY id DESC LIMIT ?",
                    (name, limit),
                ).fetchall()
            finally:
                conn.close()
            return [
                Event(at=parse_dt(r["at"]), kind=r["kind"], detail=r["detail"])  # type: ignore[arg-type]
                for r in rows
            ]

        try:
            return await asyncio.to_thread(_read)
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e
