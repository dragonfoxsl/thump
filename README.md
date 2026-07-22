<p align="center">
  <img src="https://raw.githubusercontent.com/dragonfoxsl/thump/main/assets/logo.png" alt="thump" width="520"/>
</p>

<p align="center">
  <a href="https://github.com/dragonfoxsl/thump/actions/workflows/ci.yml">
    <img src="https://github.com/dragonfoxsl/thump/actions/workflows/ci.yml/badge.svg" alt="CI"/>
  </a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/uv-package%20manager-DE5FE9?logo=python&logoColor=white" alt="uv package manager"/>
  <img src="https://img.shields.io/badge/FastAPI-server-009688?logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/SQLite%20%7C%20Redis-storage-003B57?logo=sqlite&logoColor=white" alt="SQLite or Redis"/>
  <img src="https://img.shields.io/badge/pytest-145%20tests-0A9EDC?logo=pytest&logoColor=white" alt="pytest"/>
  <img src="https://img.shields.io/badge/docker-amd64%20%7C%20arm64-2496ED?logo=docker&logoColor=white" alt="Docker multi-arch"/>
</p>

<p align="center">
  <a href="https://ko-fi.com/D5X721S5GY">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Support me on Ko-fi"/>
  </a>
</p>

<br>

**thump** is a self-hosted monitor for the things your uptime vendor cannot see — cron jobs that silently never ran, and services on private networks it cannot reach. It runs *inside* your network, accepts heartbeats, probes private endpoints, and re-exposes both as plain `200`/`503` URLs your existing vendor already knows how to poll. It does the seeing; your vendor keeps doing the paging.

```bash
curl -fsS https://mon.example.com/ping/<token>   # cron checks in
curl      https://mon.example.com/status/backup  # 200 or 503, for your vendor
```

---

## The two blind spots

| Blind spot | Why a vendor misses it | What thump does |
|---|---|---|
| **Cron jobs** | A nightly backup that never runs has no endpoint to poll. Nothing is "down" — the job simply didn't happen, on a host that is otherwise healthy. | Dead man's switch. The job checks in; silence past its deadline is the alert. |
| **Private instances** | A service on `10.0.x.x` cannot be reached from the internet at all. | Probes it from inside the VPC and republishes the result on a public status URL. |

## Check types

| Type | Deadline is | Goes down when | Requires |
|---|---|---|---|
| `heartbeat` | `interval` (a duration) | nothing pinged within `interval + grace` | one of `interval` / `schedule` |
| `heartbeat` | `schedule` (a cron expression) | the most recent occurrence whose `grace` expired went uncovered | `server.timezone` |
| `probe` | — | `failure_threshold` consecutive probe failures | `url`, `interval` |

> A probe is never marked down because time passed — only because probes actually
> failed. Time-based death is the heartbeat's job.

## Requirements

