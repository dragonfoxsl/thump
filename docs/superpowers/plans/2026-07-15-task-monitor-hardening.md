# task-monitor Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the LOW/Minor findings deferred from the task-monitor MVP whole-branch review: make `SqliteStore` non-blocking and fail-loud, make config numeric fields fail as `ConfigError` instead of crashing, and clean up duplicated helpers, runtime-invariant asserts, and an unused import.

**Architecture:** Three independent, sequential tasks over the already-merged `thump` package. Task 1 rewrites `SqliteStore` so every operation runs in a worker thread on its own short-lived connection (removing the shared-connection + unlocked-`healthy()` hazards) and introduces a shared timestamp-serde module. Task 2 adds a numeric-validation helper mirroring the existing `_duration` error-collection pattern. Task 3 finishes the DRY cleanup and removes the fragile asserts. Every task keeps the existing suite green (99 tests today) and adds focused tests for the new behavior.

**Tech Stack:** Python 3.12+ (dev box runs 3.14), stdlib `sqlite3`, `asyncio.to_thread`, PyYAML, redis-py (async). Tests: pytest, pytest-asyncio (`asyncio_mode = auto`), fakeredis. Use `.venv/bin/pytest` — the project virtualenv already exists.

**Source of findings:** Deferred items recorded in the MVP final review (silent-fail-open scheduler bug was already fixed in commit `e850889`; these are the remaining LOW/Minor items).

## Global Constraints

- **Package root:** `src/thump/`. Tests in `tests/`. Import as `from thump.x import y`.
- **All datetimes are timezone-aware UTC.** Never `datetime.utcnow()`. Stored timestamps are ISO-8601 UTC; parsed timestamps are tz-aware UTC.
- **`Store` errors fail LOUD:** every backend error (`sqlite3.Error`, `RedisError`) surfaces as `StoreUnavailable` — callers must never fail open. `connect()` is included in this contract.
- **The event loop must not block:** blocking `sqlite3` work runs in `asyncio.to_thread`, never inline in an `async` method.
- **No `sqlite3.Connection` is shared across threads.** Each operation opens and closes its own connection.
- **`evaluate()` performs no I/O and stays pure.** Do not add I/O, and do not weaken its correctness. It must not depend on `assert` for a runtime invariant (asserts vanish under `python -O`).
- **The store conformance suite is the contract.** `tests/store/test_conformance.py` runs against every `Store` impl; its test bodies must stay byte-identical. Both `SqliteStore` and `RedisStore` must keep passing all 9 parametrized cases each.
- **Config is fatal-on-invalid, and collects all errors of a class together** via `ConfigError(errors: list[str])`. A malformed value must append to `errors`, never raise a bare `ValueError`/`TypeError`.
- **Commit after every task.** Run the full suite (`.venv/bin/pytest -q`) once before each commit.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/thump/store/serde.py` | Create (Task 1) | Shared `iso()` / `parse_dt()` timestamp (de)serialization for all stores. |
| `src/thump/store/sqlite.py` | Rewrite internals (Task 1) | Per-operation connections offloaded via `asyncio.to_thread`; fail-loud `connect()`. Public interface unchanged. |
| `tests/store/test_sqlite.py` | Create (Task 1) | SQLite-specific behavior the parametrized conformance suite can't express (fail-loud `connect()`, non-blocking). |
| `src/thump/config.py` | Modify (Task 2) | Add `_int()` helper; route `history`/`failure_threshold`/`expect_status` through it. |
| `tests/test_config.py` | Modify (Task 2) | Add malformed-integer validation tests. |
| `src/thump/store/redis.py` | Modify (Task 3) | Use `serde`; drop local `_iso`/`_parse`; replace bare `assert` with an explicit fail-loud raise. |
| `src/thump/evaluator.py` | Modify (Task 3) | Replace the bare `assert` invariant with an explicit `ValueError`. |
| `tests/test_evaluator.py` | Modify (Task 3) | Remove the unused `import pytest`. |

---

### Task 1: Non-blocking, fail-loud SqliteStore + shared timestamp serde

Closes: sync-sqlite-in-async blocking the event loop, `connect()` not wrapping `sqlite3.Error`, and (half of) the duplicated `_iso`/`_parse` helpers.

**Files:**
- Create: `src/thump/store/serde.py`, `tests/store/test_sqlite.py`
- Rewrite: `src/thump/store/sqlite.py`

**Interfaces:**
- Consumes: `CheckState`, `Event` from `thump.models`; `StoreUnavailable` from `thump.store.base`.
- Produces:
  - `thump.store.serde.iso(dt: datetime) -> str` and `thump.store.serde.parse_dt(s: str | None) -> datetime | None`.
  - `SqliteStore(dsn: str)` — same public async methods as today (`connect`, `close`, `healthy`, `get_state`, `get_states`, `record_success`, `record_failure`, `get_events`), same semantics, now non-blocking and fail-loud on `connect()`.

**Semantics that must not change** (already asserted by the conformance suite): `record_success` sets `last_seen=at`, `last_result_ok=True`, `consecutive_failures=0`, pushes an event; `record_failure` sets `last_result_ok=False`, increments `consecutive_failures`, leaves `last_seen` unchanged, pushes an event; events are newest-first, trimmed to `history`; unknown name returns a default `CheckState()`; timestamps round-trip as tz-aware UTC.

**Note — in-memory DSN is unsupported by design.** With one short-lived connection per operation, a `:memory:` DSN would be a fresh empty database every call. No test or config uses `:memory:` for a real `SqliteStore` (the API/main tests all pass file paths), and the design already documents that SQLite needs a PersistentVolume. Document this in the module docstring; do not add special-casing.

- [ ] **Step 1: Write the failing SQLite-specific test**

Create `tests/store/test_sqlite.py`:

```python
"""SQLite-specific behavior the parametrized conformance suite cannot express."""

