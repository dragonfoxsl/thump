# Working on thump

Rules for anyone — human or agent — changing this repo. They encode decisions
already baked into the code and CI; follow them so the next change doesn't
quietly undo one.

## 1. Test-driven, always

No production code without a failing test first. Red → green → refactor. Watch
the test fail *for the right reason* before implementing — a test that passes
the moment you write it has proven nothing. Bug fixes start with a test that
reproduces the bug.

## 2. Fail loud, never fail open

A monitor that lies is worse than one that's down.

- Invalid config aborts startup (`ConfigError`, non-zero exit) — never boot
  half-configured.
- Store unavailable → report `down`/`503`, never "all clear".
- Warnings are errors (`filterwarnings = ["error"]`). Triage a new one; don't
  scroll past it. Add a scoped `ignore::` only when it's understood and kept.
- A stale/missing install must fail loudly: tests run against the installed
  package (no `pythonpath` in pytest config). Run `uv sync --extra dev` first.

## 3. The gates must be green — check before claiming done

All three run in CI and must pass locally too:

```bash
uv run ruff check src tests
uv run mypy                       # strict, on src/thump
uv run pytest                     # coverage floor: --cov-fail-under=90
```

Never claim work is complete without running these and reading the output.
Real-Redis integration tests run when `REDIS_URL` is set (CI provides one):
`REDIS_URL=redis://localhost:6379/15 uv run pytest`.

## 4. Reproducible, locked builds

`uv.lock` is committed; CI installs with `--locked`; the image builds from it.
After changing a dependency, regenerate and commit the lockfile in the same
change. Pin the Python to `.python-version` (matches the container).

## 5. Keep the correctness core pure

State is decided at *read time* by `evaluator.evaluate(check, state, now)` — a
pure function, no I/O, no clock reads. `metrics.render_metrics` is pure too.
New correctness logic goes here, not into the ingest/probe write paths, so it
stays testable in microseconds.

## 6. One store seam, two backends

Any change to the `Store` protocol must keep **both** SQLite and Redis passing
the shared conformance suite (`tests/store/test_conformance.py`). Distributed
behaviour (the probe lease) also needs a **real-Redis** test — fakeredis does
not faithfully emulate `SET NX PX` expiry.

## 7. Security boundaries are deliberate

Don't erode them for convenience:

- `/status` stays unauthenticated and leaks nothing (`up`/`down` only).
- Admin endpoints (`/checks`, `/metrics`) fail **closed**: 404 when no
  `admin_token`, constant-time bearer compare otherwise.
- `/healthz` = liveness (never gated on the store); `/readyz` = readiness.
- Bound every request read; the container runs non-root; thump sits behind TLS.

See [SECURITY.md](SECURITY.md).

## 8. Supply chain

GitHub Actions are pinned to commit SHAs with the version in a trailing
comment — never a bare tag. Published images are cosign-signed with an SBOM
and build provenance. Resolve any new action's SHA from the GitHub API; don't
trust a SHA handed to you.

## 9. Comments say *why*, in the house voice

Match the existing terse, reasoned style. Document a deliberate decision and
its failure mode at the point of the decision, so it isn't "fixed" later.
Before changing documented behaviour, look for a test or comment that locks it
in (e.g. ping bodies are *truncated, not rejected*) and surface it rather than
silently flipping it.

## 10. Git

Branch off `main`; don't commit to it directly. Conventional-style prefixes
(`feat` / `fix` / `ci` / `docs` / `build` / `test` / `refactor`), and say
*why* in the body. Commit or push only when asked.
