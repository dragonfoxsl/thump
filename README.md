<p align="center">
  <img src="assets/logo.png" alt="thump" width="520"/>
</p>

<p align="center">
  <a href="https://github.com/dragonfoxsl/thump/actions/workflows/ci.yml">
    <img src="https://github.com/dragonfoxsl/thump/actions/workflows/ci.yml/badge.svg" alt="CI"/>
  </a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/uv-package%20manager-DE5FE9?logo=python&logoColor=white" alt="uv package manager"/>
  <img src="https://img.shields.io/badge/FastAPI-server-009688?logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/SQLite%20%7C%20Redis-storage-003B57?logo=sqlite&logoColor=white" alt="SQLite or Redis"/>
  <img src="https://img.shields.io/badge/pytest-tested-0A9EDC?logo=pytest&logoColor=white" alt="pytest"/>
  <img src="https://img.shields.io/badge/docker-amd64%20%7C%20arm64-2496ED?logo=docker&logoColor=white" alt="Docker multi-arch"/>
</p>

<p align="center">
  <a href="https://ko-fi.com/D5X721S5GY">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Support me on Ko-fi"/>
  </a>
</p>

<p align="center">
  <a href="https://dragonfoxsl.github.io/thump/">Website</a> ·
  <a href="#installation">Installation</a>
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

Every image is built from `uv.lock` and only published after the test suite passes on that commit. Published images are **cosign-signed** and carry an SBOM and build provenance. Verify before you run:

```bash
cosign verify ghcr.io/dragonfoxsl/thump:latest \
  --certificate-identity-regexp '^https://github.com/dragonfoxsl/thump/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

### With docker-compose (thump + persistent Redis)

The bundled [`docker-compose.yml`](docker-compose.yml) runs thump against a dedicated Redis with AOF on and eviction off — the durable setup. Edit [`deploy/config.yaml`](deploy/config.yaml) to declare your checks, then:

```bash
THUMP_SECRET=$(openssl rand -hex 32) docker compose up -d
```

### On Kubernetes

[`deploy/kubernetes.yaml`](deploy/kubernetes.yaml) is a reference manifest: two thump replicas, a persistent authenticated Redis, a Redis ingress NetworkPolicy, `livenessProbe` on `/healthz`, `readinessProbe` on `/readyz`, and resource requests/limits. The NetworkPolicy requires a CNI that enforces it; Redis authentication remains defense in depth.

The thump Deployment uses `Recreate` so an upgrade never mixes binaries that write different persistent-state schemas. Expect a brief monitoring gap while the two application pods restart; Redis remains available throughout.

```bash
REDIS_AUTH=*** rand -hex 32)"
kubectl create secret generic thump-secret \
  --from-literal=THUMP_SECRET=*** rand -hex 32)" \
  --from-literal=REDIS_AUTH=*** \
  --from-literal=THUMP_REDIS_DSN="redis://:${REDIS_AUTH}@thump-redis:6379/0"
