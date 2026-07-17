"""The pure decision function.

PERFORMS NO I/O. No network, no database, no clock reads. `now` is a parameter.
This is what makes the correctness surface of the system testable in microseconds,
and it is why `down` can be computed at read time rather than by a background sweep.
"""

from datetime import datetime

from tskmon.models import Check, CheckState, CheckType, State


def evaluate(check: Check, state: CheckState, now: datetime) -> State:
    if not check.enabled:
        return State.PAUSED

    if state.last_result_ok is None:
        # Nothing has ever been observed. Healthy on purpose: treating this as
        # DOWN would page on every deploy until each interval elapsed.
        return State.PENDING

    if check.type is CheckType.HEARTBEAT:
        if state.last_result_ok is False:
            return State.DOWN
        if state.last_seen is None:
            # last_result_ok is True but no sighting recorded: an impossible
            # state from any real Store. Fail loud rather than compute against None.
            raise ValueError(
                f"check {check.name!r}: last_result_ok is True but last_seen is None"
            )
        if now - state.last_seen > check.interval + check.grace:
            return State.DOWN
        return State.UP

    # Probe: down only because probes failed, never because time passed.
    if state.consecutive_failures >= check.failure_threshold:
        return State.DOWN
    return State.UP