import pytest

from thump.store.base import StoreUnavailable
from thump.store.sqlite import SqliteStore


async def test_connect_wraps_backend_errors_as_store_unavailable(tmp_path):
    # A DSN under a nonexistent directory cannot be opened by sqlite3;
    # the failure must surface as StoreUnavailable, not a raw sqlite3.Error.
    bad_dsn = tmp_path / "no-such-dir" / "state.db"
    store = SqliteStore(str(bad_dsn))
    with pytest.raises(StoreUnavailable):
        await store.connect()


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
```

- [ ] **Step 2: Run the new test to verify it fails against the current implementation**

Run: `.venv/bin/pytest tests/store/test_sqlite.py -v`
Expected: `test_connect_wraps_backend_errors_as_store_unavailable` FAILS — the current `connect()` (`src/thump/store/sqlite.py:51-56`) lets a raw `sqlite3.OperationalError` escape instead of `StoreUnavailable`. (The second test may pass or fail depending on the current shared-connection behavior; the first is the RED signal.)

- [ ] **Step 3: Create the shared serde module**

Create `src/thump/store/serde.py`:

```python
"""Timestamp (de)serialization shared by every Store implementation.

Every stored timestamp is ISO-8601 in UTC; every parsed timestamp is
timezone-aware UTC. Centralized so SQLite and Redis cannot drift.
"""

from datetime import datetime, timezone


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(timezone.utc) if s else None
```

- [ ] **Step 4: Rewrite `SqliteStore`**

Replace the entire contents of `src/thump/store/sqlite.py` with:

```python
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
```

- [ ] **Step 5: Run the SQLite tests and the conformance suite**

Run: `.venv/bin/pytest tests/store -v`
Expected: all pass — the 9 `[sqlite]` + 9 `[redis]` conformance cases plus the 2 new `tests/store/test_sqlite.py` cases. If any conformance case fails, the rewrite changed a semantic — fix the implementation, not the test.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all green (101 tests: prior 99 + 2 new).

- [ ] **Step 7: Commit**

```bash
git add src/thump/store/serde.py src/thump/store/sqlite.py tests/store/test_sqlite.py
git commit -m "fix: SqliteStore runs off the event loop with per-op connections and fail-loud connect()

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Config numeric fields fail as ConfigError, not a raw crash

Closes: `history`/`failure_threshold`/`expect_status` coerced with bare `int()`, which raises an unhandled `ValueError`/`TypeError` on malformed input instead of collecting a `ConfigError`.

**Files:**
- Modify: `src/thump/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: the existing `errors: list[str]` accumulation pattern and `ConfigError` in `src/thump/config.py`.
- Produces: a private `_int(raw, key, default, errors, where) -> int` helper, mirroring the existing `_duration()` helper (`src/thump/config.py:89-96`). No public signature changes.

- [ ] **Step 1: Write the failing tests**

Add these tests to the end of `tests/test_config.py` (they use the existing `MINIMAL`, `ENV`, `ConfigError`, `parse_config` already imported/defined in that file):

```python
def test_malformed_integer_field_is_fatal_not_a_crash():
    text = MINIMAL + "    history: many\n"
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("history" in e for e in exc.value.errors)


