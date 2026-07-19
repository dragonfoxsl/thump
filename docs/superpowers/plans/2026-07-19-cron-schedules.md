# Cron-expression Schedules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a heartbeat check declare `schedule: "0 2 * * *"` instead of `interval: 24h`, so its deadline is anchored to when the job was *supposed to run* rather than when it was last seen.

**Architecture:** Four sequential tasks. Task 1 adds the `cronsim` dependency and a `CronSchedule` wrapper in a new `schedule.py`, keeping third-party cron logic out of the pure evaluator. Task 2 widens `Check` and forks the evaluator's heartbeat branch — the pure core, testable with hand-built `Check` objects. Task 3 wires config validation so a cron check can be *produced* only after the evaluator can *consume* one. Task 4 documents. Every task keeps the existing 104 tests green.

**Tech Stack:** Python 3.12+ (dev box runs 3.14), `cronsim` 2.7 (new runtime dependency, zero transitive deps), stdlib `zoneinfo`. Tests: pytest, pytest-asyncio (`asyncio_mode = auto`). Use `.venv/bin/pytest` — the project virtualenv already exists.

**Source:** `docs/superpowers/specs/2026-07-19-cron-schedules-design.md`

## Global Constraints

- **Package root:** `src/thump/`. Tests in `tests/`. Import as `from thump.x import y`.
- **All datetimes are timezone-aware UTC** at every module boundary. The local timezone is an implementation detail internal to `CronSchedule`. Never `datetime.utcnow()`.
- **`evaluate()` performs no I/O and stays pure.** No network, no database, no clock reads. `now` remains a parameter. It must not import `cronsim`.
- **`evaluate()` must not use `assert` for runtime invariants** — asserts vanish under `python -O`. Use explicit `raise`.
- **Config is fatal-on-invalid and collects all errors of a class together** via `ConfigError(errors: list[str])`. A malformed value appends to `errors`; it never raises a bare `ValueError`/`TypeError`.
- **`EARLY_TOLERANCE` is a flat 60 seconds**, module-level in `evaluator.py`, not configurable, with no DST-specific or occurrence-specific widening.
- **Backward compatibility is absolute.** Every existing `interval`-based config must behave identically. The 104 existing tests must stay green throughout.
- **Commit after every task.** Run `.venv/bin/pytest -q` once before each commit.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify (Task 1) | Add `cronsim>=2.7` to `[project.dependencies]`. |
| `src/thump/schedule.py` | Create (Task 1) | `CronSchedule` wrapper; the only module importing `cronsim`. |
| `tests/test_schedule.py` | Create (Task 1) | Parsing, at-or-before boundary, DST pinning. |
| `src/thump/models.py` | Modify (Task 2) | `interval` widens to `timedelta \| None`; add `schedule`. |
| `src/thump/evaluator.py` | Modify (Task 2) | Fork heartbeat branch on `check.schedule`; add `EARLY_TOLERANCE`. |
| `tests/test_evaluator.py` | Modify (Task 2) | Cron-heartbeat evaluation cases. |
| `src/thump/config.py` | Modify (Task 3) | Exactly-one-of validation; build `CronSchedule` from `server.timezone`. |
| `tests/test_config.py` | Modify (Task 3) | Validation and wiring cases. |
| `README.md` | Modify (Task 4) | Document `schedule:`, tolerance, DST. |
| `config.example.yaml` | Modify (Task 4) | Show a cron heartbeat. |

---

### Task 1: `CronSchedule` wrapper over `cronsim`

Creates the seam that keeps `cronsim` out of the pure evaluator, and nails down the two behaviors that are easy to get silently wrong: at-or-before boundary semantics, and DST.

**Files:**
- Modify: `pyproject.toml`
- Create: `src/thump/schedule.py`, `tests/test_schedule.py`

**Interfaces:**
- Consumes: nothing from the project.
- Produces:
  - `thump.schedule.CronSchedule` — frozen dataclass with fields `expr: str`, `tz: ZoneInfo`.
  - `CronSchedule.parse(expr: str, tz: ZoneInfo) -> CronSchedule` — classmethod; raises `ScheduleError` on a malformed expression.
  - `CronSchedule.prev_at_or_before(dt: datetime) -> datetime | None` — most recent occurrence at or before `dt`, returned as tz-aware UTC.
  - `thump.schedule.ScheduleError` — exception raised for a malformed expression. Task 3 catches this.

