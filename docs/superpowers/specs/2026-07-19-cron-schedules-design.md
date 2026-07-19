# Cron-expression schedules — Design

**Date:** 2026-07-19
**Status:** Approved for planning

## Problem

`thump` today expresses a heartbeat deadline as a duration: `interval: 24h` plus a
`grace` window. The evaluator marks a check DOWN when
`now - last_seen > interval + grace` (`src/thump/evaluator.py:31`).

For the system's primary use case — monitoring cron jobs — this is the wrong model,
in three distinct ways.

1. **The deadline drifts with the observation.** A backup running at `0 2 * * *`
   configured as `interval: 24h, grace: 5m` gets an effective deadline of 02:05. The
   deadline is anchored to when the job was last *seen*, not to when it was
   *supposed to run*, so ordinary jitter walks the window forward until a healthy
   job pages.
2. **DST breaks it outright.** On a spring-forward Sunday the gap between two
   consecutive 02:00 runs is 23 hours; on fall-back it is 25. A fixed 24-hour
   interval is wrong twice a year, in opposite directions, and the fall-back case
   pages at 3am.
3. **No duration can express a real schedule.** `0 2 * * 1-5` (weekdays only) has no
   single interval: the Friday→Monday gap is 72 hours and the rest are 24. Operators
   must set `grace` to cover the longest gap, which blinds the check for the other
   four days.

The original design anticipated this. `server.timezone` already exists and is parsed
(`src/thump/config.py:132`) but is currently unused — it was put there as the hook
this feature hangs on.

## Solution

Add an optional `schedule:` field to heartbeat checks, accepting a standard cron
expression, evaluated in `server.timezone`:

```yaml
server:
  timezone: Europe/London

checks:
  - name: nightly-backup
    type: heartbeat
    schedule: "0 2 * * *"
    grace: 30m
```

The check is DOWN when the most recent occurrence whose grace has expired was not
covered by a ping.

### Scope

**In scope:** cron schedules for `heartbeat` checks.

**Out of scope:** cron schedules for `probe` checks. A cron answers "when was this
job supposed to run?", which is meaningful only for a dead-man's switch. A probe's
`interval` is a polling frequency, where a duration is the correct model. Adding
cron to probes would require rewriting the scheduler's `asyncio.sleep(interval)` loop
into a sleep-until-next-occurrence loop, with catch-up and drift handling on restart.
The `CronSchedule` interface below is designed so this remains a later addition
rather than a rewrite, per the project's YAGNI stance.

## Semantics

### Deadline formulation

Given a check with a schedule, its `grace`, the stored `last_seen`, and `now`:

```
last_due = schedule.prev_at_or_before(now - grace)
DOWN  if  last_due is not None and last_seen < last_due - EARLY_TOLERANCE
UP    otherwise
```

Read aloud: *has the most recent occurrence whose grace period has already expired
been covered by a ping?*

This is deliberately anchored to `now` rather than to `last_seen`. The rejected
alternative — `due = schedule.next_after(last_seen)`, DOWN when `now > due + grace` —
behaves identically on the normal path but degrades during a long outage, where it
measures against a deadline derived from an increasingly stale `last_seen`. The
chosen formulation asks the same question regardless of how long ago the last ping
arrived, which is also how an operator reasons about it during an incident.

`last_due is None` means no occurrence has yet come due (a schedule whose first
occurrence is still in the future). The check is UP, consistent with the existing
`pending`-is-healthy stance documented in the README.

### Early-ping tolerance

If the cron host's clock runs slightly ahead of the monitor's, a `0 2 * * *` job can
ping at 01:59:30 — before its own occurrence. Without accommodation that ping fails
to cover the 02:00 occurrence and the check goes DOWN at 02:30 despite the job having
run correctly. This is the monitor manufacturing a 2am page, the exact failure this
project exists to prevent.

A ping therefore counts for occurrence `O` when `last_seen >= O - EARLY_TOLERANCE`,
where `EARLY_TOLERANCE` is a module-level constant of 60 seconds in `evaluator.py`.

