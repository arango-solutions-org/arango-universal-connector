FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Built from the repo root (the uv workspace): docker build -f packages/standalone/Dockerfile .
# Install dependencies first for better layer caching, then the two workspace packages
# this image needs: arango-connector (standalone) and arango-connector-core.
COPY pyproject.toml uv.lock README.md ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/standalone/pyproject.toml packages/standalone/
RUN uv sync --frozen --no-dev --package arango-connector --no-install-workspace

COPY packages/core/src packages/core/src
COPY packages/standalone/src packages/standalone/src
RUN uv sync --frozen --no-dev --package arango-connector --no-editable

RUN useradd --create-home --uid 10001 connector \
    && mkdir -p /data \
    && chown connector:connector /data
USER connector

ENV DEAD_LETTER_PATH=/data/dead_letter.jsonl

CMD ["python", "-m", "arango_connector_standalone.main"]