def test_malformed_integer_is_collected_with_other_errors():
    # A bad driver AND a bad integer must report together — not crash on the first.
    text = MINIMAL.replace("driver: sqlite", "driver: mongodb") + "    history: lots\n"
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert len(exc.value.errors) == 2


def test_boolean_is_rejected_as_integer():
    # YAML `true` becomes Python True (an int subclass); silently accepting it
    # as history=1 would be a latent bug.
    text = MINIMAL + "    failure_threshold: true\n"
    with pytest.raises(ConfigError):
        parse_config(text, ENV)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v -k "malformed or boolean_is_rejected"`
Expected: FAIL — `test_malformed_integer_field_is_fatal_not_a_crash` and `test_malformed_integer_is_collected_with_other_errors` raise a bare `ValueError` (not `ConfigError`) from `int("many")`/`int("lots")`; `test_boolean_is_rejected_as_integer` fails because `int(True)` currently succeeds as `1` and no `ConfigError` is raised.

- [ ] **Step 3: Add the `_int` helper**

In `src/thump/config.py`, add this function immediately after `_duration` (which ends at line 96):

```python
def _int(raw: dict[str, Any], key: str, default: int, errors: list[str], where: str) -> int:
    if key not in raw:
        return default
    value = raw[key]
    # bool is a subclass of int; `history: true` must not silently become 1.
    if isinstance(value, bool):
        errors.append(f"{where}: {key} must be an integer, got {value!r}")
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        errors.append(f"{where}: {key} must be an integer, got {value!r}")
        return default
```

- [ ] **Step 4: Route the numeric fields through `_int`**

In `src/thump/config.py`, replace the two defaults lines (currently `src/thump/config.py:134-135`):

```python
    def_history = int(d.get("history", 100))
    def_threshold = int(d.get("failure_threshold", 2))
```

with:

```python
    def_history = _int(d, "history", 100, errors, "defaults.history")
    def_threshold = _int(d, "failure_threshold", 2, errors, "defaults.failure_threshold")
```

Then, in the `Check(...)` construction (currently `src/thump/config.py:175-180`), replace these three lines:

```python
                history=int(raw.get("history", def_history)),
                failure_threshold=int(raw.get("failure_threshold", def_threshold)),
```
and
```python
                expect_status=int(raw.get("expect_status", 200)),
```

with:

```python
                history=_int(raw, "history", def_history, errors, where),
                failure_threshold=_int(raw, "failure_threshold", def_threshold, errors, where),
```
and
```python
                expect_status=_int(raw, "expect_status", 200, errors, where),
```

Leave the other `Check(...)` fields untouched.

- [ ] **Step 5: Run the config tests**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: all pass — the 3 new tests plus the existing config tests (the existing `test_defaults_are_applied` and `test_per_check_override_beats_default` still confirm valid integers work).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all green (104 tests: 101 + 3 new).

- [ ] **Step 7: Commit**

```bash
git add src/thump/config.py tests/test_config.py
git commit -m "fix: config rejects malformed integer fields as ConfigError instead of crashing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: DRY the Redis store onto shared serde; replace fragile asserts; drop unused import

Closes: duplicated `_iso`/`_parse` in `redis.py` (the other half of the DRY finding), bare `assert` used as a runtime invariant in `redis.py` and `evaluator.py` (both vanish under `python -O`), and an unused `import pytest`.

**Files:**
- Modify: `src/thump/store/redis.py`, `src/thump/evaluator.py`, `tests/test_evaluator.py`

**Interfaces:**
- Consumes: `thump.store.serde.iso` / `parse_dt` (created in Task 1).
- Produces: no signature changes. Behavior is identical for valid data; the only behavior change is that two "impossible" states now raise an explicit, `-O`-proof error instead of an assertion.

- [ ] **Step 1: Point `redis.py` at the shared serde and remove its local copies**

In `src/thump/store/redis.py`:

Add the serde import next to the existing store imports (the file currently imports `from thump.store.base import StoreUnavailable`):

```python
from thump.store.serde import iso, parse_dt
```

Delete the two local helper definitions (currently `src/thump/store/redis.py:18-23`):

```python
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(timezone.utc) if s else None
```

