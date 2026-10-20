FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Built from the repo root (the uv workspace): docker build -f packages/links/Dockerfile .
# Dependencies first for layer caching, then the two workspace packages this image needs:
# arango-connector-links and arango-connector-core.
COPY pyproject.toml uv.lock README.md ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/links/pyproject.toml packages/links/
RUN uv sync --frozen --no-dev --package arango-connector-links --no-install-workspace

COPY packages/core/src packages/core/src
COPY packages/links/src packages/links/src
RUN uv sync --frozen --no-dev --package arango-connector-links --no-editable

RUN useradd --create-home --uid 10001 connector
USER connector

# The link to run is the first argument, e.g. `arango-connector-link gmail`.
ENTRYPOINT ["arango-connector-link"]
CMD ["selftest"]
