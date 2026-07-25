# thump Handoff

## Current State

- Default branch: `main`
- Current documentation branch: `docs/public-readme-cleanup`
- Remote: `git@github.com:dragonfoxsl/thump.git`
- CI runs on push, pull request, manual dispatch, and Fridays at `03:30 UTC`.
- Version `0.1.0` is published as a GHCR container on `v*` tags. The unrelated
  PyPI project named `thump` is not this repository and must not be modified.

## Maintainer Rules

- Keep public README content focused on users and contributors.
- Put agent instructions, branch state, dated PR lists, and operational handoff
  notes in `HANDOFF.md` or `AGENTS.md`, never in the public README.
- Keep both SQLite and Redis implementations passing the shared conformance suite.
- Keep release actions push-only; scheduled and manual CI must not publish or sign images.
- Keep third-party GitHub Actions pinned to verified commit SHAs.

## Verification Baseline

- Lint: `uv run ruff check src tests`
- Types: `uv run mypy`
- Tests: `uv run pytest`
- Real Redis: `REDIS_URL=redis://localhost:6379/15 uv run pytest`
- Container: `docker buildx build --platform linux/amd64 --output=type=cacheonly .`

## Open Items

- Dependabot audit (2026-07-25): no open version-update PRs.
- Review the configured GHCR package description separately from repository README changes.