**Behavior that must hold** (all verified against `cronsim` 2.7 during design):
- `CronSim`'s reverse iterator is **strictly before** its seed. `prev_at_or_before` must seed from `dt + 1 second` to get at-or-before semantics.
- Spring forward 2025-03-09 `America/New_York`, `0 2 * * *` → `03:00 -04:00` = `07:00 UTC`.
- Fall back 2025-11-02 `America/New_York`, `0 2 * * *` → `02:00 -05:00` = `07:00 UTC`.
- Fall back 2025-11-02 `America/New_York`, `0 1 * * *` → `01:00 -04:00` = `05:00 UTC` (the first of the repeated hour, once).
- Reverse iteration **collapses the repeated hour**: for `0 * * * *`, both `05:30 UTC` and `06:30 UTC` resolve back to `05:00 UTC`. Fail-safe (more lenient, never a false DOWN); pinned by test, not worked around.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, change the `dependencies` list to include `cronsim`:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "redis>=5.2",
    "cronsim>=2.7",
]
```

Then install it into the existing virtualenv:

```bash
.venv/bin/pip install 'cronsim>=2.7'
```

Expected: `Successfully installed cronsim-2.7` (or newer).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_schedule.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule.py -v`
Expected: every test ERRORs at collection with `ModuleNotFoundError: No module named 'thump.schedule'`.

- [ ] **Step 4: Implement `schedule.py`**

Create `src/thump/schedule.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule.py -v`
Expected: 13 passed.

If `test_prev_includes_an_instant_exactly_on_an_occurrence` fails with the previous day's date, the `+ timedelta(seconds=1)` seeding was dropped — restore it.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: 117 passed (104 existing + 13 new).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/thump/schedule.py tests/test_schedule.py
git commit -m "feat: CronSchedule wrapper over cronsim with at-or-before semantics

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Widen `Check` and fork the evaluator

The pure correctness core. Done before config so that the evaluator can consume a cron check before config is able to produce one — there is no intermediate state where a valid config crashes the evaluator.

**Files:**
- Modify: `src/thump/models.py`, `src/thump/evaluator.py`
- Test: `tests/test_evaluator.py`

**Interfaces:**
- Consumes: `CronSchedule` and `CronSchedule.prev_at_or_before` from Task 1.
- Produces:
  - `Check.interval: timedelta | None` (was `timedelta`) and `Check.schedule: CronSchedule | None = None`.
  - `thump.evaluator.EARLY_TOLERANCE: timedelta` — 60 seconds.

**Note on field ordering:** `Check` is a frozen dataclass whose fields without defaults must precede those with defaults. `interval` keeps its position (it is not given a default, it merely widens its type), and `schedule` is added at the end alongside the other defaulted fields. Existing positional construction is unaffected; all project code and tests build `Check` with keyword arguments.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/test_evaluator.py`:

```python
# --- cron-scheduled heartbeats -------------------------------------------

from zoneinfo import ZoneInfo

from thump.schedule import CronSchedule

NY = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")


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
```

Note: `tests/test_evaluator.py` currently has no `import pytest` (it was removed during the hardening work). The last test needs it. Add `import pytest` to the imports at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_evaluator.py -v -k cron`
Expected: FAIL — `TypeError: Check.__init__() got an unexpected keyword argument 'schedule'`.

- [ ] **Step 3: Widen the model**

In `src/thump/models.py`, add the import at the top (after the existing `from enum import StrEnum`):

```python
from thump.schedule import CronSchedule
```

Then change the `Check` dataclass. Replace:

```python
@dataclass(frozen=True, slots=True)
class Check:
    name: str
    type: CheckType
    interval: timedelta
    grace: timedelta
    timeout: timedelta
    history: int
    failure_threshold: int
    token: str
    enabled: bool = True
    url: str | None = None
    expect_status: int = 200
```

with:

```python
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
```

- [ ] **Step 4: Fork the evaluator**

In `src/thump/evaluator.py`, replace the import block:

```python
from datetime import datetime

from thump.models import Check, CheckState, CheckType, State
```

with:

```python
from datetime import datetime, timedelta

from thump.models import Check, CheckState, CheckType, State

# Absorbs clock skew between the cron host and the monitor: a job whose host
# runs slightly fast can ping just before its own scheduled occurrence. Not
# configurable — skew is an environmental defect with a fixed remedy (NTP),
# not a per-check policy.
EARLY_TOLERANCE = timedelta(seconds=60)
```