Nothing but a container runtime for normal use. Python 3.12+ and [uv](https://docs.astral.sh/uv/) if you run it from source.

| Optional | Without it |
|---|---|
| Redis | SQLite is used. Fine for one replica; **silently wrong** across several — see Operational notes |
| `admin_token` | `/checks` and `/metrics` are **disabled**, not open |

## Installation

### With Docker (recommended)

```bash
docker run -e THUMP_SECRET=$(openssl rand -hex 32) \
  -v ./config.yaml:/etc/thump/config.yaml:ro \
  -p 8080:8080 ghcr.io/dragonfoxsl/thump
```

Images are built by GitHub Actions and published to GHCR for `linux/amd64` and `linux/arm64`.

| Tag | Points at |
|---|---|
| `latest` | newest commit on `main` |
| `main` | same, by branch name |
| `1.2.3`, `1.2` | a `v*` release tag |
| `sha-<commit>` | one exact commit — use this if you want reproducible deploys |

Every image is built from `uv.lock` and only published after the test suite passes on that commit.

### From source

```bash
git clone https://github.com/dragonfoxsl/thump
cd thump
uv sync
THUMP_SECRET=$(openssl rand -hex 32) THUMP_CONFIG=./config.yaml uv run python -m thump.main
```

### Development install

```bash
uv sync --extra dev
uv run pytest
```

## Usage

### Step 1: Declare your checks

```yaml
store:
  driver: sqlite                     # or: redis
  dsn: /var/lib/thump/state.db
server:
  listen: ":8080"
  secret: ${THUMP_SECRET}
  timezone: Europe/London            # cron expressions are evaluated here

checks:
  - name: nightly-backup
    type: heartbeat
    schedule: "0 2 * * *"            # 02:00 local, daily
    grace: 30m

  - name: internal-payments-api
    type: probe
    url: http://payments.internal:8080/healthz
    interval: 60s
```

Invalid config is fatal at boot. A monitor that starts half-configured and silently fails to watch something is worse than one that refuses to start: the first failure mode is invisible, the second is a `CrashLoopBackOff` noticed in thirty seconds.

### Step 2: Have cron check in

The URL is the credential, so there is nothing else to plumb in:

```bash
0 2 * * * /opt/backup.sh && curl -fsS https://mon.example.com/ping/<token> \
                        || curl -fsS https://mon.example.com/ping/<token>/fail
```

Get a token from `/checks`, or derive it yourself — `HMAC-SHA256(secret, check_name)`, first 32 hex chars.

### Step 3: Point your vendor at it

One upstream monitor per check, at `https://mon.example.com/status/<name>`, so the page you get at 3am says *which* check tripped.

## Scheduling a heartbeat

A heartbeat's deadline is either a duration or a cron expression. They are mutually exclusive — setting both, or neither, is a config error.

```yaml
  - name: weekday-report
    type: heartbeat
    schedule: "0 6 * * 1-5"
    grace: 30m
```

Prefer `schedule` for anything driven by cron. `interval: 24h` measures 24 hours from the *last ping*, which fails three ways:

| Failure | Detail |
|---|---|
| The deadline drifts | Anchored to when the job was last *seen*, not when it was *due*, so ordinary jitter walks the window forward until a healthy job pages you |
| DST breaks it | A fixed 24h interval is wrong by an hour on both transitions, in opposite directions |
| Real schedules don't fit | `0 2 * * 1-5` has no single interval — Friday→Monday is 72h, every other gap is 24h |

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

`/status` is unauthenticated because your vendor must reach it, and therefore leaks nothing: the body is literally `up` or `down`. Internal topology lives behind the bearer token.

## Operational notes

- **`pending` counts as healthy.** A newly deployed check reports `200` until its first ping. Deliberate: the alternative pages you for every heartbeat on every deploy, and you would learn to ignore the alerts within a week.
- **SQLite + multiple replicas is silently wrong.** The cron's ping and the vendor's poll can land on different pods that disagree. Use `driver: redis` for multi-replica, or a PersistentVolume with a single replica.
- **Only one replica probes at a time.** With `driver: redis`, replicas share a probe lease, so a private endpoint is polled once per interval no matter how many pods you run — not once per pod. If the lease holder dies, another takes over within the lease TTL. SQLite is single-replica by contract and always probes. (Heartbeat pings are unaffected: they are ingested by whichever pod the cron reaches.)
- **`/healthz` is not `/status`.** Never point a Kubernetes liveness probe at `/status`, or a genuinely dead backup job will cause k8s to kill the monitor reporting it.
- **Clock skew breaks heartbeats.** Every decision is a subtraction against the local clock. Depend on the host's NTP, and suspect the clock first if this misbehaves.
- **A ping up to 60s early still counts.** If the cron host's clock runs slightly ahead, a `0 2 * * *` job can check in at 01:59:30 — before its own occurrence. That ping covers it. The tolerance is fixed and not configurable: clock skew is an environmental defect with a fixed remedy (NTP), not a per-check policy.
- **DST is handled by the schedule, not by you.** Occurrences are computed in `server.timezone`, so a 25-hour day has 25 hourly occurrences and a spring-forward day moves a missing `0 2 * * *` to 03:00 — matching cron itself. No grace padding is needed for either transition.

## How it works

State is written by two paths and read by a third. Nothing runs a background sweep to decide health:

| Component | Responsibility |
|---|---|
| `api.py` | Ingests pings. Writes state, never decides up/down. |
| `scheduler.py` | Probes endpoints on an interval. Also write-only. |
| `evaluator.py` | Decides `up`/`down`/`pending`/`paused` — **at read time**, as a pure function of `(check, state, now)`. No I/O, no clock reads. |
| `store/` | `SqliteStore` and `RedisStore` behind one `Store` protocol, both passing the same conformance suite. |
| `schedule.py` | The only module that imports `cronsim`, so the evaluator stays free of third-party cron logic. |

Computing state at read time is what makes the correctness surface testable in microseconds, and it means a restart can never lose a verdict — only the observations behind it.

## Development

```bash
uv sync --extra dev     # creates .venv from uv.lock
uv run pytest           # 145 tests
```

`.python-version` pins 3.12 — the same version the container ships, so a green suite can't hide a break on the Python your users actually run.

`uv.lock` is committed and the image builds with `uv sync --locked`, so the container gets the exact versions the tests ran against. CI uses `--locked` too, which fails if the lockfile has drifted from `pyproject.toml`. After changing a dependency, commit the regenerated lockfile.

> There is deliberately no `pythonpath` setting in the pytest config. Tests run against
> the installed package, so a missing or stale install fails loudly rather than being
> silently masked — which is exactly how a broken editable install once went unnoticed
> here.

### Building the image

```bash
docker buildx build -t thump:dev .                                      # host arch
docker buildx build --platform linux/amd64,linux/arm64 -t thump:dev .   # both
```

Multi-arch needs QEMU registered on the host, or the arm64 stage dies with `exec format error`:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

CI handles this with `docker/setup-qemu-action`. Images publish to GHCR on a `v*` tag; every other run builds both architectures without pushing, so an arch-specific break surfaces on the PR that causes it.

## Credits

| Project | Role |
|---|---|
| [cronsim](https://github.com/cuu508/cronsim) by [@cuu508](https://github.com/cuu508) | Cron expression parsing and DST-correct occurrence maths. Written for [Healthchecks.io](https://healthchecks.io) — the same problem domain, so its edge cases were found by exactly this use case. |
| [FastAPI](https://fastapi.tiangolo.com) | HTTP surface |
| [uv](https://docs.astral.sh/uv/) | Packaging and reproducible builds |

---

## License

MIT