kubectl apply -f deploy/kubernetes.yaml
```

> thump speaks plain HTTP. Terminate TLS at your ingress or reverse proxy — the ping token is the credential and travels in the URL. See [SECURITY.md](SECURITY.md).

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `THUMP_SECRET` | *(required)* | HMAC root for derived ping tokens |
| `THUMP_CONFIG` | `/etc/thump/config.yaml` | Path to the config file |
| `THUMP_ADMIN_TOKEN` | *(unset)* | Bearer for `/checks` and `/metrics`; unset disables them |
| `THUMP_LOG_LEVEL` | `INFO` | Standard Python log level |
| `THUMP_LOG_FORMAT` | `text` | `json` for one-line structured logs |
| `THUMP_LEASE_TTL` | `60` | Seconds a probe leader holds the lease before renewing (Redis); lower = faster failover, more round-trips |
| `THUMP_HOLDER` | hostname | Identity recorded as the probe-lease holder; `redis-cli get thump:probe-leader` names the probing replica |

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
| `GET /healthz` | none | liveness — is this process serving? (k8s `livenessProbe`) |
| `GET /readyz` | none | readiness — is the store reachable? (k8s `readinessProbe`) |

`/status` is unauthenticated because your vendor must reach it, and therefore leaks nothing: the body is literally `up` or `down`. Internal topology lives behind the bearer token.

### Dashboards and alerts

`/metrics` exposes `thump_check_up`, `thump_check_consecutive_failures`, `thump_check_last_seen_seconds`, `thump_unknown_ping_total`, and `thump_build_info`. Ready-made [`deploy/grafana-dashboard.json`](deploy/grafana-dashboard.json) and [`deploy/prometheus-alerts.yml`](deploy/prometheus-alerts.yml) import straight in — the alert rules include one for thump itself being unscrapeable (the monitor of your monitors).

## Operational notes

- **`pending` counts as healthy.** A newly deployed check reports `200` until its first ping. Deliberate: the alternative pages you for every heartbeat on every deploy, and you would learn to ignore the alerts within a week.
- **SQLite + multiple replicas is silently wrong.** The cron's ping and the vendor's poll can land on different pods that disagree. Use `driver: redis` for multi-replica, or a PersistentVolume with a single replica.
- **Only one replica probes at a time.** With `driver: redis`, replicas share a probe lease, so a private endpoint is polled once per interval no matter how many pods you run — not once per pod. If the lease holder dies, another takes over within the lease TTL. SQLite is single-replica by contract and always probes. (Heartbeat pings are unaffected: they are ingested by whichever pod the cron reaches.)
- **Redis must persist, or you go blind silently.** All state lives in Redis. If it restarts without persistence — or evicts thump's keys under `maxmemory` pressure — every check resets to `pending`, which reports `200`, so you learn nothing is being watched only when something breaks unnoticed. Give thump a **dedicated** Redis with AOF enabled and **no eviction** (`maxmemory-policy noeviction`); never point it at a shared cache tier that evicts. The bundled `docker-compose.yml` and k8s manifests are configured this way.
- **`/healthz` is liveness, `/readyz` is readiness — and neither is `/status`.** `/healthz` answers "is this process alive?" and stays `200` even if the store is unreachable: a Redis blip is fixed by waiting, not by restarting into a CrashLoopBackOff, and a genuinely dead backup job must never restart the monitor reporting it. `/readyz` answers "can we serve correct answers?" — it goes `503` when the store is gone, so k8s pulls the pod from the Service until it recovers, no restart. Point `livenessProbe` at `/healthz` and `readinessProbe` at `/readyz`.
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
uv run pytest           # real-Redis integration tests skip unless REDIS_URL is set
```

`.python-version` pins 3.14 — the same version the container ships. CI also runs the suite on Python 3.12, the declared compatibility floor.

`uv.lock` is committed and the image builds with `uv sync --locked`, so the container gets the exact versions the tests ran against. CI uses `--locked` too, which fails if the lockfile has drifted from `pyproject.toml`. After changing a dependency, commit the regenerated lockfile.

> There is deliberately no `pythonpath` setting in the pytest config. Tests run against
> the installed package, so a missing or stale install fails loudly rather than being
> silently masked — which is exactly how a broken editable install once went unnoticed
> here.

### Conventions

Contributions follow a small set of rules — the ones already baked into the code and CI:

- **Test-driven.** No production code without a failing test first; watch it fail for the right reason.
- **Fail loud, never fail open.** Invalid config aborts boot; an unreachable store reports `down`, never "all clear"; warnings are errors.
- **Green gates before "done":** `ruff check src tests`, `mypy` (strict), and `pytest` (coverage floor 90%). All three run in CI.
- **Keep the correctness core pure** (`evaluator`, `metrics`), and keep **both** stores passing the shared conformance suite — with a real-Redis test for the probe lease.
- **Locked builds** (`uv.lock` committed, CI `--locked`), **SHA-pinned Actions**, and **signed images**.
- **Comments say _why_.** Document deliberate decisions and their failure modes so they aren't "fixed" later.

Run the full suite the way CI does:

```bash
uv run ruff check src tests && uv run mypy && \
  REDIS_URL=redis://localhost:6379/15 uv run pytest   # start a throwaway Redis first
```

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
