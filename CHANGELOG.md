# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-23

### Added

- **Single-prober lease.** With `driver: redis`, replicas share a probe lease,
  so a private endpoint is polled once per interval regardless of replica count
  instead of once per pod. SQLite grants unconditionally (single-replica).
- **Startup jitter** staggers the first probe across replicas and co-booting
  checks; probe scheduling is now fixed-rate rather than fixed-delay.
- **`/readyz`** readiness endpoint (store reachable). `/healthz` is now pure
  process liveness and stays `200` even when the store is down.
- **Metrics:** `thump_check_consecutive_failures` and
  `thump_build_info{version=...}`.
- **`THUMP_LOG_FORMAT=json`** for one-line structured logs.
- **Deployment references:** `docker-compose.yml` (thump + persistent Redis),
  `deploy/kubernetes.yaml`, and a Dockerfile `HEALTHCHECK`.
- **`SECURITY.md`** documenting the security model and reporting process.
- Quality gates: `ruff`, `mypy --strict`, and a coverage floor, all enforced
  in CI.
- **`THUMP_LEASE_TTL`** and **`THUMP_HOLDER`** env vars: tune probe-lease
  failover speed, and name the probing replica (`redis-cli get
  thump:probe-leader`).
- **Real-Redis integration tests** (run in CI against a Redis service, and
  locally via `REDIS_URL`) covering lease expiry, renewal, and takeover — the
  distributed semantics fakeredis can't prove.
- **`scripts/e2e-redis.sh`:** a container end-to-end (two replicas + Redis +
  probe target) exercising heartbeats, probes, the lease, and leader failover.
- **Supply chain:** published images are cosign-signed and carry an SBOM and
  build provenance.
- **Observability extras:** `deploy/grafana-dashboard.json` and
  `deploy/prometheus-alerts.yml`.
- **Dependabot** for Python (uv), GitHub Actions, and Docker base images.

### Changed

- The probe HTTP client no longer follows redirects (a `302` to a login page
  must not read as "up"), and has a bounded connection pool and default
  timeout.
- Ping request bodies are read with a bounded buffer, so a large or endless
  body cannot exhaust memory (behaviour unchanged: still truncated, not
  rejected).

### Security

- Point Kubernetes `livenessProbe` at `/healthz` and `readinessProbe` at
  `/readyz`: a Redis blip no longer restarts a healthy pod.
