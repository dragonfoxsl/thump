# thump

Uptime vendors can only ping public endpoints. That leaves two blind spots:

- **Cron jobs.** A nightly backup that never runs has no endpoint to poll. Nothing is
  "down" — the job simply didn't happen, on a host that is otherwise healthy.
- **Private instances.** A service on `10.0.x.x` cannot be reached from the internet.

`thump` runs *inside* your network, accepts heartbeats from cron jobs, probes private
endpoints, and re-exposes both as plain `200`/`503` URLs your existing uptime vendor
already knows how to poll. It does the seeing; your vendor keeps doing the paging.

## Quick start

```sh
docker run -e THUMP_SECRET=$(openssl rand -hex 32) \
  -v ./config.yaml:/etc/thump/config.yaml:ro -p 8080:8080 ghcr.io/dragonfoxsl/thump
```

Add a heartbeat to a cron job — the URL is the credential, so there is nothing else to
plumb in:

```sh
0 2 * * * /opt/backup.sh && curl -fsS https://mon.example.com/ping/<token> \
                        || curl -fsS https://mon.example.com/ping/<token>/fail
```

Then point one upstream monitor per check at `https://mon.example.com/status/<name>`, so
the page you get at 3am says *which* check tripped.

Get a check's token from `/checks` (requires `admin_token`; set `THUMP_ADMIN_TOKEN` and
uncomment `admin_token` in `config.yaml` to enable it — it's disabled by default), or
derive it yourself: `HMAC-SHA256(secret, check_name)`, first 32 hex chars.

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
- **A ping up to 60s early still counts.** If the cron host's clock runs slightly
  ahead, a `0 2 * * *` job can check in at 01:59:30 — before its own occurrence. That
  ping covers it. The tolerance is fixed and not configurable: clock skew is an
  environmental defect with a fixed remedy (NTP), not a per-check policy.
- **DST is handled by the schedule, not by you.** Occurrences are computed in
  `server.timezone`, so a 25-hour day has 25 hourly occurrences and a spring-forward
  day moves a missing `0 2 * * *` to 03:00 — matching cron itself. No grace padding is
  needed for either transition.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra dev     # creates .venv, installs from uv.lock
uv run pytest           # 137 tests
```

`.python-version` pins 3.12 — the same version the container ships, so a green
suite can't hide a break on the Python your users actually run.

`uv.lock` is committed and the image builds with `uv sync --locked`, so the
container gets the exact versions the tests ran against. CI uses `--locked` too,
which fails if the lockfile has drifted from `pyproject.toml`. After changing a
dependency, commit the regenerated lockfile.

There is deliberately no `pythonpath` setting in the pytest config. Tests run
against the installed package, so a missing or stale install fails loudly rather
than being silently masked — which is how a broken editable install once went
unnoticed here for weeks.

## Building the image

```sh
docker buildx build -t thump:dev .                              # host arch
docker buildx build --platform linux/amd64,linux/arm64 -t thump:dev .
```

Multi-arch needs QEMU registered on the host, or the arm64 stage dies with
`exec format error`:

```sh
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

CI handles this with `docker/setup-qemu-action`. Images publish to GHCR on a
`v*` tag; every other run builds both architectures without pushing, so an
arch-specific break surfaces on the PR that causes it.
