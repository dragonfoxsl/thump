# task-monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a container-deployable middleware that accepts cron-job heartbeats and probes private-network endpoints, then re-exposes both as plain `200`/`503` HTTP endpoints an external uptime vendor can poll.

**Architecture:** One FastAPI process over a swappable `Store` (SQLite default, Redis for multi-replica). Ingest and Scheduler only *write* state; Status only *reads* it and calls a **pure** `evaluate(check, state, now) -> State` function that performs no I/O. `down` is therefore computed at read time, so a stale heartbeat cannot be missed because a background sweep died.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, PyYAML, redis-py (async), stdlib `sqlite3`. Tests: pytest, pytest-asyncio, fakeredis.

**Spec:** `docs/superpowers/specs/2026-07-15-task-monitor-design.md`

## Global Constraints

- **Package root:** `src/tskmon/`. Tests in `tests/`. Import as `from tskmon.x import y`.
- **All datetimes are timezone-aware UTC.** Never use `datetime.utcnow()` (it returns naive). Use `datetime.now(timezone.utc)`.
- **`server.timezone` is DISPLAY ONLY.** It affects rendering in `/checks` and logs. If it ever affects evaluation, that is a bug.
- **The clock is injected.** Every component that needs the current time takes a `clock: Callable[[], datetime]` defaulting to `lambda: datetime.now(timezone.utc)`. This is what makes the evaluator tests fast and non-flaky.
- **`evaluate()` performs no I/O.** No network, no database, no `datetime.now()` inside it.
- **`pending` counts as healthy** (returns 200). Deliberate — see spec.
- **Unknown ping token returns `404`, never `401`** — an attacker must not be able to distinguish "wrong token" from "no such check".
- **Status endpoints leak nothing** — body is literally `up` or `down`. No JSON, no names, no timestamps.
- **Fail closed:** if `server.admin_token` is unset, `/checks` and `/metrics` are **disabled** (404), not open.
- **Commit after every task.**

---

## File Structure

| File | Responsibility |
|---|---|
| `src/tskmon/models.py` | Domain types: `State`, `CheckType`, `Check`, `CheckState`, `Event`. No logic. |
| `src/tskmon/evaluator.py` | The pure decision function. The correctness surface. |
| `src/tskmon/tokens.py` | HMAC token derivation. |
| `src/tskmon/config.py` | YAML load, env expansion, defaults merge, validation. |
| `src/tskmon/store/base.py` | `Store` protocol + `StoreUnavailable`. |
| `src/tskmon/store/sqlite.py` | Default store. |
| `src/tskmon/store/redis.py` | Multi-replica store. |
| `src/tskmon/metrics.py` | Pure Prometheus text renderer. |
| `src/tskmon/api.py` | FastAPI app: ingest, status, observability, healthz. |
| `src/tskmon/scheduler.py` | Async probe loop. |
| `src/tskmon/main.py` | Entrypoint wiring. |

---

### Task 1: Scaffold + domain models

**Files:**
- Create: `pyproject.toml`, `src/tskmon/__init__.py`, `src/tskmon/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `State` (`UP`/`DOWN`/`PENDING`/`PAUSED`), `CheckType` (`HEARTBEAT`/`PROBE`), `Check`, `CheckState`, `Event`. Every later task imports these.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "tskmon"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "redis>=5.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "fakeredis>=2.26",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tskmon"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
```

- [ ] **Step 2: Install**

Run: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`
Expected: `Successfully installed tskmon-0.1.0 ...`

- [ ] **Step 3: Write the failing test**

Create `tests/test_models.py`:

```python
from datetime import timedelta

from tskmon.models import Check, CheckState, CheckType, Event, State


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
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.models'`

- [ ] **Step 5: Write the implementation**

Create `src/tskmon/__init__.py` (empty file).

Create `src/tskmon/models.py`:

```python
"""Domain types. No logic lives here."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


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
    interval: timedelta
    grace: timedelta
    timeout: timedelta
    history: int
    failure_threshold: int
    token: str
    enabled: bool = True
    url: str | None = None
    expect_status: int = 200


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
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
printf '.venv/\n__pycache__/\n*.pyc\n*.db\n' > .gitignore
git add pyproject.toml .gitignore src/tskmon/__init__.py src/tskmon/models.py tests/test_models.py
git commit -m "feat: scaffold project and domain models"
```

---

### Task 2: The evaluator (pure decision function)

This is the correctness surface of the entire system. It gets the most tests.

**Files:**
- Create: `src/tskmon/evaluator.py`
- Test: `tests/test_evaluator.py`

**Interfaces:**
- Consumes: `Check`, `CheckState`, `State`, `CheckType` from `tskmon.models`.
- Produces: `evaluate(check: Check, state: CheckState, now: datetime) -> State`. Called by `api.py` on every status request and by `metrics.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evaluator.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_evaluator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.evaluator'`

- [ ] **Step 3: Write the implementation**

Create `src/tskmon/evaluator.py`:

```python
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
        assert state.last_seen is not None  # implied by last_result_ok is True
        if now - state.last_seen > check.interval + check.grace:
            return State.DOWN
        return State.UP

    # Probe: down only because probes failed, never because time passed.
    if state.consecutive_failures >= check.failure_threshold:
        return State.DOWN
    return State.UP
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_evaluator.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add src/tskmon/evaluator.py tests/test_evaluator.py
git commit -m "feat: pure evaluator — the correctness surface"
```

---

### Task 3: HMAC token derivation

**Files:**
- Create: `src/tskmon/tokens.py`
- Test: `tests/test_tokens.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `derive_token(secret: str, name: str) -> str` (32 lowercase hex chars). Used by `config.py` when a check declares no explicit `token`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tokens.py`:

```python
from tskmon.tokens import derive_token


def test_token_is_32_hex_chars():
    t = derive_token("s3cret", "nightly-db-backup")
    assert len(t) == 32
    assert all(c in "0123456789abcdef" for c in t)


def test_token_is_stable_across_calls():
    # Must survive restarts: the cron's URL cannot change under it.
    a = derive_token("s3cret", "nightly-db-backup")
    b = derive_token("s3cret", "nightly-db-backup")
    assert a == b


def test_token_differs_per_check_name():
    a = derive_token("s3cret", "nightly-db-backup")
    b = derive_token("s3cret", "legacy-etl")
    assert a != b


def test_token_differs_per_secret():
    # Rotating the secret rotates every token.
    a = derive_token("s3cret", "nightly-db-backup")
    b = derive_token("other", "nightly-db-backup")
    assert a != b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_tokens.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.tokens'`

- [ ] **Step 3: Write the implementation**

Create `src/tskmon/tokens.py`:

```python
"""Ping tokens: the URL *is* the credential.

Derived rather than stored, so there is no token table and only one thing
(the server secret) to rotate.
"""

import hashlib
import hmac


def derive_token(secret: str, name: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), name.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:32]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_tokens.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/tskmon/tokens.py tests/test_tokens.py
git commit -m "feat: HMAC-derived ping tokens"
```

---

### Task 4: Config loading, env expansion, and validation

**Files:**
- Create: `src/tskmon/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `Check`, `CheckType` from `tskmon.models`; `derive_token` from `tskmon.tokens`.
- Produces:
  - `parse_duration(s: str) -> timedelta`
  - `ConfigError(Exception)` with `.errors: list[str]`
  - `StoreConfig(driver: str, dsn: str)`
  - `ServerConfig(listen: str, secret: str, admin_token: str | None, timezone: ZoneInfo)`
  - `Config(store, server, checks: tuple[Check, ...])` with `.by_name: dict[str, Check]` and `.by_token: dict[str, Check]`
  - `parse_config(text: str, env: Mapping[str, str]) -> Config`
  - `load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from tskmon.config import ConfigError, parse_config, parse_duration
from tskmon.models import CheckType
from tskmon.tokens import derive_token