It is deliberately not configurable. It corrects for clock skew, which is an
environmental defect with a fixed remedy (NTP), not a per-check policy an operator
should be tuning. The constant is documented in the README.

### DST

Occurrences are computed in `server.timezone`, so `0 2 * * *` means 02:00 local on
every calendar day, and the interval between occurrences is correctly 23 or 25 hours
across a transition.

Both behaviors below were verified empirically against `cronsim` 2.7 with
`America/New_York`, rather than assumed:

- **Spring forward (2025-03-09):** 02:00 does not exist. `cronsim` does not skip the
  day — it yields `03:00 -04:00` (07:00 UTC), exactly 24h after the previous
  occurrence. This matches systemd-timer behavior and Vixie cron. No special handling
  needed.
- **Fall back (2025-11-02):** `cronsim` yields `02:00 -05:00` (07:00 UTC), once.

Note that 02:00 is **not** in the repeated hour. At 02:00 EDT the clock jumps back to
01:00 EST, so the wall-clock times that occur twice are 01:00–01:59. Wall-clock 02:00
occurs exactly once, and the day is 25 hours long.

For a schedule *inside* the repeated hour, `cronsim` fires once, at the first
(pre-transition) instant:

- `0 1 * * *` on 2025-11-02 yields `01:00 -04:00` (05:00 UTC) only — not the second
  01:00. This matches Vixie cron, which also runs a repeated-hour job once.
- `0 * * * *` correctly yields **both** `01:00 -04:00` and `01:00 -05:00`, giving a
  25-hour day.

**No DST false-positive mitigation is required.** An earlier draft of this spec
posited a one-hour disagreement between the host's cron and `cronsim` on fall-back
night, and specified first a `grace: 1h` workaround and then a DST-aware tolerance to
correct it. Both were removed: the premise was wrong. It assumed 02:00 was the
repeated hour, and it assumed cron implementations disagree about the repeated hour.
Neither holds. `EARLY_TOLERANCE` stays a flat 60 seconds, and no occurrence-specific
widening exists.

What this history does justify is **pinning the DST behaviors as tests** (see
Testing), so that a future `cronsim` upgrade which changes any of them fails in CI
rather than silently at 2am.

All datetimes crossing the module boundary remain timezone-aware UTC, preserving the
existing project-wide invariant. The local timezone is an implementation detail
internal to `CronSchedule`.

## Architecture

### New module: `src/thump/schedule.py`

```python
@dataclass(frozen=True, slots=True)
class CronSchedule:
    expr: str
    tz: ZoneInfo

    def prev_at_or_before(self, dt: datetime) -> datetime | None: ...
```

`prev_at_or_before` converts `dt` into `tz`, iterates `cronsim` in reverse, and
returns a tz-aware UTC datetime (or `None` if no occurrence precedes `dt`).

**Critical implementation detail — the reverse iterator is strictly *before*.**
Seeding `CronSim(expr, dt, reverse=True)` with a `dt` that falls exactly on an
occurrence returns the *previous* one: seeding at `02:00` on a `0 2 * * *` schedule
yields the prior day's `02:00`, not today's. Since the deadline formula evaluates
`prev_at_or_before(now - grace)`, and `now - grace` lands exactly on an occurrence
once per period, the naive implementation would resolve the deadline a full day late
on that boundary. The wrapper therefore seeds from `dt + 1 second` to obtain
*at-or-before* semantics. This is verified by a boundary test.

This wrapper exists so that `evaluator.py` — the pure correctness core — never
imports a third-party library, and so the cron behavior is testable in isolation from
check evaluation. It is also the seam through which probe scheduling could later
obtain `next_after()` without disturbing anything else.

Expressions are parsed once at config load. A `CronSimError` is converted into a
collected `ConfigError` entry, so a malformed expression is fatal at boot rather than
at 2am — consistent with the project's existing fail-fast configuration contract.

### Dependency: `cronsim`

Selected over `croniter` and over hand-rolling:

- **Reverse iteration** (`CronSim(expr, dt, reverse=True)`) directly supports the
  chosen deadline formulation. Forward-only parsers would force a scan.
