"""The storage seam. Data volume is trivial; this interface exists for
deployment topology (single-node SQLite vs multi-replica Redis vs Lambda later).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from thump.models import CheckState, Event


class StoreUnavailable(Exception):
    """The store could not be reached. Callers MUST fail loud, never fail open."""


class Store(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def healthy(self) -> bool: ...

    async def get_state(self, name: str) -> CheckState: ...

    async def get_states(self, names: Sequence[str]) -> dict[str, CheckState]: ...

    async def record_success(
        self, name: str, at: datetime, event: Event, history: int
    ) -> None: ...

    async def record_failure(
        self, name: str, at: datetime, event: Event, history: int
    ) -> None: ...

    async def get_events(self, name: str, limit: int) -> list[Event]: ...

    async def acquire_probe_lease(self, holder: str, ttl: float) -> bool:
        """Return True if `holder` may probe right now.

        This is the single-prober seam. With multiple replicas sharing one
        store, exactly one should reach out to a given endpoint per tick;
        otherwise every replica probes it, multiplying outbound traffic and
        writes. Redis backs a real lease (one holder at a time, expiring after
        `ttl` seconds so a dead leader is replaced). SQLite is single-replica
        by contract, so it grants unconditionally — there is no one to
        coordinate with. Callers re-acquire each tick: the holder renews, a
        follower is refused.
        """
        ...
