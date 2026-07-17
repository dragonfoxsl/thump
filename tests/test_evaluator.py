from datetime import datetime, timedelta, timezone

from tskmon.evaluator import evaluate
from tskmon.models import Check, CheckState, CheckType, State

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
