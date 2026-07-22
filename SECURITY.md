# Security

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
private vulnerability reporting: the **Security** tab → **Report a
vulnerability**. That opens a private advisory visible only to the maintainers.

## Supported versions

Fixes land on `main` and ship in the next image build. Run a recent `latest`
or a pinned `v*` tag; there is no long-term support branch.

## Security model

thump is designed to sit **inside** your network and be reached by cron jobs,
your uptime vendor, and (optionally) Prometheus. Its boundaries are deliberate:

- **Transport is your job.** thump speaks plain HTTP. The ping token is the
  credential and travels in the URL; the admin token travels in a header.
  **Terminate TLS at a reverse proxy or ingress** in front of thump — without
  it, both are exposed on the wire.
- **Ping tokens.** Each is `HMAC-SHA256(secret, check_name)`, first 32 hex
  chars (128 bits) — not brute-forceable. The URL *is* the credential, so there
  is nothing else to leak. Keep `THUMP_SECRET` secret; rotating it rotates
  every token at once. An unknown token returns `404`, never `401`, so probing
  the token space reveals nothing about what exists.
- **Admin endpoints.** `/checks` and `/metrics` require a bearer `admin_token`,
  compared in constant time. If no `admin_token` is configured they are
  **disabled (404), not open** — fail closed. This is where internal topology
  (names, state, event history) lives.
- **`/status` is intentionally unauthenticated** because your vendor must reach
  it, and it leaks nothing: the body is literally `up` or `down` — no names, no
  timestamps, no JSON.
- **Request bodies are bounded.** Ping bodies are read up to a fixed cap
  (`MAX_BODY_BYTES`, 4096) and truncated, so a large or endless POST cannot
  exhaust memory.
- **The container runs as a non-root user** (uid 10001) on a slim base with no
  build tooling.

## Out of scope

- **Rate limiting / DoS.** thump does not rate-limit. Put it behind a reverse
  proxy or WAF if it is exposed to untrusted networks.
- **Redis/SQLite access control.** Anything that can reach the store can read
  and write thump's state. Keep the store on a private network.
