# syntax=docker/dockerfile:1.7
#
# Multi-arch (linux/amd64, linux/arm64). Build with buildx:
#   docker buildx build --platform linux/amd64,linux/arm64 -t thump:dev .
#
# Dependencies come from uv.lock, so the image ships the exact versions the
# test suite ran against rather than whatever resolved at build time.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build

# Copy rather than symlink: the runtime stage copies the venv out of this
# stage, and symlinks into uv's cache would dangle there.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

ARG TARGETARCH

# Dependencies first, without the project. This layer is keyed only on the
# lockfile, so editing source re-downloads nothing. The cache mount is scoped
# per-arch so two architectures don't fight over one cache during a multi-arch
# build.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY pyproject.toml uv.lock ./
COPY src ./src

# Then the project itself, as a separate layer so a source edit rebuilds only
# this step. --no-editable bakes the package in rather than linking to /app/src.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH \
    uv sync --locked --no-editable --no-dev


FROM python:3.14-slim-bookworm

# Copy the resolved venv rather than installing again: it is already resolved,
# byte-compiled, and correct for this architecture.
COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    THUMP_CONFIG=/etc/thump/config.yaml

RUN useradd -r -u 10001 thump \
    && mkdir -p /var/lib/thump \
    && chown thump /var/lib/thump

USER thump
EXPOSE 8080

# Liveness for plain `docker run` users (k8s uses the /healthz + /readyz probes
# directly). No curl in the slim image, so drive it with stdlib Python.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]

CMD ["python", "-m", "thump.main"]
