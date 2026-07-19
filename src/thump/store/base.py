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
