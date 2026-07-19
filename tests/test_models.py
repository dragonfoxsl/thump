from datetime import timedelta

from thump.models import Check, CheckState, CheckType, Event, State


def test_state_values():
    assert State.UP == "up"
    assert State.DOWN == "down"
    assert State.PENDING == "pending"
    assert State.PAUSED == "paused"


def test_check_defaults():
    c = Check(
        name="nightly-db-backup",
        type=CheckType.HEARTBEAT,
        interval=timedelta(hours=24),
        grace=timedelta(hours=1),
        timeout=timedelta(seconds=10),
        history=100,
        failure_threshold=2,
        token="abc",
    )
    assert c.enabled is True
    assert c.url is None
    assert c.expect_status == 200


def test_checkstate_defaults_are_unobserved():
    s = CheckState()
    assert s.last_seen is None
    assert s.last_result_ok is None
    assert s.consecutive_failures == 0


def test_event_detail_defaults_empty():
    from datetime import datetime, timezone

    e = Event(at=datetime(2026, 7, 15, tzinfo=timezone.utc), kind="ping")
    assert e.detail == ""
