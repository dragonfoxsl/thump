# task-monitor — Design

**Date:** 2026-07-15
**Status:** Approved for planning

## Problem

Uptime vendors (Pingdom, UptimeRobot, Better Stack, et al.) can only ping publicly
reachable endpoints. This leaves two blind spots for anyone running real
infrastructure:

1. **Cron jobs.** A nightly backup that never runs has no endpoint to poll. Nothing
   is "down" — the job simply didn't happen, on a host that is otherwise perfectly
   healthy. No prober can detect this.
2. **Private instances.** A service on `10.0.x.x` inside a VPC cannot be reached from
   the public internet, so the uptime vendor cannot see it at all.

## Solution

A small middleware service, deployed as a container **inside** the network being
monitored (VM, Kubernetes, later Lambda). It:

- **Accepts heartbeats** from cron jobs (`curl` at the end of the job) and marks a
  check dead if a heartbeat does not arrive within its expected window — a dead man's
  switch.
- **Actively probes** private endpoints, which it can reach because it lives inside
  the VPC.
- **Re-exposes both** as plain `200`/`503` HTTP endpoints that an existing upstream
  uptime vendor can poll.

The upstream vendor keeps doing what it is good at — alerting, paging, on-call
rotation, status pages. This service is the **adapter** that makes the invisible
visible to it. It deliberately does not compete with the vendor.

## Scope

### In scope (MVP)

- Heartbeat checks (push) and probe checks (pull)
- Declarative YAML configuration; no runtime registration API
- Per-check and aggregate status endpoints
- SQLite and Redis store implementations behind one interface
- Bounded per-check event history
- Prometheus metrics
- HMAC-derived ping tokens, with per-check and global overrides

### Explicitly deferred ("later" line)

These were considered and consciously postponed. They are not omissions.

- **Lambda deployment** — the `Store` interface exists so this is a port, not a
  rewrite. Would need DynamoDB + EventBridge.
- **Check groups** (`/status/group/backups`) — build when the monitor-count pain is
  real. YAGNI.
- **Postgres store** — no advantage over Redis unless one is already operated.
- **Cron-expression schedules** (`0 2 * * *` instead of `interval: 24h`) — a strictly
  better model for cron monitoring; `server.timezone` exists now as the hook it will
  hang on.
- **Full uptime history / SLA math** — this is the vendor's job.
- **Notifications** — this is the vendor's job.

## Stack

Python. FastAPI for the HTTP surface, `httpx` for outbound probes, `asyncio.TaskGroup`
for the scheduler, stdlib `sqlite3` for the default store, `redis-py` for the second.

Trade-off accepted: a Python container is ~50–120MB against ~10MB for a Go static
binary. The architecture is language-independent; only the idiom changes.

## Architecture

One process, one HTTP server, four components over one storage interface.

```
                    ┌─────────────────────────────────────┐
   cron job ──────► │  Ingest    POST /ping/<token>       │
   (curl)           │            marks check "seen now"   │
                    ├─────────────────────────────────────┤
                    │  Scheduler  ticks each probe's      │
                    │             interval, makes the     │──► http://payments.internal:8080
                    │             outbound HTTP call      │    (private network — reachable
                    ├─────────────────────────────────────┤     because *we* live in the VPC)
                    │  Evaluator  PURE: (cfg, state, now) │
                    │             → up | down             │
                    ├─────────────────────────────────────┤
   upstream    ◄─── │  Status    GET /status[/<name>]     │
   uptime vendor    └──────────────┬──────────────────────┘
                                   │
                            ┌──────▼──────┐
                            │   Store     │  SQLite (default) | Redis
                            └─────────────┘
```

### The load-bearing constraint: Evaluator is pure

`Evaluator` takes a check's config, its stored state, and the current time, and returns
a state. **It performs no I/O.** Ingest and Scheduler only *write* state. Status only
*reads* state and calls Evaluator. Nothing else in the codebase is permitted to decide
whether a check is healthy.

Two consequences, both deliberate:

1. **The entire correctness surface is testable** with no network, no database, and no
   real clock. `now` is a parameter.
2. **`down` is computed at read time, not write time.** Nothing has to fire a timer to
   *notice* that a heartbeat went stale — staleness is a mathematical consequence of
   the stored timestamp and the current clock, recomputed on every poll. A stale check
   therefore cannot be missed because a background sweep died or a queue backed up.
   This is also what makes the Lambda port viable at all, since Lambda has no
   always-on loop.

## Configuration

One YAML file, read at boot. The single source of truth. Committed to a repo, mounted
as a ConfigMap or baked into the image.

