"""SQLite-specific behavior the parametrized conformance suite cannot express."""

import pytest

from tskmon.store.base import StoreUnavailable
from tskmon.store.sqlite import SqliteStore


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
