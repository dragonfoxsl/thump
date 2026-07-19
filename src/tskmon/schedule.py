"""Cron expressions, wrapped.

This is the ONLY module that imports cronsim. The evaluator — the pure
correctness core — depends on this interface instead, so cron behavior is
testable in isolation and the third-party library stays at arm's length.

It is also the seam through which probe scheduling could later obtain a
next_after(), without disturbing anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cronsim import CronSim, CronSimError

UTC = ZoneInfo("UTC")


class ScheduleError(ValueError):
    """A cron expression that cronsim cannot parse."""


@dataclass(frozen=True, slots=True)
class CronSchedule:
    expr: str
    tz: ZoneInfo

    @classmethod
    def parse(cls, expr: str, tz: ZoneInfo) -> CronSchedule:
        # cronsim validates eagerly in its constructor, so building a throwaway
        # iterator here surfaces a bad expression at config load rather than at
        # 2am on the first evaluation.
        try:
            CronSim(expr, datetime(2000, 1, 1, tzinfo=tz))
        except CronSimError as e:
            raise ScheduleError(f"invalid cron expression {expr!r}: {e}") from e
        return cls(expr=expr, tz=tz)

    def prev_at_or_before(self, dt: datetime) -> datetime | None:
        """The most recent occurrence at or before `dt`, as tz-aware UTC.

        cronsim's reverse iterator is strictly BEFORE its seed: seeding exactly
        on an occurrence yields the previous one. Since the caller evaluates
        this at `now - grace`, which lands exactly on an occurrence once per
        period, seeding naively would resolve the deadline a full period late.
        Seeding from dt + 1s gives at-or-before semantics.
        """
        if dt.tzinfo is None:
            # astimezone() would silently reinterpret a naive datetime as
            # system-local, resolving the deadline off a wrong instant and
            # reporting `up`. The interval path raises TypeError on a naive
            # `now`; this path must not be quieter about the same mistake.
            raise ValueError(
                f"prev_at_or_before requires a timezone-aware datetime, got {dt!r}"
            )
        seed = dt.astimezone(self.tz) + timedelta(seconds=1)
        try:
            occurrence = next(CronSim(self.expr, seed, reverse=True))
        except (StopIteration, OverflowError):
            # StopIteration: cronsim gave up searching backwards.
            # OverflowError: it walked past datetime.min doing so — cronsim
            # raises this rather than StopIteration. Unreachable for any real
            # timestamp, but it must not escape into the pure evaluator.
            return None
        return occurrence.astimezone(UTC)