```yaml
store:
  driver: sqlite              # sqlite | redis
  dsn: /var/lib/tskmon/state.db

server:
  listen: ":8080"
  secret: ${TSKMON_SECRET}    # env-expanded; HMAC root for derived ping tokens
  admin_token: ${TSKMON_ADMIN_TOKEN}   # bearer token for /checks and /metrics
  timezone: Asia/Kolkata      # default UTC. DISPLAY ONLY — never affects evaluation.

defaults:
  grace: 5m
  timeout: 10s
  history: 100
  failure_threshold: 2

checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
    grace: 1h                 # override

  - name: internal-payments-api
    type: probe
    url: http://payments.internal:8080/healthz
    interval: 60s
    expect_status: 200
    history: 20               # override

  - name: legacy-etl
    type: heartbeat
    interval: 1h
    token: 7c9f2a...          # explicit; overrides the HMAC-derived token
    enabled: true
```

**Design rule: global default + per-check override.** Applies uniformly to `grace`,
`timeout`, `history`, `failure_threshold`, and `token`. The common case needs no
per-check config.

**The secret lives in the environment, not the file.** `${TSKMON_SECRET}` is expanded
at load. This is what allows the config to be committed to git and mounted as a
ConfigMap while the HMAC root lives in a k8s Secret. Without it, "commit your config"
would be a security footgun.

**Timezone is presentation-only.** All computation is duration arithmetic on absolute
UTC instants, which is timezone-invariant. `timezone` affects only how timestamps are
*rendered* in `/checks` and in logs, so an engineer debugging at 3am does not do UTC
math in their head. If it ever affected evaluation, that would be a bug.

**Invalid config is fatal at boot.** Duplicate names, a probe with no `url`, a
heartbeat *with* a `url`, an unparseable interval, an unknown IANA timezone — all
reported together, then the process exits. A monitor that starts half-configured and
silently fails to watch something is worse than one that refuses to start: the first
failure mode is invisible, the second is a CrashLoopBackOff noticed in thirty seconds.

## State machine

Four states. `Evaluator` decides which.

| State | Meaning | Counts as healthy? |
|---|---|---|
| `up` | Fine | yes |
| `down` | Page someone | **no** |
| `pending` | Configured, nothing has happened yet | yes (see below) |
| `paused` | `enabled: false` — silenced deliberately | yes, and excluded from aggregate |

### Heartbeat transitions

A ping writes `last_seen = now` → `up`. On evaluation, if
`now - last_seen > interval + grace` → `down`.

That is the whole logic. There is no retry or threshold concept: a cron job either
checked in or it did not. Note the asymmetry with probes — **a heartbeat goes down by
the passage of time, with nothing happening.** That is the dead-man's-switch property,
and it is precisely what catches the failure a prober cannot: a job that never ran, on
a host that is fine.

### Probe transitions

The scheduler fires every `interval`. A response matching `expect_status` resets
`consecutive_failures` to 0 → `up`. Any other status, a timeout, a connection refusal,
or a DNS failure increments it; on reaching `failure_threshold` → `down`.

Recovery is immediate on first success — no flap-damping on the way back up. A monitor
slow to report that an outage *ended* is an annoyance; one slow to report that it
*started* is a liability.

### `pending` counts as healthy — a deliberate trade-off

A newly deployed heartbeat check has never been pinged, and cannot have been: the cron
has not fired yet. Treating that as `down` would page the operator for every heartbeat
check on every deployment until each interval elapsed, and they would learn to ignore
the alerts within a week.

**The cost:** a brand-new check is not protecting anything until its first successful
ping.
**The alternative cost:** guaranteed alert fatigue on every restart.

The second is worse — a monitoring system whose pages have been learned-to-dismiss is
worth less than none. **Mitigation:** `pending` is *visible* and distinct in `/checks`
and in logs, so "why is this still pending?" is an answerable question rather than a
silent hole.

## HTTP surface

The grouping is a security boundary, not an organizational one.

### Ingest — for cron jobs (auth: the token *is* the credential)

- `POST|GET /ping/<token>` → `200`, tiny body. `GET` supported because plenty of
  legacy cron jobs and appliances can only manage a `wget`.
- `POST /ping/<token>/fail` → for a job that *ran and failed* and wants to say so
  immediately rather than waiting for its window to lapse: `backup.sh || curl .../fail`
- Optional request body, capped at a few KB, stored on the event. Turns "the backup is
  red" into "the backup is red, and here is its stderr."
- Unknown token → **`404`, not `401`**, so an attacker probing the token space cannot
  distinguish "wrong token" from "no such check" and learn what exists.

Token is HMAC(`server.secret`, check name), unless the check declares an explicit
`token`. This gives a stable, unguessable URL per check with no token table to store,
and only one thing to rotate. Cron-side ergonomics stay at "paste this curl":

```sh
0 2 * * * /opt/backup.sh && curl -fsS https://mon.example.com/ping/8f3a2c...
```

