"""CronSchedule wraps cronsim; these tests pin the behaviors that are easy to
get silently wrong — at-or-before boundary semantics, and DST transitions.

The DST cases assert exact instants on purpose. If a future cronsim upgrade
changes any of them, this suite fails in CI rather than at 2am.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from thump.schedule import CronSchedule, ScheduleError

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def test_parse_rejects_a_malformed_expression():
    with pytest.raises(ScheduleError):
        CronSchedule.parse("not a cron", UTC)


def test_parse_rejects_an_impossible_date():
    # February 30th never occurs; cronsim rejects it at parse time.
    with pytest.raises(ScheduleError):
        CronSchedule.parse("0 0 30 2 *", UTC)


def test_parse_accepts_a_valid_expression():
    s = CronSchedule.parse("0 2 * * *", UTC)
    assert s.expr == "0 2 * * *"
    assert s.tz is UTC


def test_prev_includes_an_instant_exactly_on_an_occurrence():
    # THE boundary case: cronsim's reverse iterator is strictly-before, so a
    # naive implementation returns the PREVIOUS day here.
    s = CronSchedule.parse("0 2 * * *", UTC)
    got = s.prev_at_or_before(datetime(2026, 7, 15, 2, 0, tzinfo=UTC))
    assert got == datetime(2026, 7, 15, 2, 0, tzinfo=UTC)


def test_prev_returns_the_same_days_occurrence_just_after_it():
    s = CronSchedule.parse("0 2 * * *", UTC)
    got = s.prev_at_or_before(datetime(2026, 7, 15, 2, 0, 30, tzinfo=UTC))
    assert got == datetime(2026, 7, 15, 2, 0, tzinfo=UTC)


def test_prev_returns_the_previous_days_occurrence_just_before_it():
    s = CronSchedule.parse("0 2 * * *", UTC)
    got = s.prev_at_or_before(datetime(2026, 7, 15, 1, 59, tzinfo=UTC))
    assert got == datetime(2026, 7, 14, 2, 0, tzinfo=UTC)


def test_prev_returns_none_when_no_occurrence_precedes_the_instant():
    # Walking backwards from the dawn of the calendar, cronsim runs off the
    # end of datetime and raises OverflowError (NOT StopIteration). The
    # wrapper must absorb it and return None rather than let it reach the
    # evaluator. Unreachable with real timestamps; guarded because the
    # evaluator's purity contract forbids surprise exceptions.
    s = CronSchedule.parse("0 2 * * *", UTC)
    assert s.prev_at_or_before(datetime(1, 1, 1, 0, 1, tzinfo=UTC)) is None


def test_prev_returns_tz_aware_utc_even_for_a_local_schedule():
    s = CronSchedule.parse("0 2 * * *", NY)
    got = s.prev_at_or_before(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    assert got is not None
    assert got.tzinfo is UTC
    # 02:00 EDT on 2026-07-15 is 06:00 UTC.
    assert got == datetime(2026, 7, 15, 6, 0, tzinfo=UTC)


def test_prev_skips_the_weekend_for_a_weekday_only_schedule():
    # Sunday 2026-07-19; the last weekday occurrence is Friday the 17th.
    s = CronSchedule.parse("0 2 * * 1-5", UTC)
    got = s.prev_at_or_before(datetime(2026, 7, 19, 12, 0, tzinfo=UTC))
    assert got == datetime(2026, 7, 17, 2, 0, tzinfo=UTC)


# --- DST pinning ----------------------------------------------------------

def test_spring_forward_shifts_the_missing_0200_to_0300():
    # 2025-03-09 02:00 does not exist in New York. cronsim does not skip the
    # day; it yields 03:00 -04:00 == 07:00 UTC.
    s = CronSchedule.parse("0 2 * * *", NY)
    got = s.prev_at_or_before(datetime(2025, 3, 9, 12, 0, tzinfo=UTC))
    assert got == datetime(2025, 3, 9, 7, 0, tzinfo=UTC)


def test_fall_back_0200_is_unambiguous_and_occurs_once():
    # 02:00 is NOT in the repeated hour: at 02:00 EDT the clock jumps back to
    # 01:00 EST, so 01:00-01:59 repeats and 02:00 happens exactly once.
    s = CronSchedule.parse("0 2 * * *", NY)
    got = s.prev_at_or_before(datetime(2025, 11, 2, 12, 0, tzinfo=UTC))
    assert got == datetime(2025, 11, 2, 7, 0, tzinfo=UTC)


def test_fall_back_repeated_hour_fires_once_at_the_first_instant():
    # 01:00 IS in the repeated hour. cronsim fires it once, at the first
    # (pre-transition, EDT) instant — matching Vixie cron.
    s = CronSchedule.parse("0 1 * * *", NY)
    got = s.prev_at_or_before(datetime(2025, 11, 2, 12, 0, tzinfo=UTC))
    assert got == datetime(2025, 11, 2, 5, 0, tzinfo=UTC)


def test_reverse_iteration_collapses_the_repeated_hour():
    # ASYMMETRY, verified against cronsim 2.7 and pinned deliberately:
    # forward iteration over `0 * * * *` emits BOTH 01:00 -04:00 and
    # 01:00 -05:00 (a 25-hour day), but reverse iteration resolves both to
    # the first. So during the repeated hour, prev_at_or_before returns the
    # earlier instant.
    #
    # This is benign and fail-SAFE: the deadline resolves up to an hour
    # early, making the check more lenient for one hour per year. It cannot
    # produce a false DOWN — only a one-hour-later detection of a genuinely
    # missed hourly ping. Not worth engineering around; pinned so that a
    # cronsim upgrade which changes it is noticed here.
    s = CronSchedule.parse("0 * * * *", NY)
    assert s.prev_at_or_before(
        datetime(2025, 11, 2, 5, 30, tzinfo=UTC)
    ) == datetime(2025, 11, 2, 5, 0, tzinfo=UTC)
    assert s.prev_at_or_before(
        datetime(2025, 11, 2, 6, 30, tzinfo=UTC)
    ) == datetime(2025, 11, 2, 5, 0, tzinfo=UTC)


def test_prev_rejects_a_naive_datetime():
    # The interval path raises TypeError on a naive `now`; the cron path must
    # not be quieter about it. Without this guard, astimezone() silently
    # reinterprets a naive datetime as system-local time and the check reports
    # `up` off a wrong instant — a fail-OPEN in a project that fails loud.
    s = CronSchedule.parse("0 2 * * *", UTC)
    with pytest.raises(ValueError):
        s.prev_at_or_before(datetime(2026, 7, 15, 2, 0))