MINIMAL = """
store:
  driver: sqlite
  dsn: /var/lib/tskmon/state.db
server:
  listen: ":8080"
  secret: ${TSKMON_SECRET}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""

ENV = {"TSKMON_SECRET": "s3cret", "TSKMON_ADMIN_TOKEN": "admin-tok"}


def test_parse_duration_units():
    assert parse_duration("30s") == timedelta(seconds=30)
    assert parse_duration("5m") == timedelta(minutes=5)
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_env_expansion_keeps_secret_out_of_the_file():
    cfg = parse_config(MINIMAL, ENV)
    assert cfg.server.secret == "s3cret"


def test_missing_env_var_is_fatal():
    with pytest.raises(ConfigError):
        parse_config(MINIMAL, {})


def test_defaults_are_applied():
    cfg = parse_config(MINIMAL, ENV)
    c = cfg.by_name["nightly-db-backup"]
    assert c.grace == timedelta(minutes=5)
    assert c.timeout == timedelta(seconds=10)
    assert c.history == 100
    assert c.failure_threshold == 2
    assert c.enabled is True


def test_per_check_override_beats_default():
    text = MINIMAL + "    grace: 1h\n    history: 20\n"
    c = parse_config(text, ENV).by_name["nightly-db-backup"]
    assert c.grace == timedelta(hours=1)
    assert c.history == 20


def test_token_is_derived_when_not_declared():
    cfg = parse_config(MINIMAL, ENV)
    c = cfg.by_name["nightly-db-backup"]
    assert c.token == derive_token("s3cret", "nightly-db-backup")
    assert cfg.by_token[c.token] is c


def test_explicit_token_overrides_derived():
    text = MINIMAL + "    token: 7c9f2a\n"
    cfg = parse_config(text, ENV)
    assert cfg.by_name["nightly-db-backup"].token == "7c9f2a"
    assert "7c9f2a" in cfg.by_token


def test_timezone_defaults_to_utc():
    assert parse_config(MINIMAL, ENV).server.timezone == ZoneInfo("UTC")


def test_timezone_is_parsed():
    text = MINIMAL.replace(
        'secret: ${TSKMON_SECRET}',
        'secret: ${TSKMON_SECRET}\n  timezone: Asia/Kolkata',
    )
    assert parse_config(text, ENV).server.timezone == ZoneInfo("Asia/Kolkata")


def test_unknown_timezone_is_fatal():
    text = MINIMAL.replace(
        'secret: ${TSKMON_SECRET}',
        'secret: ${TSKMON_SECRET}\n  timezone: Mars/Olympus',
    )
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_admin_token_is_optional_and_none_when_absent():
    assert parse_config(MINIMAL, ENV).server.admin_token is None


def test_probe_check_is_parsed():
    text = """
store: {driver: sqlite, dsn: ./s.db}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: internal-payments-api
    type: probe
    url: http://payments.internal:8080/healthz
    interval: 60s
    expect_status: 204
"""
    c = parse_config(text, ENV).by_name["internal-payments-api"]
    assert c.type is CheckType.PROBE
    assert c.url == "http://payments.internal:8080/healthz"
    assert c.expect_status == 204


# --- validation: a half-configured monitor is worse than one that won't start ---

def test_duplicate_check_names_are_fatal():
    text = MINIMAL + "  - name: nightly-db-backup\n    type: heartbeat\n    interval: 1h\n"
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_probe_without_url_is_fatal():
    text = """
store: {driver: sqlite, dsn: ./s.db}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: p
    type: probe
    interval: 60s
"""
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_heartbeat_with_url_is_fatal():
    text = MINIMAL + "    url: http://nope\n"
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_unknown_store_driver_is_fatal():
    with pytest.raises(ConfigError):
        parse_config(MINIMAL.replace("driver: sqlite", "driver: mongodb"), ENV)


def test_all_errors_are_reported_together():
    text = """
store: {driver: mongodb, dsn: ./s.db}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: p
    type: probe
    interval: 60s
"""
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert len(exc.value.errors) == 2  # bad driver AND probe-without-url
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.config'`

- [ ] **Step 3: Write the implementation**

Create `src/tskmon/config.py`:

```python
"""YAML is the single source of truth. Invalid config is fatal at boot.

A monitor that starts half-configured and silently fails to watch something is
worse than one that refuses to start: the first failure mode is invisible, the
second is a CrashLoopBackOff noticed in thirty seconds.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from tskmon.models import Check, CheckType
from tskmon.tokens import derive_token

VALID_DRIVERS = ("sqlite", "redis")

_DURATION_RE = re.compile(r"(\d+)([smhd])")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class ConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid config:\n  - " + "\n  - ".join(errors))


def parse_duration(s: str) -> timedelta:
    text = str(s).strip()
    if not text or not _DURATION_RE.fullmatch(text) and not _DURATION_RE.match(text):
        raise ValueError(f"invalid duration: {s!r}")
    parts = _DURATION_RE.findall(text)
    if "".join(n + u for n, u in parts) != text:
        raise ValueError(f"invalid duration: {s!r}")
    total = timedelta()
    for number, unit in parts:
        total += timedelta(**{_UNITS[unit]: int(number)})
    return total


@dataclass(frozen=True, slots=True)
class StoreConfig:
    driver: str
    dsn: str


@dataclass(frozen=True, slots=True)
class ServerConfig:
    listen: str
    secret: str
    admin_token: str | None
    timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class Config:
    store: StoreConfig
    server: ServerConfig
    checks: tuple[Check, ...]

    @property
    def by_name(self) -> dict[str, Check]:
        return {c.name: c for c in self.checks}

    @property
    def by_token(self) -> dict[str, Check]:
        return {c.token: c for c in self.checks}


def _expand_env(text: str, env: Mapping[str, str], errors: list[str]) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in env:
            errors.append(f"environment variable {name} is not set")
            return ""
        return env[name]

    return _ENV_RE.sub(sub, text)


def _duration(raw: dict[str, Any], key: str, default: timedelta, errors: list[str], where: str) -> timedelta:
    if key not in raw:
        return default
    try:
        return parse_duration(raw[key])
    except ValueError as e:
        errors.append(f"{where}: {e}")
        return default


def parse_config(text: str, env: Mapping[str, str]) -> Config:
    errors: list[str] = []
    expanded = _expand_env(text, env, errors)
    if errors:
        raise ConfigError(errors)

    doc = yaml.safe_load(expanded) or {}

    raw_store = doc.get("store") or {}
    driver = str(raw_store.get("driver", "sqlite"))
    if driver not in VALID_DRIVERS:
        errors.append(f"store.driver must be one of {VALID_DRIVERS}, got {driver!r}")
    store = StoreConfig(driver=driver, dsn=str(raw_store.get("dsn", "./tskmon.db")))

    raw_server = doc.get("server") or {}
    secret = raw_server.get("secret")
    if not secret:
        errors.append("server.secret is required")
    tz_name = str(raw_server.get("timezone", "UTC"))
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"server.timezone: unknown IANA timezone {tz_name!r}")
        tz = ZoneInfo("UTC")
    admin = raw_server.get("admin_token") or None
    server = ServerConfig(
        listen=str(raw_server.get("listen", ":8080")),
        secret=str(secret or ""),
        admin_token=str(admin) if admin else None,
        timezone=tz,
    )

    d = doc.get("defaults") or {}
    def_grace = _duration(d, "grace", timedelta(minutes=5), errors, "defaults.grace")
    def_timeout = _duration(d, "timeout", timedelta(seconds=10), errors, "defaults.timeout")
    def_history = int(d.get("history", 100))
    def_threshold = int(d.get("failure_threshold", 2))

    checks: list[Check] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc.get("checks") or []):
        where = f"checks[{i}]"
        name = raw.get("name")
        if not name:
            errors.append(f"{where}: name is required")
            continue
        where = f"check {name!r}"
        if name in seen:
            errors.append(f"{where}: duplicate check name")
            continue
        seen.add(name)

        type_raw = str(raw.get("type", ""))
        if type_raw not in ("heartbeat", "probe"):
            errors.append(f"{where}: type must be 'heartbeat' or 'probe', got {type_raw!r}")
            continue
        ctype = CheckType(type_raw)

        url = raw.get("url")
        if ctype is CheckType.PROBE and not url:
            errors.append(f"{where}: probe checks require a url")
        if ctype is CheckType.HEARTBEAT and url:
            errors.append(f"{where}: heartbeat checks must not have a url")

        if "interval" not in raw:
            errors.append(f"{where}: interval is required")
            continue
        interval = _duration(raw, "interval", timedelta(hours=1), errors, where)

        checks.append(
            Check(
                name=str(name),
                type=ctype,
                interval=interval,
                grace=_duration(raw, "grace", def_grace, errors, where),
                timeout=_duration(raw, "timeout", def_timeout, errors, where),
                history=int(raw.get("history", def_history)),
                failure_threshold=int(raw.get("failure_threshold", def_threshold)),
                token=str(raw.get("token") or derive_token(server.secret, str(name))),
                enabled=bool(raw.get("enabled", True)),
                url=str(url) if url else None,
                expect_status=int(raw.get("expect_status", 200)),
            )
        )

    if errors:
        raise ConfigError(errors)
    return Config(store=store, server=server, checks=tuple(checks))


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    import os

    return parse_config(Path(path).read_text(), env if env is not None else os.environ)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add src/tskmon/config.py tests/test_config.py
