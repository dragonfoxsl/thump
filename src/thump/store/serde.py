"""Timestamp (de)serialization shared by every Store implementation.

Every stored timestamp is ISO-8601 in UTC; every parsed timestamp is
timezone-aware UTC. Centralized so SQLite and Redis cannot drift.
"""

from datetime import datetime, timezone


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(timezone.utc) if s else None
