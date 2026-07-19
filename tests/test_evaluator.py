from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tskmon.evaluator import evaluate
from tskmon.models import Check, CheckState, CheckType, State
from tskmon.schedule import CronSchedule

NY = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def heartbeat(**kw) -> Check:
    base = dict(
        name="nightly-db-backup",
        type=CheckType.HEARTBEAT,
        interval=timedelta(hours=24),
        grace=timedelta(hours=1),
        timeout=timedelta(seconds=10),
        history=100,
        failure_threshold=2,
        token="t",
    )
    return Check(**{**base, **kw})


def probe(**kw) -> Check:
    base = dict(
        name="internal-payments-api",
        type=CheckType.PROBE,
        interval=timedelta(seconds=60),
        grace=timedelta(minutes=5),
        timeout=timedelta(seconds=10),
        history=100,
        failure_threshold=2,
        token="t",
        url="http://payments.internal:8080/healthz",
    )
    return Check(**{**base, **kw})


# --- paused ---------------------------------------------------------------

def test_disabled_check_is_paused_even_when_stale():
    c = heartbeat(enabled=False)
    s = CheckState(last_seen=T0 - timedelta(days=30), last_result_ok=True)
    assert evaluate(c, s, T0) is State.PAUSED


# --- heartbeat ------------------------------------------------------------

def test_heartbeat_never_pinged_is_pending():
    assert evaluate(heartbeat(), CheckState(), T0) is State.PENDING


def test_heartbeat_pinged_recently_is_up():
    s = CheckState(last_seen=T0 - timedelta(hours=1), last_result_ok=True)
    assert evaluate(heartbeat(), s, T0) is State.UP


def test_heartbeat_inside_grace_is_still_up():
    # interval 24h + grace 1h = 25h window; 24h30m late is inside it.
    s = CheckState(last_seen=T0 - timedelta(hours=24, minutes=30), last_result_ok=True)
    assert evaluate(heartbeat(), s, T0) is State.UP


def test_heartbeat_past_interval_plus_grace_is_down():
    # The dead man's switch: nothing happened, and that IS the failure.
    s = CheckState(last_seen=T0 - timedelta(hours=25, minutes=1), last_result_ok=True)
    assert evaluate(heartbeat(), s, T0) is State.DOWN


def test_heartbeat_exactly_at_boundary_is_up():
    s = CheckState(last_seen=T0 - timedelta(hours=25), last_result_ok=True)
    assert evaluate(heartbeat(), s, T0) is State.UP


def test_heartbeat_explicit_fail_is_down_immediately():
    # `backup.sh || curl .../fail` — ran, failed, said so. No waiting.
    s = CheckState(last_seen=T0 - timedelta(minutes=1), last_result_ok=False)
    assert evaluate(heartbeat(), s, T0) is State.DOWN


def test_heartbeat_recovers_on_next_successful_ping():
    s = CheckState(last_seen=T0, last_result_ok=True, consecutive_failures=0)
    assert evaluate(heartbeat(), s, T0) is State.UP


# --- probe ----------------------------------------------------------------

def test_probe_never_run_is_pending():
    assert evaluate(probe(), CheckState(), T0) is State.PENDING


def test_probe_single_failure_below_threshold_is_still_up():
    # One transient reset must not page anyone.
    s = CheckState(last_seen=T0, last_result_ok=False, consecutive_failures=1)
    assert evaluate(probe(), s, T0) is State.UP


def test_probe_at_failure_threshold_is_down():
    s = CheckState(last_seen=T0, last_result_ok=False, consecutive_failures=2)
    assert evaluate(probe(), s, T0) is State.DOWN


def test_probe_recovers_immediately_on_first_success():
    # No flap-damping on the way back up.
    s = CheckState(last_seen=T0, last_result_ok=True, consecutive_failures=0)
    assert evaluate(probe(), s, T0) is State.UP


def test_probe_does_not_go_down_from_staleness():
    # A probe is only down because probes FAILED, never because time passed.
    s = CheckState(last_seen=T0 - timedelta(days=7), last_result_ok=True)
    assert evaluate(probe(), s, T0) is State.UP


# --- purity ---------------------------------------------------------------

def test_evaluate_is_timezone_invariant():
    from zoneinfo import ZoneInfo

    s = CheckState(last_seen=T0 - timedelta(hours=26), last_result_ok=True)
    utc = evaluate(heartbeat(), s, T0)
    kolkata = evaluate(heartbeat(), s, T0.astimezone(ZoneInfo("Asia/Kolkata")))
    assert utc is kolkata is State.DOWN


# --- cron-scheduled heartbeats -------------------------------------------