git commit -m "feat: YAML config with env expansion and fail-fast validation"
```

---

### Task 5: Store protocol + SQLite implementation + shared conformance suite

The conformance suite is the deliverable that keeps the abstraction honest. Task 6 reuses it verbatim against Redis.

**Files:**
- Create: `src/tskmon/store/__init__.py`, `src/tskmon/store/base.py`, `src/tskmon/store/sqlite.py`
- Test: `tests/store/__init__.py`, `tests/store/test_conformance.py`

**Interfaces:**
- Consumes: `CheckState`, `Event` from `tskmon.models`.
- Produces:
  - `StoreUnavailable(Exception)`
  - `Store` protocol:
    - `async connect() -> None`
    - `async close() -> None`
    - `async healthy() -> bool`
    - `async get_state(name: str) -> CheckState`
    - `async get_states(names: Sequence[str]) -> dict[str, CheckState]`
    - `async record_success(name: str, at: datetime, event: Event, history: int) -> None`
    - `async record_failure(name: str, at: datetime, event: Event, history: int) -> None`
    - `async get_events(name: str, limit: int) -> list[Event]` (newest first)
  - `SqliteStore(dsn: str)`

Semantics both implementations must satisfy:
- `record_success`: `last_seen = at`, `last_result_ok = True`, `consecutive_failures = 0`, push event.
- `record_failure`: `last_result_ok = False`, `consecutive_failures += 1`, **`last_seen` unchanged**, push event.
- Event list is trimmed to `history` entries, newest first.
- `get_state` for an unknown name returns a default `CheckState()` — never raises.

- [ ] **Step 1: Write the failing conformance suite**

Create `tests/store/__init__.py` (empty).

Create `tests/store/test_conformance.py`:

```python
"""One suite, run against every Store implementation.

This is the only thing keeping the abstraction honest rather than quietly
SQLite-shaped.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tskmon.models import Event
from tskmon.store.sqlite import SqliteStore

T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStore(str(tmp_path / "state.db"))
    else:  # pragma: no cover - added in Task 6
        raise AssertionError(f"unknown store {request.param}")
    await s.connect()
    yield s
    await s.close()


async def test_unknown_check_returns_unobserved_state(store):
    s = await store.get_state("never-heard-of-it")
    assert s.last_seen is None
    assert s.last_result_ok is None
    assert s.consecutive_failures == 0


async def test_record_success_sets_last_seen_and_clears_failures(store):
    await store.record_failure("c", T0, Event(at=T0, kind="probe_fail"), 100)
    await store.record_success("c", T0, Event(at=T0, kind="probe_ok"), 100)
    s = await store.get_state("c")
    assert s.last_seen == T0
    assert s.last_result_ok is True
    assert s.consecutive_failures == 0


async def test_record_failure_increments_and_preserves_last_seen(store):
    await store.record_success("c", T0, Event(at=T0, kind="ping"), 100)
    t1 = T0 + timedelta(minutes=1)
    await store.record_failure("c", t1, Event(at=t1, kind="probe_fail"), 100)
    t2 = T0 + timedelta(minutes=2)
    await store.record_failure("c", t2, Event(at=t2, kind="probe_fail"), 100)

    s = await store.get_state("c")
    assert s.consecutive_failures == 2
    assert s.last_result_ok is False
    assert s.last_seen == T0  # unchanged by failures


async def test_timestamps_round_trip_as_utc_aware(store):
    await store.record_success("c", T0, Event(at=T0, kind="ping"), 100)
    s = await store.get_state("c")
    assert s.last_seen is not None
    assert s.last_seen.tzinfo is not None
    assert s.last_seen == T0


async def test_events_are_newest_first(store):
    for i in range(3):
        t = T0 + timedelta(minutes=i)
        await store.record_success("c", t, Event(at=t, kind="ping", detail=f"e{i}"), 100)
    events = await store.get_events("c", 10)
    assert [e.detail for e in events] == ["e2", "e1", "e0"]


async def test_event_ring_buffer_is_trimmed_to_history(store):
    for i in range(10):
        t = T0 + timedelta(minutes=i)
        await store.record_success("c", t, Event(at=t, kind="ping", detail=f"e{i}"), history=3)
    events = await store.get_events("c", 100)
    assert [e.detail for e in events] == ["e9", "e8", "e7"]


async def test_events_are_isolated_per_check(store):
    await store.record_success("a", T0, Event(at=T0, kind="ping", detail="for-a"), 100)
    await store.record_success("b", T0, Event(at=T0, kind="ping", detail="for-b"), 100)
    assert [e.detail for e in await store.get_events("a", 10)] == ["for-a"]
    assert [e.detail for e in await store.get_events("b", 10)] == ["for-b"]


async def test_get_states_batches(store):
    await store.record_success("a", T0, Event(at=T0, kind="ping"), 100)
    states = await store.get_states(["a", "b"])
    assert states["a"].last_result_ok is True
    assert states["b"].last_result_ok is None  # unknown -> default, never raises


async def test_healthy_is_true_when_connected(store):
    assert await store.healthy() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.store'`

- [ ] **Step 3: Write the protocol**

Create `src/tskmon/store/__init__.py`:

```python
from tskmon.store.base import Store, StoreUnavailable

__all__ = ["Store", "StoreUnavailable"]
```

Create `src/tskmon/store/base.py`:

```python
"""The storage seam. Data volume is trivial; this interface exists for
deployment topology (single-node SQLite vs multi-replica Redis vs Lambda later).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from tskmon.models import CheckState, Event


class StoreUnavailable(Exception):
    """The store could not be reached. Callers MUST fail loud, never fail open."""


class Store(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def healthy(self) -> bool: ...

    async def get_state(self, name: str) -> CheckState: ...

    async def get_states(self, names: Sequence[str]) -> dict[str, CheckState]: ...

    async def record_success(
        self, name: str, at: datetime, event: Event, history: int
    ) -> None: ...

    async def record_failure(
        self, name: str, at: datetime, event: Event, history: int
    ) -> None: ...

    async def get_events(self, name: str, limit: int) -> list[Event]: ...
```

- [ ] **Step 4: Write the SQLite implementation**

Create `src/tskmon/store/sqlite.py`:

```python
"""Default store: one file, stdlib, zero dependencies.

CAVEATS (documented, not bugs):
  - Restarts lose state unless the DB is on a PersistentVolume.
  - Multiple replicas SILENTLY BREAK CORRECTNESS: a cron's ping lands on replica
    A, the vendor's poll lands on replica B, which never heard of it. Use Redis.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone

from tskmon.models import CheckState, Event
from tskmon.store.base import StoreUnavailable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_state (
    name                 TEXT PRIMARY KEY,
    last_seen            TEXT,
    last_result_ok       INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    at     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_by_check ON events (name, id DESC);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(timezone.utc) if s else None


class SqliteStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = sqlite3.connect(self._dsn, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreUnavailable("sqlite store is not connected")
        return self._conn

    async def healthy(self) -> bool:
        try:
            self._db().execute("SELECT 1").fetchone()
            return True
        except (sqlite3.Error, StoreUnavailable):
            return False

    async def get_state(self, name: str) -> CheckState:
        return (await self.get_states([name]))[name]

    async def get_states(self, names: Sequence[str]) -> dict[str, CheckState]:
        try:
            async with self._lock:
                rows = self._db().execute("SELECT * FROM check_state").fetchall()
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

        found = {
            r["name"]: CheckState(
                last_seen=_parse(r["last_seen"]),
                last_result_ok=None if r["last_result_ok"] is None else bool(r["last_result_ok"]),
                consecutive_failures=r["consecutive_failures"],
            )
            for r in rows
        }
        return {n: found.get(n, CheckState()) for n in names}

    async def _record(
        self, name: str, at: datetime, event: Event, history: int, *, ok: bool
    ) -> None:
        try:
            async with self._lock:
                db = self._db()
                if ok:
                    db.execute(
                        """
                        INSERT INTO check_state (name, last_seen, last_result_ok, consecutive_failures)
                        VALUES (?, ?, 1, 0)
                        ON CONFLICT(name) DO UPDATE SET
                            last_seen = excluded.last_seen,
                            last_result_ok = 1,
                            consecutive_failures = 0
                        """,
                        (name, _iso(at)),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO check_state (name, last_seen, last_result_ok, consecutive_failures)
                        VALUES (?, NULL, 0, 1)
                        ON CONFLICT(name) DO UPDATE SET
                            last_result_ok = 0,
                            consecutive_failures = check_state.consecutive_failures + 1
                        """,
                        (name,),
                    )
                db.execute(
                    "INSERT INTO events (name, at, kind, detail) VALUES (?, ?, ?, ?)",
                    (name, _iso(event.at), event.kind, event.detail),
                )
                db.execute(
                    """
                    DELETE FROM events
                     WHERE name = ?
                       AND id NOT IN (
                           SELECT id FROM events WHERE name = ? ORDER BY id DESC LIMIT ?
                       )
                    """,
                    (name, name, history),
                )
                db.commit()
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

    async def record_success(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=True)

    async def record_failure(self, name: str, at: datetime, event: Event, history: int) -> None:
        await self._record(name, at, event, history, ok=False)

    async def get_events(self, name: str, limit: int) -> list[Event]:
        try:
            async with self._lock:
                rows = self._db().execute(
                    "SELECT at, kind, detail FROM events WHERE name = ? ORDER BY id DESC LIMIT ?",
                    (name, limit),
                ).fetchall()
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e
        return [
            Event(at=_parse(r["at"]), kind=r["kind"], detail=r["detail"])  # type: ignore[arg-type]
            for r in rows
        ]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/store -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add src/tskmon/store tests/store
