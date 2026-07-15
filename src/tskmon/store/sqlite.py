"""Default store: one file, stdlib, zero dependencies.

CAVEATS (documented, not bugs):
  - Restarts lose state unless the DB is on a PersistentVolume.
  - Multiple replicas SILENTLY BREAK CORRECTNESS: a cron's ping lands on replica
    A, the vendor's poll lands on replica B, which never heard of it. Use Redis.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone

from tskmon.models import CheckState, Event
from tskmon.store.base import StoreUnavailable

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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(timezone.utc) if s else None


class SqliteStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = sqlite3.connect(self._dsn, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreUnavailable("sqlite store is not connected")
        return self._conn

    async def healthy(self) -> bool:
        try:
            self._db().execute("SELECT 1").fetchone()
            return True
        except (sqlite3.Error, StoreUnavailable):
            return False

    async def get_state(self, name: str) -> CheckState:
        return (await self.get_states([name]))[name]

    async def get_states(self, names: Sequence[str]) -> dict[str, CheckState]:
        try:
            async with self._lock:
                rows = self._db().execute("SELECT * FROM check_state").fetchall()
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

        found = {
            r["name"]: CheckState(
                last_seen=_parse(r["last_seen"]),
                last_result_ok=None if r["last_result_ok"] is None else bool(r["last_result_ok"]),
                consecutive_failures=r["consecutive_failures"],
            )
            for r in rows
        }
        return {n: found.get(n, CheckState()) for n in names}

    async def _record(
        self, name: str, at: datetime, event: Event, history: int, *, ok: bool
    ) -> None:
        try:
            async with self._lock:
                db = self._db()
                if ok:
                    db.execute(
                        """
                        INSERT INTO check_state (name, last_seen, last_result_ok, consecutive_failures)
                        VALUES (?, ?, 1, 0)
                        ON CONFLICT(name) DO UPDATE SET
                            last_seen = excluded.last_seen,
                            last_result_ok = 1,
                            consecutive_failures = 0
                        """,
                        (name, _iso(at)),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO check_state (name, last_seen, last_result_ok, consecutive_failures)
                        VALUES (?, NULL, 0, 1)
                        ON CONFLICT(name) DO UPDATE SET
                            last_result_ok = 0,
                            consecutive_failures = check_state.consecutive_failures + 1
                        """,
                        (name,),
                    )
                db.execute(
                    "INSERT INTO events (name, at, kind, detail) VALUES (?, ?, ?, ?)",
                    (name, _iso(event.at), event.kind, event.detail),
                )
                db.execute(
                    """
                    DELETE FROM events
                     WHERE name = ?
                       AND id NOT IN (
                           SELECT id FROM events WHERE name = ? ORDER BY id DESC LIMIT ?
                       )
                    """,
                    (name, name, history),
                )
                db.commit()
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

    async def record_success(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=True)

    async def record_failure(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=False)

    async def get_events(self, name: str, limit: int) -> list[Event]:
        try:
            async with self._lock:
                rows = self._db().execute(
                    "SELECT at, kind, detail FROM events WHERE name = ? ORDER BY id DESC LIMIT ?",
                    (name, limit),
                ).fetchall()
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e
        return [
            Event(at=_parse(r["at"]), kind=r["kind"], detail=r["detail"])  # type: ignore[arg-type]
            for r in rows
        ]