Then replace the heartbeat branch. The current code is:

```python
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
```

Replace it with:

```python
    if check.type is CheckType.HEARTBEAT:
        if state.last_result_ok is False:
            return State.DOWN
        if state.last_seen is None:
            # last_result_ok is True but no sighting recorded: an impossible
            # state from any real Store. Fail loud rather than compute against None.
            raise ValueError(
                f"check {check.name!r}: last_result_ok is True but last_seen is None"
            )

        if check.schedule is not None:
            # Has the most recent occurrence whose grace has already expired
            # been covered by a ping? Anchored to `now`, not to `last_seen`, so
            # the verdict does not drift during a long outage.
            last_due = check.schedule.prev_at_or_before(now - check.grace)
            if last_due is not None and state.last_seen < last_due - EARLY_TOLERANCE:
                return State.DOWN
            return State.UP

        if check.interval is None:
            # Config guarantees a heartbeat has exactly one of interval or
            # schedule. Fail loud rather than compute against None.
            raise ValueError(
                f"check {check.name!r}: heartbeat has neither interval nor schedule"
            )
        if now - state.last_seen > check.interval + check.grace:
            return State.DOWN
        return State.UP
```

- [ ] **Step 5: Run the evaluator tests**

Run: `.venv/bin/pytest tests/test_evaluator.py -v`
Expected: all pass — the 12 new cron cases plus every existing interval case unchanged.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: 129 passed (117 + 12 new).

- [ ] **Step 7: Commit**

```bash
git add src/thump/models.py src/thump/evaluator.py tests/test_evaluator.py
git commit -m "feat: evaluate heartbeats against a cron schedule

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Config validation and wiring

Makes `schedule:` reachable from a YAML file, enforcing exactly-one-of and finally using the `server.timezone` hook that has been parsed-but-unused since the MVP.

**Files:**
- Modify: `src/thump/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `CronSchedule.parse` and `ScheduleError` from Task 1; the widened `Check` from Task 2.
- Produces: no new public names. `parse_config` gains validation rules.

**Validation rules:**
- `heartbeat` with neither `interval` nor `schedule` → error `"<where>: heartbeat requires either interval or schedule"`.
- `heartbeat` with both → error `"<where>: interval and schedule are mutually exclusive"`.
- `probe` with `schedule` → error `"<where>: schedule is only valid for heartbeat checks"`.
- `probe` without `interval` → the existing `"<where>: interval is required"` error, unchanged.
- Malformed expression → the `ScheduleError` message, collected.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/test_config.py`:

```python
CRON = """
store:
  driver: sqlite
  dsn: /var/lib/thump/state.db
server:
  listen: ":8080"
  secret: ${THUMP_SECRET}
  timezone: America/New_York
checks:
  - name: nightly-db-backup
    type: heartbeat
    schedule: "0 2 * * *"
    grace: 30m
"""


def test_schedule_is_parsed_against_the_server_timezone():
    cfg = parse_config(CRON, ENV)
    check = cfg.checks[0]
    assert check.schedule is not None
    assert check.schedule.expr == "0 2 * * *"
    assert check.schedule.tz == ZoneInfo("America/New_York")
    assert check.interval is None


def test_heartbeat_with_neither_interval_nor_schedule_is_fatal():
    text = CRON.replace('    schedule: "0 2 * * *"\n', "")
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("interval or schedule" in e for e in exc.value.errors)


def test_heartbeat_with_both_interval_and_schedule_is_fatal():
    text = CRON.replace(
        '    schedule: "0 2 * * *"\n',
        '    schedule: "0 2 * * *"\n    interval: 24h\n',
    )
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("mutually exclusive" in e for e in exc.value.errors)


def test_probe_may_not_carry_a_schedule():
    text = """
store:
  driver: sqlite
server:
  secret: ${THUMP_SECRET}
checks:
  - name: payments
    type: probe
    url: http://payments.internal/healthz
    interval: 60s
    schedule: "0 2 * * *"
"""
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("only valid for heartbeat" in e for e in exc.value.errors)


def test_malformed_cron_expression_is_collected_as_a_config_error():
    text = CRON.replace('"0 2 * * *"', '"not a cron"')
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("cron expression" in e for e in exc.value.errors)