git commit -m "feat: Store protocol, SQLite implementation, conformance suite"
```

---

### Task 6: Redis store (against the same conformance suite)

**Files:**
- Create: `src/tskmon/store/redis.py`
- Modify: `tests/store/test_conformance.py` (extend the fixture params — the test bodies do not change)

**Interfaces:**
- Consumes: the `Store` protocol from Task 5.
- Produces: `RedisStore(dsn: str, client: redis.asyncio.Redis | None = None)`. `client` is injected by tests (fakeredis); production passes only `dsn`.

Data model: one hash per check at `tskmon:state:<name>`, one capped list at `tskmon:events:<name>` maintained with `LPUSH` + `LTRIM` — the ring buffer is a native Redis operation.

- [ ] **Step 1: Extend the conformance fixture (the failing test)**

In `tests/store/test_conformance.py`, replace the imports and the `store` fixture with:

```python
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

from tskmon.models import Event
from tskmon.store.redis import RedisStore
from tskmon.store.sqlite import SqliteStore

T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", "redis"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStore(str(tmp_path / "state.db"))
    else:
        s = RedisStore("redis://localhost:6379/0", client=fakeredis.aioredis.FakeRedis())
    await s.connect()
    yield s
    await s.close()
```

Leave every test body exactly as it is. That is the point of a conformance suite.

- [ ] **Step 2: Run tests to verify the Redis half fails**

Run: `.venv/bin/pytest tests/store -v`
Expected: the 9 `[sqlite]` tests pass; the 9 `[redis]` tests ERROR with `ModuleNotFoundError: No module named 'tskmon.store.redis'`

- [ ] **Step 3: Write the implementation**

Create `src/tskmon/store/redis.py`:

```python
"""Multi-replica store. All replicas share state, so a cron's ping and the
vendor's poll agree regardless of which pod they land on. No PVC needed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from tskmon.models import CheckState, Event
from tskmon.store.base import StoreUnavailable


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(timezone.utc) if s else None


class RedisStore:
    def __init__(self, dsn: str, client: aioredis.Redis | None = None) -> None:
        self._dsn = dsn
        self._client = client

    def _key_state(self, name: str) -> str:
        return f"tskmon:state:{name}"

    def _key_events(self, name: str) -> str:
        return f"tskmon:events:{name}"

    def _db(self) -> aioredis.Redis:
        if self._client is None:
            raise StoreUnavailable("redis store is not connected")
        return self._client

    async def connect(self) -> None:
        if self._client is None:
            self._client = aioredis.from_url(self._dsn, decode_responses=True)
        try:
            await self._client.ping()
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthy(self) -> bool:
        try:
            await self._db().ping()
            return True
        except (RedisError, StoreUnavailable):
            return False

    async def get_state(self, name: str) -> CheckState:
        return (await self.get_states([name]))[name]

    async def get_states(self, names: Sequence[str]) -> dict[str, CheckState]:
        try:
            pipe = self._db().pipeline()
            for n in names:
                pipe.hgetall(self._key_state(n))
            raws = await pipe.execute()
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

        out: dict[str, CheckState] = {}
        for name, raw in zip(names, raws, strict=True):
            raw = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in (raw or {}).items()
            }
            if not raw:
                out[name] = CheckState()
                continue
            ok_raw = raw.get("last_result_ok")
            out[name] = CheckState(
                last_seen=_parse(raw.get("last_seen") or None),
                last_result_ok=None if ok_raw in (None, "") else ok_raw == "1",
                consecutive_failures=int(raw.get("consecutive_failures", 0)),
            )
        return out

    async def _push_event(self, pipe: aioredis.client.Pipeline, name: str, event: Event, history: int) -> None:
        pipe.lpush(
            self._key_events(name),
            json.dumps({"at": _iso(event.at), "kind": event.kind, "detail": event.detail}),
        )
        pipe.ltrim(self._key_events(name), 0, history - 1)

    async def record_success(self, name: str, at: datetime, event: Event, history: int) -> None:
        try:
            pipe = self._db().pipeline()
            pipe.hset(
                self._key_state(name),
                mapping={"last_seen": _iso(at), "last_result_ok": "1", "consecutive_failures": 0},
            )
            await self._push_event(pipe, name, event, history)
            await pipe.execute()
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

    async def record_failure(self, name: str, at: datetime, event: Event, history: int) -> None:
        try:
            pipe = self._db().pipeline()
            # last_seen deliberately untouched: a failure is not a sighting.
            pipe.hset(self._key_state(name), "last_result_ok", "0")
            pipe.hincrby(self._key_state(name), "consecutive_failures", 1)
            await self._push_event(pipe, name, event, history)
            await pipe.execute()
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e

    async def get_events(self, name: str, limit: int) -> list[Event]:
        try:
            raws = await self._db().lrange(self._key_events(name), 0, limit - 1)
        except RedisError as e:
            raise StoreUnavailable(str(e)) from e
        out: list[Event] = []
        for raw in raws:
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            at = _parse(d["at"])
            assert at is not None
            out.append(Event(at=at, kind=d["kind"], detail=d.get("detail", "")))
        return out
```

- [ ] **Step 4: Run tests to verify both implementations pass the same suite**

Run: `.venv/bin/pytest tests/store -v`
Expected: 18 passed (9 sqlite + 9 redis)

- [ ] **Step 5: Commit**

```bash
git add src/tskmon/store/redis.py tests/store/test_conformance.py
git commit -m "feat: Redis store, passing the same conformance suite"
```

---

### Task 7: Ingest endpoints (`/ping`)

**Files:**
- Create: `src/tskmon/api.py`
- Test: `tests/test_api_ingest.py`

**Interfaces:**
- Consumes: `Config`, `Store`, `Event`.
- Produces: `build_app(config: Config, store: Store, clock: Callable[[], datetime] = ...) -> FastAPI`, and a module-level `MAX_BODY_BYTES = 4096`. Later tasks add routes to this same factory.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_ingest.py`:

```python
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tskmon.api import build_app
from tskmon.config import parse_config
from tskmon.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
ENV = {"TSKMON_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
async def ctx(tmp_path):
    cfg = parse_config(YAML, ENV)
    store = SqliteStore(str(tmp_path / "s.db"))
    await store.connect()
    app = build_app(cfg, store, clock=lambda: T0)
    with TestClient(app) as client:
        yield cfg, store, client
    await store.close()


async def test_post_ping_records_a_sighting(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    r = client.post(f"/ping/{token}")
    assert r.status_code == 200

    state = await store.get_state("nightly-db-backup")
    assert state.last_seen == T0
    assert state.last_result_ok is True


async def test_get_ping_also_works_for_legacy_wget(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token
    assert client.get(f"/ping/{token}").status_code == 200
    assert (await store.get_state("nightly-db-backup")).last_result_ok is True


async def test_unknown_token_is_404_not_401(ctx):
    # An attacker probing the token space must not learn what exists.
    _, _, client = ctx
    r = client.post("/ping/deadbeefdeadbeefdeadbeefdeadbeef")
    assert r.status_code == 404


async def test_fail_endpoint_marks_failure_without_a_sighting(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    client.post(f"/ping/{token}")            # ran fine once
    r = client.post(f"/ping/{token}/fail")   # then ran and failed
    assert r.status_code == 200

    state = await store.get_state("nightly-db-backup")
    assert state.last_result_ok is False
    assert state.last_seen == T0  # a failure is not a sighting


async def test_body_is_stored_on_the_event(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    client.post(f"/ping/{token}/fail", content=b"pg_dump: connection refused")
    events = await store.get_events("nightly-db-backup", 10)
    assert events[0].kind == "fail"
    assert events[0].detail == "pg_dump: connection refused"


async def test_oversized_body_is_truncated_not_rejected(ctx):
    cfg, store, client = ctx
    token = cfg.by_name["nightly-db-backup"].token

    client.post(f"/ping/{token}", content=b"x" * 10_000)
    events = await store.get_events("nightly-db-backup", 10)
    assert len(events[0].detail) == 4096
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.api'`

- [ ] **Step 3: Write the implementation**

Create `src/tskmon/api.py`:

```python
"""HTTP surface. The route grouping is a security boundary, not an
organizational one:

  /ping/*    ingest    — the token IS the credential
  /status/*  status    — UNAUTHENTICATED by necessity, therefore LEAKS NOTHING
  /checks,/metrics     — bearer token; this is where internal topology lives
  /healthz   liveness  — the check on the checker. NOT /status.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response

from tskmon.config import Config
from tskmon.models import Event
from tskmon.store.base import Store

MAX_BODY_BYTES = 4096

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_app(config: Config, store: Store, clock: Clock = _utcnow) -> FastAPI:
    app = FastAPI(title="tskmon", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.store = store
    app.state.clock = clock

    async def _detail(request: Request) -> str:
        body = await request.body()
        return body[:MAX_BODY_BYTES].decode("utf-8", errors="replace")

    def _lookup(token: str):
        check = config.by_token.get(token)
        if check is None:
            # 404, never 401: "wrong token" must be indistinguishable from
            # "no such check".
            raise HTTPException(status_code=404, detail="not found")
        return check

    @app.api_route("/ping/{token}", methods=["GET", "POST"])
    async def ping(token: str, request: Request) -> Response:
        check = _lookup(token)
        now = clock()
        await store.record_success(
            check.name, now, Event(at=now, kind="ping", detail=await _detail(request)), check.history
        )
        return Response(content="ok", media_type="text/plain")

    @app.api_route("/ping/{token}/fail", methods=["GET", "POST"])
    async def ping_fail(token: str, request: Request) -> Response:
        check = _lookup(token)
        now = clock()
        await store.record_failure(
            check.name, now, Event(at=now, kind="fail", detail=await _detail(request)), check.history
        )
        return Response(content="ok", media_type="text/plain")

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_ingest.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/tskmon/api.py tests/test_api_ingest.py
git commit -m "feat: ping ingest endpoints"
```

---

### Task 8: Status endpoints (`/status`) and `/healthz`

**Files:**
- Modify: `src/tskmon/api.py` (add routes inside `build_app`)
- Test: `tests/test_api_status.py`

**Interfaces:**
- Consumes: `evaluate` from `tskmon.evaluator`, `StoreUnavailable` from `tskmon.store.base`.
- Produces: `GET /status`, `GET /status/{name}`, `GET /healthz`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_status.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tskmon.api import build_app
from tskmon.config import parse_config
from tskmon.store.base import StoreUnavailable
from tskmon.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
    grace: 1h
  - name: legacy-etl
    type: heartbeat
    interval: 1h
    enabled: false
"""
ENV = {"TSKMON_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
async def ctx(tmp_path):
    cfg = parse_config(YAML, ENV)
    store = SqliteStore(str(tmp_path / "s.db"))
    await store.connect()
    clock = Clock(T0)
    app = build_app(cfg, store, clock=clock)
    with TestClient(app) as client:
        yield cfg, store, client, clock
    await store.close()


async def test_pending_check_is_healthy(ctx):
    # Deliberate: a never-pinged check must not page on every deploy.
    _, _, client, _ = ctx
    r = client.get("/status/nightly-db-backup")
    assert r.status_code == 200
    assert r.text == "up"


async def test_aggregate_is_200_when_nothing_is_down(ctx):
    _, _, client, _ = ctx
    assert client.get("/status").status_code == 200


async def test_stale_heartbeat_is_503(ctx):
    cfg, _, client, clock = ctx
    token = cfg.by_name["nightly-db-backup"].token
    client.post(f"/ping/{token}")

    clock.t = T0 + timedelta(hours=26)  # past interval + grace

    r = client.get("/status/nightly-db-backup")
    assert r.status_code == 503
    assert r.text == "down"


async def test_one_down_check_takes_the_aggregate_down(ctx):
    cfg, _, client, clock = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")
    clock.t = T0 + timedelta(hours=26)
    assert client.get("/status").status_code == 503


async def test_disabled_check_is_excluded_from_the_aggregate(ctx):
    # legacy-etl is paused and never pinged; it must not drag /status down.
    _, _, client, clock = ctx
    clock.t = T0 + timedelta(days=30)
    assert client.get("/status").status_code == 200
    assert client.get("/status/legacy-etl").status_code == 200


async def test_status_body_leaks_nothing(ctx):
    _, _, client, _ = ctx
    body = client.get("/status").text
    assert body in ("up", "down")
    assert "nightly-db-backup" not in body


async def test_unknown_check_name_is_404(ctx):
    _, _, client, _ = ctx
    assert client.get("/status/no-such-check").status_code == 404


async def test_unreadable_store_fails_LOUD_not_open(ctx):
    # The worst possible bug is reporting "all clear" while blind.
    cfg, store, client, _ = ctx

    async def boom(_names):
        raise StoreUnavailable("redis is gone")

    store.get_states = boom  # type: ignore[method-assign]

    assert client.get("/status").status_code == 503
    assert client.get("/status/nightly-db-backup").status_code == 503


async def test_healthz_is_200_when_store_is_reachable(ctx):
    _, _, client, _ = ctx
    assert client.get("/healthz").status_code == 200


async def test_healthz_is_503_when_store_is_gone(ctx):
    _, store, client, _ = ctx

    async def unhealthy():
        return False

    store.healthy = unhealthy  # type: ignore[method-assign]
    assert client.get("/healthz").status_code == 503


async def test_healthz_is_not_status(ctx):
    # A dead backup job must NOT make k8s restart the monitor reporting it.
    cfg, _, client, clock = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")
    clock.t = T0 + timedelta(hours=26)

    assert client.get("/status").status_code == 503
    assert client.get("/healthz").status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_status.py -v`
Expected: FAIL — 404s on `/status` (routes not defined)

- [ ] **Step 3: Add the routes**

In `src/tskmon/api.py`, extend the imports:

```python
from tskmon.evaluator import evaluate
from tskmon.models import Event, State
from tskmon.store.base import Store, StoreUnavailable
```

Then add these routes inside `build_app`, immediately before `return app`:

```python
    def _plain(state: State) -> Response:
        # Unauthenticated. Leaks NOTHING: no JSON, no names, no timestamps.
        healthy = state is not State.DOWN
        return Response(
            content="up" if healthy else "down",
            status_code=200 if healthy else 503,
            media_type="text/plain",
        )

    @app.get("/status/{name}")
    async def status_one(name: str) -> Response:
        check = config.by_name.get(name)
        if check is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            state = await store.get_state(name)
        except StoreUnavailable:
            # Fail LOUD. Reporting "all clear" while blind is the worst bug
            # this system can have.
            return Response(content="down", status_code=503, media_type="text/plain")
        return _plain(evaluate(check, state, clock()))

    @app.get("/status")
    async def status_all() -> Response:
        names = [c.name for c in config.checks]
        try:
            states = await store.get_states(names)
        except StoreUnavailable:
            return Response(content="down", status_code=503, media_type="text/plain")

        now = clock()
        down = any(
            evaluate(c, states[c.name], now) is State.DOWN for c in config.checks
        )
        return _plain(State.DOWN if down else State.UP)

    @app.get("/healthz")
    async def healthz() -> Response:
        # The check ON THE CHECKER. Deliberately NOT /status: a genuinely-dead
        # backup job must never cause k8s to kill the monitor reporting it.
        ok = await store.healthy()
        return Response(
            content="ok" if ok else "store unavailable",
            status_code=200 if ok else 503,
            media_type="text/plain",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_status.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/tskmon/api.py tests/test_api_status.py
git commit -m "feat: status endpoints and healthz — fail loud, leak nothing"
```

---

### Task 9: Observability endpoints (`/checks`, `/metrics`) with fail-closed auth

**Files:**
- Create: `src/tskmon/metrics.py`
- Modify: `src/tskmon/api.py`
- Test: `tests/test_metrics.py`, `tests/test_api_observability.py`

**Interfaces:**
- Consumes: `evaluate`, `Config`, `CheckState`, `State`.
- Produces: `render_metrics(config: Config, states: dict[str, CheckState], now: datetime, unknown_pings: int) -> str` (pure). Routes `GET /checks`, `GET /checks/{name}`, `GET /metrics`, all behind `Authorization: Bearer <admin_token>`.

Also wires the `tskmon_unknown_ping_total` counter, which is incremented by the ingest 404 path from Task 7.

- [ ] **Step 1: Write the failing metrics test**

Create `tests/test_metrics.py`:

```python
from datetime import datetime, timedelta, timezone

from tskmon.config import parse_config
from tskmon.metrics import render_metrics
from tskmon.models import CheckState

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
    grace: 1h
"""
ENV = {"TSKMON_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def test_up_check_renders_1():
    cfg = parse_config(YAML, ENV)
    states = {"nightly-db-backup": CheckState(last_seen=T0, last_result_ok=True)}
    out = render_metrics(cfg, states, T0, unknown_pings=0)
    assert 'tskmon_check_up{name="nightly-db-backup"} 1' in out


def test_down_check_renders_0():
    cfg = parse_config(YAML, ENV)
    states = {
        "nightly-db-backup": CheckState(
            last_seen=T0 - timedelta(hours=26), last_result_ok=True
        )
    }
    out = render_metrics(cfg, states, T0, unknown_pings=0)
    assert 'tskmon_check_up{name="nightly-db-backup"} 0' in out


def test_last_seen_is_a_unix_timestamp():
    cfg = parse_config(YAML, ENV)
    states = {"nightly-db-backup": CheckState(last_seen=T0, last_result_ok=True)}
    out = render_metrics(cfg, states, T0, unknown_pings=0)
    assert f'tskmon_check_last_seen_seconds{{name="nightly-db-backup"}} {T0.timestamp()}' in out


def test_unknown_ping_counter_is_exposed():
    cfg = parse_config(YAML, ENV)
    out = render_metrics(cfg, {"nightly-db-backup": CheckState()}, T0, unknown_pings=3)
    assert "tskmon_unknown_ping_total 3" in out


def test_help_and_type_lines_are_present():
    cfg = parse_config(YAML, ENV)
    out = render_metrics(cfg, {"nightly-db-backup": CheckState()}, T0, unknown_pings=0)
    assert "# HELP tskmon_check_up" in out
    assert "# TYPE tskmon_check_up gauge" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.metrics'`

- [ ] **Step 3: Write the metrics renderer**

Create `src/tskmon/metrics.py`:

```python
"""Prometheus text rendering. Pure, like the evaluator: no I/O, `now` injected.

This endpoint is why the same box that adapts a private network to an EXTERNAL
uptime vendor also plugs straight into an INTERNAL Prometheus.
"""

from __future__ import annotations

from datetime import datetime

from tskmon.config import Config
from tskmon.evaluator import evaluate
from tskmon.models import CheckState, State


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(
    config: Config,
    states: dict[str, CheckState],
    now: datetime,
    unknown_pings: int,
) -> str:
    lines = [
        "# HELP tskmon_check_up Whether the check is not DOWN (1) or DOWN (0).",
        "# TYPE tskmon_check_up gauge",
    ]
    for check in config.checks:
        state = evaluate(check, states.get(check.name, CheckState()), now)
        up = 0 if state is State.DOWN else 1
        lines.append(f'tskmon_check_up{{name="{_escape(check.name)}"}} {up}')

    lines += [
        "# HELP tskmon_check_last_seen_seconds Unix time of the last successful sighting.",
        "# TYPE tskmon_check_last_seen_seconds gauge",
    ]
    for check in config.checks:
        last_seen = states.get(check.name, CheckState()).last_seen
        if last_seen is not None:
            lines.append(
                f'tskmon_check_last_seen_seconds{{name="{_escape(check.name)}"}} {last_seen.timestamp()}'
            )

    lines += [
        "# HELP tskmon_unknown_ping_total Pings for checks that do not exist.",
        "# TYPE tskmon_unknown_ping_total counter",
        f"tskmon_unknown_ping_total {unknown_pings}",
        "",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_metrics.py -v`
Expected: 5 passed

- [ ] **Step 5: Write the failing API tests**

Create `tests/test_api_observability.py`:

```python
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tskmon.api import build_app
from tskmon.config import parse_config
from tskmon.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server:
  listen: ":8080"
  secret: ${TSKMON_SECRET}
  admin_token: ${TSKMON_ADMIN_TOKEN}
  timezone: Asia/Kolkata
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
NO_ADMIN_YAML = YAML.replace("  admin_token: ${TSKMON_ADMIN_TOKEN}\n", "")
ENV = {"TSKMON_SECRET": "s3cret", "TSKMON_ADMIN_TOKEN": "admin-tok"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
AUTH = {"Authorization": "Bearer admin-tok"}


@pytest.fixture
async def ctx(tmp_path):
    cfg = parse_config(YAML, ENV)
    store = SqliteStore(str(tmp_path / "s.db"))
    await store.connect()
    app = build_app(cfg, store, clock=lambda: T0)
    with TestClient(app) as client:
        yield cfg, store, client
    await store.close()


async def test_checks_requires_auth(ctx):
    _, _, client = ctx
    assert client.get("/checks").status_code == 401
    assert client.get("/checks", headers={"Authorization": "Bearer wrong"}).status_code == 401


async def test_metrics_requires_auth(ctx):
    _, _, client = ctx
    assert client.get("/metrics").status_code == 401


async def test_checks_returns_state_with_auth(ctx):
    cfg, _, client = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")

    r = client.get("/checks", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "nightly-db-backup"
    assert body[0]["state"] == "up"


async def test_check_detail_includes_the_event_ring_buffer(ctx):
    cfg, _, client = ctx
    token = cfg.by_name["nightly-db-backup"].token
    client.post(f"/ping/{token}/fail", content=b"pg_dump: connection refused")

    r = client.get("/checks/nightly-db-backup", headers=AUTH)
    assert r.status_code == 200
    events = r.json()["events"]
    assert events[0]["kind"] == "fail"
    assert events[0]["detail"] == "pg_dump: connection refused"


async def test_timestamps_render_in_the_configured_timezone(ctx):
    # Display only. The engineer debugging at 3am should not do UTC math.
    cfg, _, client = ctx
    client.post(f"/ping/{cfg.by_name['nightly-db-backup'].token}")

    r = client.get("/checks/nightly-db-backup", headers=AUTH)
    # T0 = 02:00 UTC -> 07:30 in Asia/Kolkata (UTC+5:30)
    assert r.json()["last_seen"].startswith("2026-07-15T07:30:00")


async def test_unknown_check_detail_is_404(ctx):
    _, _, client = ctx
    assert client.get("/checks/no-such-check", headers=AUTH).status_code == 404


async def test_metrics_counts_unknown_pings(ctx):
    _, _, client = ctx
    client.post("/ping/deadbeefdeadbeefdeadbeefdeadbeef")
    client.post("/ping/deadbeefdeadbeefdeadbeefdeadbeef")

    body = client.get("/metrics", headers=AUTH).text
    assert "tskmon_unknown_ping_total 2" in body


async def test_endpoints_are_DISABLED_when_no_admin_token_configured(tmp_path):
    # Fail CLOSED. An accidentally-public /checks hands out the internal topology.
    cfg = parse_config(NO_ADMIN_YAML, ENV)
    store = SqliteStore(str(tmp_path / "s2.db"))
    await store.connect()
    with TestClient(build_app(cfg, store, clock=lambda: T0)) as client:
        assert client.get("/checks").status_code == 404
        assert client.get("/metrics").status_code == 404
        assert client.get("/checks", headers=AUTH).status_code == 404
    await store.close()
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_observability.py -v`
Expected: FAIL — 404s on `/checks` and `/metrics` (routes not defined)

- [ ] **Step 7: Wire the routes and the counter**

In `src/tskmon/api.py`, extend the imports:

```python
import secrets

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from tskmon.metrics import render_metrics
```

Inside `build_app`, immediately after `app.state.clock = clock`, add the counter:

```python
    app.state.unknown_pings = 0
```

In `_lookup`, increment the counter before raising:

```python
    def _lookup(token: str):
        check = config.by_token.get(token)
        if check is None:
            # A real operational signal: a cron job believes it is monitored
            # when it is not. 404, never 401.
            app.state.unknown_pings += 1
            raise HTTPException(status_code=404, detail="not found")
        return check
```

Then add these routes immediately before `return app`:

```python
    tz = config.server.timezone

    def _render_dt(dt: datetime | None) -> str | None:
        # Display only. Evaluation is always UTC duration arithmetic.
        return dt.astimezone(tz).isoformat() if dt is not None else None

    def _require_admin(authorization: str | None) -> None:
        if config.server.admin_token is None:
            # Fail CLOSED: unconfigured means DISABLED, not open.
            raise HTTPException(status_code=404, detail="not found")
        expected = f"Bearer {config.server.admin_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/checks")
    async def list_checks(authorization: str | None = Header(default=None)) -> JSONResponse:
        _require_admin(authorization)
        names = [c.name for c in config.checks]
        states = await store.get_states(names)
        now = clock()
        return JSONResponse(
            [
                {
                    "name": c.name,
                    "type": str(c.type),
                    "state": str(evaluate(c, states[c.name], now)),
                    "last_seen": _render_dt(states[c.name].last_seen),
                    "consecutive_failures": states[c.name].consecutive_failures,
                }
                for c in config.checks
            ]
        )

    @app.get("/checks/{name}")
    async def check_detail(
        name: str, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        _require_admin(authorization)
        check = config.by_name.get(name)
        if check is None:
            raise HTTPException(status_code=404, detail="not found")
        state = await store.get_state(name)
        events = await store.get_events(name, check.history)
        return JSONResponse(
            {
                "name": check.name,
                "type": str(check.type),
                "state": str(evaluate(check, state, clock())),
                "last_seen": _render_dt(state.last_seen),
                "consecutive_failures": state.consecutive_failures,
                "events": [
                    {"at": _render_dt(e.at), "kind": e.kind, "detail": e.detail}
                    for e in events
                ],
            }
        )

    @app.get("/metrics")
    async def metrics(authorization: str | None = Header(default=None)) -> PlainTextResponse:
        _require_admin(authorization)
        states = await store.get_states([c.name for c in config.checks])
        body = render_metrics(config, states, clock(), app.state.unknown_pings)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_observability.py -v`
Expected: 8 passed

- [ ] **Step 9: Commit**

```bash
git add src/tskmon/metrics.py src/tskmon/api.py tests/test_metrics.py tests/test_api_observability.py
git commit -m "feat: /checks and /metrics behind fail-closed bearer auth"
```

---

### Task 10: The probe scheduler

**Files:**
- Create: `src/tskmon/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Config`, `Store`, `Check`, `Event`.
- Produces: `Scheduler(config, store, client: httpx.AsyncClient, clock=...)` with:
  - `async probe_once(check: Check) -> bool` — one outbound call; records success/failure; returns whether it matched `expect_status`.
  - `async run() -> None` — one loop per enabled probe check, in an `asyncio.TaskGroup`. Runs until cancelled.

The scheduler only *writes* state. It never decides up/down — that is the evaluator's job, at read time.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scheduler.py`:

```python
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from tskmon.config import parse_config
from tskmon.scheduler import Scheduler
from tskmon.store.sqlite import SqliteStore

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: internal-payments-api
    type: probe
    url: http://payments.internal:8080/healthz
    interval: 60s
    expect_status: 200
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
ENV = {"TSKMON_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def client_returning(status: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(status))
    )


def client_raising(exc: Exception) -> httpx.AsyncClient:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(str(tmp_path / "s.db"))
    await s.connect()
    yield s
    await s.close()


async def test_matching_status_records_success(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(200), clock=lambda: T0)

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is True

    state = await store.get_state("internal-payments-api")
    assert state.last_result_ok is True
    assert state.last_seen == T0
    assert state.consecutive_failures == 0


async def test_wrong_status_records_failure(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(500), clock=lambda: T0)

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is False

    state = await store.get_state("internal-payments-api")
    assert state.last_result_ok is False
    assert state.consecutive_failures == 1


async def test_connection_error_records_failure(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(
        cfg, store, client_raising(httpx.ConnectError("refused")), clock=lambda: T0
    )

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is False
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 1


async def test_timeout_records_failure(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(
        cfg, store, client_raising(httpx.ReadTimeout("slow")), clock=lambda: T0
    )

    assert await sched.probe_once(cfg.by_name["internal-payments-api"]) is False
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 1


async def test_failures_accumulate_then_reset_on_success(store):
    cfg = parse_config(YAML, ENV)
    check = cfg.by_name["internal-payments-api"]

    failing = Scheduler(cfg, store, client_returning(500), clock=lambda: T0)
    await failing.probe_once(check)
    await failing.probe_once(check)
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 2

    recovering = Scheduler(cfg, store, client_returning(200), clock=lambda: T0)
    await recovering.probe_once(check)
    assert (await store.get_state("internal-payments-api")).consecutive_failures == 0


async def test_probe_records_an_event(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(500), clock=lambda: T0)
    await sched.probe_once(cfg.by_name["internal-payments-api"])

    events = await store.get_events("internal-payments-api", 10)
    assert events[0].kind == "probe_fail"
    assert "500" in events[0].detail


async def test_run_probes_only_probe_checks_and_stops_on_cancel(store):
    cfg = parse_config(YAML, ENV)
    sched = Scheduler(cfg, store, client_returning(200), clock=lambda: T0)

    task = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)  # let the first immediate tick land
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await store.get_state("internal-payments-api")).last_result_ok is True
    # The heartbeat check is NOT probed — nothing to reach out to.
    assert (await store.get_state("nightly-db-backup")).last_result_ok is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.scheduler'`

- [ ] **Step 3: Write the implementation**

Create `src/tskmon/scheduler.py`:

```python
"""Outbound prober. Reaches endpoints on the private network because IT LIVES
THERE — this is the half of the system the external uptime vendor cannot do.

The scheduler only WRITES state. It never decides up/down; that is the
evaluator's job, computed at read time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from tskmon.config import Config
from tskmon.models import Check, CheckType, Event
from tskmon.store.base import Store, StoreUnavailable

log = logging.getLogger("tskmon.scheduler")

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(
        self,
        config: Config,
        store: Store,
        client: httpx.AsyncClient,
        clock: Clock = _utcnow,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client
        self._clock = clock

    async def probe_once(self, check: Check) -> bool:
        assert check.url is not None  # guaranteed by config validation
        now = self._clock()
        try:
            response = await self._client.get(
                check.url, timeout=check.timeout.total_seconds()
            )
            ok = response.status_code == check.expect_status
            detail = f"HTTP {response.status_code}"
        except httpx.HTTPError as e:
            ok = False
            detail = f"{type(e).__name__}: {e}"

        event = Event(
            at=now, kind="probe_ok" if ok else "probe_fail", detail=detail
        )
        if ok:
            await self._store.record_success(check.name, now, event, check.history)
        else:
            await self._store.record_failure(check.name, now, event, check.history)
        return ok

    async def _loop(self, check: Check) -> None:
        while True:
            try:
                await self.probe_once(check)
            except StoreUnavailable as e:
                # Do not kill the loop: /healthz already reports the store, and
                # k8s will restart us. Keep trying.
                log.error("probe %s: store unavailable: %s", check.name, e)
            await asyncio.sleep(check.interval.total_seconds())

    async def run(self) -> None:
        probes = [
            c
            for c in self._config.checks
            if c.type is CheckType.PROBE and c.enabled
        ]
        if not probes:
            await asyncio.Event().wait()  # nothing to do; block until cancelled
        async with asyncio.TaskGroup() as tg:
            for check in probes:
                tg.create_task(self._loop(check))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/tskmon/scheduler.py tests/test_scheduler.py
git commit -m "feat: async probe scheduler"
```

---

### Task 11: Entrypoint, container, and docs

**Files:**
- Create: `src/tskmon/main.py`, `Dockerfile`, `config.example.yaml`, `README.md`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything.
- Produces: `build_store(cfg: Config) -> Store`, `create_app(config_path: str | None = None) -> FastAPI`, and a `python -m tskmon.main` entrypoint.

The lifespan connects the store, starts the `Scheduler` as a background task, and tears both down on shutdown.

- [ ] **Step 1: Write the failing test**

Create `tests/test_main.py`:

```python
import pytest
from fastapi.testclient import TestClient

from tskmon.config import parse_config
from tskmon.main import build_store
from tskmon.store.redis import RedisStore
from tskmon.store.sqlite import SqliteStore

BASE = """
store: {{driver: {driver}, dsn: '{dsn}'}}
server: {{listen: ":8080", secret: ${{TSKMON_SECRET}}}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""
ENV = {"TSKMON_SECRET": "s3cret"}


def test_build_store_selects_sqlite():
    cfg = parse_config(BASE.format(driver="sqlite", dsn="./s.db"), ENV)
    assert isinstance(build_store(cfg), SqliteStore)


def test_build_store_selects_redis():
    cfg = parse_config(BASE.format(driver="redis", dsn="redis://localhost:6379/0"), ENV)
    assert isinstance(build_store(cfg), RedisStore)


def test_app_boots_end_to_end_and_serves_a_ping(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(BASE.format(driver="sqlite", dsn=str(tmp_path / "s.db")))

    from tskmon.main import create_app

    app = create_app(str(path), env=ENV)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/status").status_code == 200  # pending == healthy

        token = app.state.config.by_name["nightly-db-backup"].token
        assert client.post(f"/ping/{token}").status_code == 200
        assert client.get("/status/nightly-db-backup").text == "up"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tskmon.main'`

- [ ] **Step 3: Write the entrypoint**

Create `src/tskmon/main.py`:

```python
"""Wiring. Config is read once at boot; invalid config exits non-zero."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from tskmon.api import build_app
from tskmon.config import Config, ConfigError, load_config
from tskmon.scheduler import Scheduler
from tskmon.store.base import Store
from tskmon.store.redis import RedisStore
from tskmon.store.sqlite import SqliteStore

log = logging.getLogger("tskmon")

DEFAULT_CONFIG_PATH = "/etc/tskmon/config.yaml"


def build_store(cfg: Config) -> Store:
    if cfg.store.driver == "redis":
        return RedisStore(cfg.store.dsn)
    return SqliteStore(cfg.store.dsn)


def create_app(config_path: str | None = None, env: Mapping[str, str] | None = None) -> FastAPI:
    path = config_path or os.environ.get("TSKMON_CONFIG", DEFAULT_CONFIG_PATH)
    config = load_config(path, env)
    store = build_store(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.connect()
        client = httpx.AsyncClient()
        scheduler = Scheduler(config, store, client)
        task = asyncio.create_task(scheduler.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await client.aclose()
            await store.close()

    app = build_app(config, store)
    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("TSKMON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        app = create_app()
    except ConfigError as e:
        # Refuse to start. A half-configured monitor silently fails to watch
        # the thing you thought it was watching.
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from e

    listen: str = app.state.config.server.listen
    host, _, port = listen.rpartition(":")
    uvicorn.run(app, host=host or "0.0.0.0", port=int(port), access_log=False)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_main.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (roughly 80), zero failures.

- [ ] **Step 6: Write the container and the example config**

Create `Dockerfile`:

```dockerfile
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --target=/deps .

FROM python:3.12-slim
COPY --from=build /deps /deps
ENV PYTHONPATH=/deps
ENV TSKMON_CONFIG=/etc/tskmon/config.yaml
RUN useradd -r -u 10001 tskmon && mkdir -p /var/lib/tskmon && chown tskmon /var/lib/tskmon
USER tskmon
EXPOSE 8080
CMD ["python", "-m", "tskmon.main"]
```

Create `config.example.yaml`:

```yaml
store:
  driver: sqlite              # sqlite | redis
  dsn: /var/lib/tskmon/state.db
  # For multi-replica Kubernetes, use Redis — SQLite silently breaks
  # correctness across replicas:
  # driver: redis
  # dsn: redis://redis:6379/0

server:
  listen: ":8080"
  secret: ${TSKMON_SECRET}              # HMAC root for derived ping tokens
  admin_token: ${TSKMON_ADMIN_TOKEN}    # omit to DISABLE /checks and /metrics
  timezone: UTC                         # display only; never affects evaluation

defaults:
  grace: 5m
  timeout: 10s
  history: 100
  failure_threshold: 2

checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
    grace: 1h

  - name: internal-payments-api
    type: probe
    url: http://payments.internal:8080/healthz
    interval: 60s
    expect_status: 200
```

- [ ] **Step 7: Build the image and smoke-test it**

```bash
docker build -t tskmon:dev .
docker run --rm -e TSKMON_SECRET=s3cret \
  -v "$PWD/config.example.yaml:/etc/tskmon/config.yaml:ro" \
  -p 8080:8080 tskmon:dev &
sleep 3
curl -fsS localhost:8080/healthz && echo
curl -fsS localhost:8080/status && echo
```

Expected: `ok` then `up` (both checks are `pending`, which is healthy by design).

- [ ] **Step 8: Write the README**

Create `README.md`:

````markdown
# tskmon

Uptime vendors can only ping public endpoints. That leaves two blind spots:

- **Cron jobs.** A nightly backup that never runs has no endpoint to poll. Nothing is
  "down" — the job simply didn't happen, on a host that is otherwise healthy.
- **Private instances.** A service on `10.0.x.x` cannot be reached from the internet.

`tskmon` runs *inside* your network, accepts heartbeats from cron jobs, probes private
endpoints, and re-exposes both as plain `200`/`503` URLs your existing uptime vendor
already knows how to poll. It does the seeing; your vendor keeps doing the paging.

## Quick start

```sh
docker run -e TSKMON_SECRET=$(openssl rand -hex 32) \
  -v ./config.yaml:/etc/tskmon/config.yaml:ro -p 8080:8080 ghcr.io/you/tskmon
```

Add a heartbeat to a cron job — the URL is the credential, so there is nothing else to
plumb in:

```sh
0 2 * * * /opt/backup.sh && curl -fsS https://mon.example.com/ping/<token> \
                        || curl -fsS https://mon.example.com/ping/<token>/fail
```

Then point one upstream monitor per check at `https://mon.example.com/status/<name>`, so
the page you get at 3am says *which* check tripped.

Get a check's token from `/checks` (requires `admin_token`), or derive it yourself:
`HMAC-SHA256(secret, check_name)`, first 32 hex chars.

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST\|GET /ping/<token>` | token is the credential | cron checks in |
| `POST\|GET /ping/<token>/fail` | token | cron ran and failed; say so now |
| `GET /status/<name>` | none | `200`/`503` for your uptime vendor |
| `GET /status` | none | `200` only if nothing is down |
| `GET /checks` | bearer | real JSON: state, last seen, event history |
| `GET /metrics` | bearer | Prometheus |
| `GET /healthz` | none | liveness — the check on the checker |

`/status` is unauthenticated because your vendor must reach it, and therefore leaks
nothing: the body is literally `up` or `down`. Internal topology lives behind the bearer
token. If `admin_token` is unset, `/checks` and `/metrics` are **disabled**, not open.

## Operational notes

- **`pending` counts as healthy.** A newly deployed check reports `200` until its first
  ping. Deliberate: the alternative pages you for every heartbeat on every deploy, and
  you would learn to ignore the alerts within a week.
- **SQLite + multiple replicas is silently wrong.** The cron's ping and the vendor's poll
  can land on different pods that disagree. Use `driver: redis` for multi-replica, or a
  PersistentVolume with a single replica.
- **`/healthz` is not `/status`.** Never point a Kubernetes liveness probe at `/status`, or
  a genuinely dead backup job will cause k8s to kill the monitor reporting it.
- **Clock skew breaks heartbeats.** Every decision is a subtraction against the local
  clock. Depend on the host's NTP, and suspect the clock first if this misbehaves.
````

- [ ] **Step 9: Commit**

```bash
git add src/tskmon/main.py tests/test_main.py Dockerfile config.example.yaml README.md
git commit -m "feat: entrypoint, container, and docs"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Heartbeat checks (push) | 2, 7 |
| Probe checks (pull) | 2, 10 |
| Declarative YAML, no registration API | 4 |
| Env-expanded secret | 4 |
| Global defaults + per-check overrides (`grace`/`timeout`/`history`/`failure_threshold`/`token`) | 4 |
| `timezone`, display-only, IANA-validated | 4, 9 |
| Fail-fast validation, all errors together | 4, 11 |
| Four states incl. `pending`-is-healthy and `paused` | 2, 8 |
| `failure_threshold` for probes; immediate recovery | 2, 10 |
| HMAC token, per-check override | 3, 4 |
| `/ping`, `/ping/../fail`, GET+POST, body capped | 7 |
| Unknown token → 404, counted | 7, 9 |
| `/status/<name>` + aggregate AND-fold; leaks nothing | 8 |
| Store unreachable → 503, never fail open | 8 |
| `/checks`, `/metrics`, bearer, fail-closed | 9 |
| `/healthz` distinct from `/status` | 8 |
| SQLite + Redis behind one interface, one conformance suite | 5, 6 |
| Bounded event ring buffer | 5, 6 |
| Clock skew documented | 11 (README) |

No gaps.

**Placeholder scan:** No TBDs. Every code step contains complete, runnable code.

**Type consistency:** `evaluate(check, state, now)`, `Store.record_success/record_failure/get_state/get_states/get_events/healthy/connect/close`, `build_app(config, store, clock)`, `Scheduler(config, store, client, clock)`, `render_metrics(config, states, now, unknown_pings)`, `build_store(cfg)`, `create_app(config_path, env)` — names and signatures are used identically in every task that references them.