Then update every call site in the file: replace `_iso(` with `iso(` and `_parse(` with `parse_dt(`. After this, `timezone` may be an unused import — if `timezone` is no longer referenced anywhere in `redis.py`, remove it from the `from datetime import ...` line (keep `datetime` if still used in type hints/signatures).

- [ ] **Step 2: Replace the bare assert in `redis.py` with an explicit fail-loud raise**

In `src/thump/store/redis.py`, the `get_events` loop currently contains (around `src/thump/store/redis.py:128-131`):

```python
            at = parse_dt(d["at"])
            assert at is not None
            out.append(Event(at=at, kind=d["kind"], detail=d.get("detail", "")))
```

(Note: after Step 1 the `_parse(` call is now `parse_dt(`.) Replace the `assert` line so a corrupt stored event fails loud even under `python -O`:

```python
            at = parse_dt(d["at"])
            if at is None:
                raise StoreUnavailable(f"stored event for {name!r} has no timestamp")
            out.append(Event(at=at, kind=d["kind"], detail=d.get("detail", "")))
```

- [ ] **Step 3: Replace the bare assert in `evaluator.py` with an explicit raise**

In `src/thump/evaluator.py`, the heartbeat branch currently contains (`src/thump/evaluator.py:25`):

```python
        assert state.last_seen is not None  # implied by last_result_ok is True
```

Replace it with an explicit, `-O`-proof guard that preserves the same invariant and fail-loud behavior:

```python
        if state.last_seen is None:
            # last_result_ok is True but no sighting recorded: an impossible
            # state from any real Store. Fail loud rather than compute against None.
            raise ValueError(
                f"check {check.name!r}: last_result_ok is True but last_seen is None"
            )
```

This keeps `evaluate()` pure (no I/O) and does not change behavior for any state a real `Store` can produce.

- [ ] **Step 4: Remove the unused import in `tests/test_evaluator.py`**

In `tests/test_evaluator.py`, delete the unused import line (`tests/test_evaluator.py:3`):

```python
import pytest
```

(Confirm `pytest` is not referenced elsewhere in the file — there are no `pytest.raises`/markers/fixtures in `test_evaluator.py` — before deleting.)

- [ ] **Step 5: Run the affected suites**

Run: `.venv/bin/pytest tests/store tests/test_evaluator.py -v`
Expected: all pass — the conformance suite still passes for both stores (proving the serde swap changed no behavior), and the evaluator tests still pass (the new raise is unreachable for the tested states).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all green (104 tests, unchanged count — this task adds no tests).

- [ ] **Step 7: Commit**

```bash
git add src/thump/store/redis.py src/thump/evaluator.py tests/test_evaluator.py
git commit -m "refactor: share timestamp serde, replace -O-fragile asserts, drop unused import

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Findings coverage:**

| Deferred finding | Task |
|---|---|
| Sync `sqlite3` in async methods blocks the event loop (naive `to_thread` fix unsafe with shared connection) | 1 — per-op connections, no shared connection, offloaded via `to_thread` |
| `SqliteStore.connect()` doesn't wrap `sqlite3.Error` into `StoreUnavailable` | 1 — `connect()` wraps and re-raises `StoreUnavailable` |
| Duplicated `_iso`/`_parse` across `sqlite.py` + `redis.py` | 1 (create `serde`, sqlite uses it) + 3 (redis uses it) |
| Config `history`/`failure_threshold`/`expect_status` bare `int()` → raw `ValueError` | 2 — `_int()` collects `ConfigError` |
| Bare `assert` as runtime invariant (`evaluator.py`, `redis.py`) — stripped under `-O` | 3 — explicit raises |
| Unused `import pytest` in `tests/test_evaluator.py` | 3 — removed |

No deferred finding is left unaddressed. (The MVP's phased env-var vs. structural-error collection was assessed as by-design in review and is intentionally out of scope here.)

**Placeholder scan:** No TBDs. Every code step contains complete, runnable code or an exact edit against a cited line range.

**Type consistency:** `serde.iso(datetime) -> str` and `serde.parse_dt(str | None) -> datetime | None` are used identically in `sqlite.py` (Task 1) and `redis.py` (Task 3). `SqliteStore`'s public async method signatures are unchanged, so `api.py`, `scheduler.py`, and `main.py` need no edits. `_int(raw, key, default, errors, where) -> int` matches the shape of the existing `_duration(...)` helper it sits beside.
