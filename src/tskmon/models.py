"""Domain types. No logic lives here."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tskmon.schedule import CronSchedule


class State(StrEnum):
    UP = "up"
    DOWN = "down"
    PENDING = "pending"
    PAUSED = "paused"


class CheckType(StrEnum):
    HEARTBEAT = "heartbeat"
    PROBE = "probe"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    type: CheckType
    # Exactly one of `interval` or `schedule` drives a heartbeat's deadline;
    # config enforces this. Probes always use `interval` as a poll frequency.
    interval: timedelta | None
    grace: timedelta
    timeout: timedelta
    history: int
    failure_threshold: int
    token: str
    enabled: bool = True
    url: str | None = None
    expect_status: int = 200
    schedule: CronSchedule | None = None


@dataclass(frozen=True, slots=True)
class CheckState:
    """`last_result_ok is None` means nothing has ever been observed."""

    last_seen: datetime | None = None
    last_result_ok: bool | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class Event:
    at: datetime
    kind: str  # "ping" | "fail" | "probe_ok" | "probe_fail"
    detail: str = ""