def test_interval_based_heartbeats_still_have_no_schedule():
    # Backward compatibility: the existing MINIMAL config is untouched.
    cfg = parse_config(MINIMAL, ENV)
    check = cfg.checks[0]
    assert check.schedule is None
    assert check.interval == timedelta(hours=24)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v -k "schedule or cron"`
Expected: FAIL — `test_schedule_is_parsed_against_the_server_timezone` fails because `parse_config` ignores the `schedule` key and errors with `"interval is required"` instead.

- [ ] **Step 3: Import the schedule module in config**

In `src/thump/config.py`, add to the imports (next to the existing `from thump.models import Check, CheckType`):

```python
from thump.schedule import CronSchedule, ScheduleError
```

- [ ] **Step 4: Replace the interval-required block with the exactly-one-of rules**

In `src/thump/config.py`, the current block is:

```python
        if "interval" not in raw:
            errors.append(f"{where}: interval is required")
            continue
        interval = _duration(raw, "interval", timedelta(hours=1), errors, where)
```

Replace it with:

```python
        has_interval = "interval" in raw
        has_schedule = "schedule" in raw
        schedule: CronSchedule | None = None

        if ctype is CheckType.PROBE:
            if has_schedule:
                errors.append(f"{where}: schedule is only valid for heartbeat checks")
            if not has_interval:
                errors.append(f"{where}: interval is required")
                continue
        else:
            if has_interval and has_schedule:
                errors.append(f"{where}: interval and schedule are mutually exclusive")
                continue
            if not has_interval and not has_schedule:
                errors.append(f"{where}: heartbeat requires either interval or schedule")
                continue
            if has_schedule:
                try:
                    schedule = CronSchedule.parse(str(raw["schedule"]), server.timezone)
                except ScheduleError as e:
                    errors.append(f"{where}: {e}")
                    continue

        interval = (
            _duration(raw, "interval", timedelta(hours=1), errors, where)
            if has_interval
            else None
        )
```

- [ ] **Step 5: Pass `schedule` into the `Check`**

In the same file, in the `Check(...)` construction, add the `schedule` argument after `expect_status`:

```python
                expect_status=_int(raw, "expect_status", 200, errors, where),
                schedule=schedule,
```

- [ ] **Step 6: Run the config tests**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: all pass — the 6 new cases plus every existing config case.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: 135 passed (129 + 6 new).

- [ ] **Step 8: Commit**

```bash
git add src/thump/config.py tests/test_config.py
git commit -m "feat: accept cron schedules in config, validated against server.timezone

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Documentation

The feature is invisible without this. The README's existing "Operational notes" section is where the non-obvious behaviors already live.

**Files:**
- Modify: `README.md`, `config.example.yaml`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Inspect the example config**

Run: `cat config.example.yaml`

Identify the first `type: heartbeat` entry. The next step edits it.

- [ ] **Step 2: Add a cron heartbeat to the example config**

