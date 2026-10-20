# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.13.3 AS uv

FROM python:3.12-slim-trixie

COPY --from=uv /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root system user the tool runs as.
RUN groupadd --system app && useradd --system --gid app app

WORKDIR /app

# Install locked dependencies first; cached unless pyproject/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Then copy the project and install it (README.md is the package readme).
COPY . .
RUN uv sync --locked

USER app

# Flags passed to `docker run` append to the entry point, so the image runs
# `paperless-tax-export --help` by default and `docker run <image> --dry-run`
# behaves as the tool's own flag.
ENTRYPOINT ["paperless-tax-export"]
CMD ["--help"]