- **Zero runtime dependencies**, ships `py.typed`, 549 lines of core logic. `croniter`
  pulls in `python-dateutil` and has a history of DST bugs.
- **Explicit DST handling** via `is_imaginary()`.
- Written by the author of Healthchecks.io — the same problem domain, so its edge
  cases were found by precisely this use case.
- Hand-rolling was rejected: DST correctness plus the `L`/`#` operators is a genuinely
  subtle parser, and owning it buys nothing.

Added to `[project.dependencies]` in `pyproject.toml`.

### Changed modules

| File | Change |
|---|---|
| `src/thump/schedule.py` | Create. `CronSchedule` wrapper + parse helper. |
| `src/thump/models.py` | `Check.interval` widens to `timedelta \| None`; add `schedule: CronSchedule \| None = None`. |
| `src/thump/config.py` | Heartbeats require exactly one of `interval`/`schedule`; parse expression against `server.timezone`. |
| `src/thump/evaluator.py` | Heartbeat branch forks on `check.schedule`; add `EARLY_TOLERANCE`. |
| `pyproject.toml` | Add `cronsim` dependency. |
| `README.md` | Document `schedule:`, the tolerance constant, and DST behavior. |

`scheduler.py`, `api.py`, `store/`, `metrics.py`, and `tokens.py` are untouched.

### Configuration contract

For `type: heartbeat`, exactly one of `interval` or `schedule` is required. Setting
both, or neither, is a collected `ConfigError`. This mirrors the existing rule that a
heartbeat must not carry a `url` (`src/thump/config.py:175`).

The rejected alternative — letting `schedule` silently win when both are present —
was declined because it introduces a precedence rule operators must memorize and lets
a stale `interval` sit in the file misrepresenting the check's behavior indefinitely.

For `type: probe`, `interval` remains required and `schedule` is rejected as a
`ConfigError`. Every existing configuration file continues to work unchanged.

### Evaluator

```python
if check.schedule is not None:
    last_due = check.schedule.prev_at_or_before(now - check.grace)
    if last_due is not None and state.last_seen < last_due - EARLY_TOLERANCE:
        return State.DOWN
    return State.UP
# existing interval path, unchanged
```

`evaluate()` remains pure: no network, no database, no clock reads. `now` stays a
parameter. The existing `last_seen is None` fail-loud guard
(`src/thump/evaluator.py:25-30`) precedes this fork and continues to protect both
paths.

## Testing

- **`tests/test_schedule.py`** (new): expression parsing, rejection of malformed
  expressions, `prev_at_or_before` returning tz-aware UTC, `None` before the first
  occurrence, and DST behavior for `0 2 * * *` across both a spring-forward and a
  fall-back Sunday in `America/New_York` — pinning the verified occurrences (`03:00
  -04:00` and `02:00 -05:00` respectively) so that a future `cronsim` upgrade which
  changes them fails loudly in CI rather than silently at 2am.
- **`tests/test_schedule.py`** additionally pins the repeated-hour behavior: `0 1 * * *`
  on 2025-11-02 yields exactly one occurrence (05:00 UTC), and `0 * * * *` yields both
  01:00 instants.
- **`tests/test_evaluator.py`**: cron-heartbeat cases — healthy ping, missed
  occurrence, ping arriving early but inside tolerance, ping early beyond tolerance,
  and stability across a multi-day outage. Existing interval cases must be unaffected.
- **`tests/test_config.py`**: both fields set, neither set, `schedule` on a probe,
  malformed expression collected as `ConfigError`, and expression parsed against a
  non-UTC `server.timezone`.

The 104 existing tests must remain green. No existing behavior changes: every current
config uses `interval`, and that path is untouched.

## Success criteria

1. A `0 2 * * *` heartbeat with `grace: 30m` reports UP when its job pings anywhere in
   01:59–02:30 local, and DOWN once 02:30 passes with no ping.
2. The same check does not report DOWN on either DST transition day, with no
   schedule-specific or grace-specific accommodation.
3. `0 2 * * 1-5` does not report DOWN over a weekend.
4. A malformed expression fails at boot with a message naming the check.
5. Existing `interval`-based configs behave identically to today.