In `config.example.yaml`, add a new check alongside the existing ones (adjust indentation to match the file's existing `checks:` entries):

```yaml
  # Deadline anchored to when the job SHOULD run, not to when it was last seen.
  # Requires server.timezone; mutually exclusive with `interval`.
  - name: weekday-report
    type: heartbeat
    schedule: "0 6 * * 1-5"   # 06:00 on weekdays, in server.timezone
    grace: 30m
```

- [ ] **Step 3: Document `schedule:` in the README**

In `README.md`, add this subsection immediately before the `## Operational notes` heading:

````markdown
## Scheduling a heartbeat

A heartbeat's deadline can be a duration or a cron expression:

```yaml
server:
  timezone: Europe/London     # cron expressions are evaluated here

checks:
  - name: nightly-backup
    type: heartbeat
    schedule: "0 2 * * *"     # 02:00 local, every day
    grace: 30m
```

`interval` and `schedule` are mutually exclusive on a heartbeat — setting both, or
neither, is a config error. Probes always use `interval`, as a poll frequency.

Prefer `schedule` for anything driven by cron. `interval: 24h` measures 24 hours from
the *last ping*, so ordinary jitter walks the deadline forward until a healthy job
pages you; it is wrong by an hour on both DST transitions; and it cannot express
`0 2 * * 1-5` at all, because the Friday→Monday gap is 72 hours while every other gap
is 24.

A check with `schedule` is DOWN when the most recent occurrence whose grace has
expired was not covered by a ping.
````

- [ ] **Step 4: Document the tolerance and DST in Operational notes**

In `README.md`, add these two bullets to the end of the existing `## Operational notes` list:

```markdown
- **A ping up to 60s early still counts.** If the cron host's clock runs slightly
  ahead, a `0 2 * * *` job can check in at 01:59:30 — before its own occurrence. That
  ping covers it. The tolerance is fixed and not configurable: clock skew is an
  environmental defect with a fixed remedy (NTP), not a per-check policy.
- **DST is handled by the schedule, not by you.** Occurrences are computed in
  `server.timezone`, so a 25-hour day has 25 hourly occurrences and a spring-forward
  day moves a missing `0 2 * * *` to 03:00 — matching cron itself. No grace padding is
  needed for either transition.
```

- [ ] **Step 5: Verify the docs match reality**

Run: `.venv/bin/pytest -q`
Expected: 135 passed — unchanged; this task adds no tests and changes no code.

Then confirm the example config actually loads:

```bash
.venv/bin/python -c "
from thump.config import load_config
cfg = load_config('config.example.yaml', {'THUMP_SECRET':'x','THUMP_ADMIN_TOKEN':'y'})
for c in cfg.checks:
    print(c.name, '| interval:', c.interval, '| schedule:', c.schedule.expr if c.schedule else None)
"
```

Expected: every check prints, with `weekday-report` showing `schedule: 0 6 * * 1-5` and `interval: None`. If it raises `ConfigError`, the example config's env placeholders differ — read the error and fix the example, not the loader.

- [ ] **Step 6: Commit**

```bash
git add README.md config.example.yaml
git commit -m "docs: document cron schedules, early tolerance, and DST behavior

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `schedule:` on heartbeat checks | 3 |
| Out of scope: cron for probes (rejected in config) | 3 |
| Deadline formula `prev_at_or_before(now - grace)` | 2 |
| `last_due is None` → UP | 1 (`test_prev_returns_none_when_no_occurrence_precedes_the_instant`), 2 (guard in evaluator) |
| `EARLY_TOLERANCE` = flat 60s, not configurable | 2 |
| DST: spring forward → 03:00 | 1 (pinned), 2 (evaluated) |
| DST: fall back, 02:00 once; repeated hour fires once | 1 (pinned), 2 (evaluated) |
| No DST-specific mitigation | Absent by construction — no such code in any task |
| tz-aware UTC at module boundaries | 1 (`test_prev_returns_tz_aware_utc...`) |
| Reverse iterator is strictly-before; seed +1s | 1 (`test_prev_includes_an_instant_exactly_on_an_occurrence`) |
| `CronSchedule` wrapper keeps cronsim out of evaluator | 1 |
| `ScheduleError` → collected `ConfigError` | 3 |
| `interval` widens to `timedelta \| None`; `schedule` added | 2 |
| Exactly-one-of validation | 3 |
| `cronsim` added to dependencies | 1 |
| README + example config | 4 |
| Existing 104 tests stay green | Verified at the end of every task |
| `evaluate()` stays pure, no `assert` | 2 (explicit `raise ValueError`) |

No spec requirement is unaddressed.

**Placeholder scan:** No TBDs. Every code step contains complete, runnable code or an exact before/after edit. Task 4 Step 1 is an inspection step whose output feeds Step 2, which shows the full YAML to add.

**Type consistency:**
- `CronSchedule.parse(expr: str, tz: ZoneInfo) -> CronSchedule` — defined Task 1, called identically in Task 2 tests and Task 3 config.
- `prev_at_or_before(dt: datetime) -> datetime | None` — defined Task 1, called in Task 2 evaluator with `now - check.grace`.
- `ScheduleError` — raised Task 1, caught Task 3.
- `Check.schedule: CronSchedule | None` — added Task 2, populated Task 3.
- `Check.interval: timedelta | None` — widened Task 2; the only other consumer is `scheduler.py:78`, which handles probes exclusively, and probes still always carry an `interval` (enforced Task 3). No change needed there.
- `EARLY_TOLERANCE` — defined Task 2, used only in Task 2.

**Import-cycle check:** `models.py` imports `schedule.py`; `schedule.py` imports only `cronsim`, `dataclasses`, `datetime`, `zoneinfo`. No cycle. `config.py` already imports `models`, and now also `schedule`. `evaluator.py` imports only `models`, so it never sees `cronsim` — the spec's isolation requirement holds.