### Status — for the upstream vendor (auth: **none, by necessity**)

- `GET /status/<name>` → `200`/`503`, body is literally `up` or `down`.
- `GET /status` → the AND-fold: `200` only if every enabled check is `up` or
  `pending`; `503` otherwise.

These must be unauthenticated because the vendor has to reach them — which is exactly
why **they leak nothing**. No JSON, no check list, no internal hostnames, no
timestamps. A `503` tells an anonymous caller that *something* is unhealthy, and
nothing more.

### Observability — for the operator (auth: **bearer token required**)

- `GET /checks`, `GET /checks/<name>` → real JSON: state, `last_seen`,
  `consecutive_failures`, event ring buffer.
- `GET /metrics` → Prometheus: `tskmon_check_up{name}`,
  `tskmon_check_last_seen_seconds{name}`, `tskmon_unknown_ping_total`.

Auth is `Authorization: Bearer <server.admin_token>`. If `admin_token` is unset, these
endpoints are **disabled** rather than left open — failing closed, because an
accidentally-public `/checks` hands an anonymous caller the internal topology this
design works to protect.

This is where the internal topology lives, hence the auth boundary. The metrics
endpoint also means the same box that adapts a private network to an *external* vendor
plugs straight into an *internal* Prometheus — many teams will use it that way instead.

### Liveness — for the platform

- `GET /healthz` → `200` if the process is up **and the store is reachable**.

This is the check *on the checker*. It is emphatically **not** `/status`. Conflating
"this monitor works" with "the things it monitors work" would mean a `503` from a
genuinely-dead backup job causes Kubernetes to kill the monitor reporting it — a
spectacular way to lose exactly the signal you need.

## Storage

Data volume is irrelevant (a few hundred KB); every candidate is fast enough by orders
of magnitude. The choice is about **deployment topology**.

`Store` is an interface. Two implementations ship in the MVP — not scope creep: two
real implementations are the only way to know the interface is honest rather than
quietly SQLite-shaped. Both run against **one shared test suite**.

### SQLite (default)

Right for a VM, Docker Compose, or a single-replica k8s Deployment with a PVC. Stdlib,
one file, zero dependencies.

Two properties that bite specifically in Kubernetes and must be documented:

- **Restarts lose state** unless backed by a PVC. `last_seen` for every heartbeat is
  gone; the check reverts to `pending`.
- **Multiple replicas silently break correctness.** A cron's `curl` lands on replica A;
  the vendor's poll lands on replica B, which has never heard of that heartbeat and
  reports it down. The result is a monitor that pages at random. SQLite is
  single-writer by design and has no answer to this.

### Redis / Valkey

The multi-replica Kubernetes answer. Shared state, all replicas agree, restarts are
free, no PVC. The data model maps exactly: a hash per check, and the ring buffer is a
native `LPUSH` + `LTRIM`. Also the clean path to Lambda later (Upstash / ElastiCache).

### Rejected

- **Plain JSON file** — atomic-rename dances plus concurrent async writers is a bug
  farm, for no benefit over SQLite.
- **LMDB / `diskcache`** — `diskcache` is built on SQLite, so it adds a dependency to
  get less.

## Failure modes

**The store is unreachable.** `/healthz` fails, so k8s restarts the pod — correct. But
`/status` must **not** return `200` merely because it could not read any failures. A
monitor that reports "all clear" while blind is the single worst bug this system can
have. An unreadable store therefore returns **`503`**, which pages, and the page is
truthful: the monitoring is broken and nothing it was watching can be trusted.

**A ping arrives for an unknown check.** `404`, and `tskmon_unknown_ping_total`
increments. This is a real operational signal: it usually means a cron job references a
check that was renamed or never added, and that job currently believes it is monitored
when it is not. Dropping it silently would hide precisely the misconfiguration worth
catching.

**Clock skew.** Every heartbeat decision is a subtraction against the local clock, so a
badly skewed container clock can mark a healthy check dead or a dead one healthy. There
is no clever fix — depend on the host's NTP, document it, and suspect the clock first
when this system misbehaves inexplicably.

## Testing

The strategy falls out of the architecture rather than being bolted on.

1. **Evaluator unit tests — the bulk of the suite.** Because it is a pure function of
   `(config, state, now)`, the entire correctness surface — the part deciding whether a
   dead backup job actually pages someone — runs with no network, no database, and no
   real clock. "It has been 25 hours and the backup never checked in" is a test that
   runs in microseconds and cannot be flaky. This is the payoff for the purity
   constraint.
2. **One shared `Store` conformance suite, run against both SQLite and Redis.** The only
   thing keeping the abstraction honest.
3. **Integration tests** over the real HTTP surface against a real store: ping a check,
   poll its status, advance the clock, watch it turn `503`. Probe tests point at a local
   test server that can be made to fail on command.
