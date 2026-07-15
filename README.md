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

Get a check's token from `/checks` (requires `admin_token`; set `TSKMON_ADMIN_TOKEN` and
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