def cron_heartbeat(expr="0 2 * * *", tz=UTC_TZ, **kw) -> Check:
    """A heartbeat driven by a cron schedule rather than an interval."""
    return heartbeat(
        interval=None,
        schedule=CronSchedule.parse(expr, tz),
        grace=timedelta(minutes=30),
        **kw,
    )


def test_cron_heartbeat_is_up_when_the_job_pinged_on_time():
    c = cron_heartbeat()
    s = CheckState(
        last_seen=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    now = datetime(2026, 7, 15, 2, 29, tzinfo=timezone.utc)
    assert evaluate(c, s, now) is State.UP


def test_cron_heartbeat_is_down_when_an_occurrence_was_missed():
    c = cron_heartbeat()
    s = CheckState(
        last_seen=datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    now = datetime(2026, 7, 15, 2, 31, tzinfo=timezone.utc)
    assert evaluate(c, s, now) is State.DOWN


def test_cron_heartbeat_tolerates_a_ping_that_arrives_slightly_early():
    # Cron host's clock runs 30s fast; the ping precedes its own occurrence.
    c = cron_heartbeat()
    s = CheckState(
        last_seen=datetime(2026, 7, 15, 1, 59, 30, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    now = datetime(2026, 7, 15, 2, 31, tzinfo=timezone.utc)
    assert evaluate(c, s, now) is State.UP


def test_cron_heartbeat_rejects_a_ping_far_earlier_than_its_occurrence():
    # Two hours early is not clock skew; that occurrence went uncovered.
    c = cron_heartbeat()
    s = CheckState(
        last_seen=datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    now = datetime(2026, 7, 15, 2, 31, tzinfo=timezone.utc)
    assert evaluate(c, s, now) is State.DOWN


def test_cron_heartbeat_stays_down_through_a_long_outage():
    # The verdict must not drift as last_seen recedes — this is why the
    # deadline is anchored to now rather than to last_seen.
    c = cron_heartbeat()
    s = CheckState(
        last_seen=datetime(2026, 6, 15, 2, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    for day in (16, 20, 30):
        now = datetime(2026, 7, day, 2, 31, tzinfo=timezone.utc)
        assert evaluate(c, s, now) is State.DOWN


def test_cron_heartbeat_is_up_over_a_weekend_for_a_weekday_schedule():
    # No duration can express this: Fri->Mon is 72h, every other gap is 24h.
    c = cron_heartbeat(expr="0 2 * * 1-5")
    s = CheckState(
        last_seen=datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    for day in (18, 19):
        now = datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc)
        assert evaluate(c, s, now) is State.UP


def test_cron_heartbeat_is_up_for_a_yearly_schedule_pinged_at_its_occurrence():
    # A yearly schedule has a 365-day gap; no interval could express it
    # without blinding the check for the whole year.
    c = cron_heartbeat(expr="0 2 1 1 *")  # 02:00 on January 1st
    s = CheckState(
        last_seen=datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    now = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)
    assert evaluate(c, s, now) is State.UP


def test_cron_heartbeat_is_down_when_the_last_result_failed():
    # An explicit /fail ping short-circuits before any schedule maths.
    c = cron_heartbeat()
    s = CheckState(
        last_seen=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
        last_result_ok=False,
    )
    now = datetime(2026, 7, 15, 2, 1, tzinfo=timezone.utc)
    assert evaluate(c, s, now) is State.DOWN


def test_cron_heartbeat_is_pending_before_any_observation():
    c = cron_heartbeat()
    now = datetime(2026, 7, 15, 2, 31, tzinfo=timezone.utc)
    assert evaluate(c, CheckState(), now) is State.PENDING


def test_cron_heartbeat_survives_a_dst_spring_forward():
    c = cron_heartbeat(tz=NY)
    # Job ran at the shifted 03:00 -04:00 == 07:00 UTC.
    s = CheckState(
        last_seen=datetime(2025, 3, 9, 7, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    for hour in (8, 12, 20):
        now = datetime(2025, 3, 9, hour, 0, tzinfo=timezone.utc)
        assert evaluate(c, s, now) is State.UP


def test_cron_heartbeat_survives_a_dst_fall_back():
    c = cron_heartbeat(tz=NY)
    # Job ran at 02:00 -05:00 == 07:00 UTC.
    s = CheckState(
        last_seen=datetime(2025, 11, 2, 7, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    for hour in (8, 12, 20):
        now = datetime(2025, 11, 2, hour, 0, tzinfo=timezone.utc)
        assert evaluate(c, s, now) is State.UP


def test_cron_heartbeat_without_interval_or_schedule_fails_loud():
    # Config prevents this; the evaluator must not silently pass it either,
    # and must not rely on assert (stripped under python -O).
    c = heartbeat(interval=None, schedule=None)
    s = CheckState(
        last_seen=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
        last_result_ok=True,
    )
    with pytest.raises(ValueError):
        evaluate(c, s, datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc))
