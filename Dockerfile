# syntax=docker/dockerfile:1.6
# ---- builder ----------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# System deps: git for KB cloning, build-essential for any wheels that need to compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

# Install into a venv so we can copy a clean tree to the runtime image.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---- runtime ----------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PKB_TRANSPORT=sse \
    PKB_DATA_DIR=/data \
    PKB_KB_ROOT=/data/notes \
    PKB_DB_PATH=/data/kb.db \
    PKB_CACHE_DIR=/data/.fastembed_cache

# git: needed at runtime to pull the KB repo on boot + on /webhook/sync.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 pkb

COPY --from=builder /opt/venv /opt/venv

# Persistent volume mount target on Railway.
RUN mkdir -p /data && chown -R pkb:pkb /data
USER pkb
WORKDIR /home/pkb

EXPOSE 8000

# Healthcheck targets /healthz which is unauthenticated.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -m pkb.healthcheck || exit 1

# pkb-mcp reads PORT from env (Railway sets it) — uvicorn binds 0.0.0.0:$PORT.
CMD ["pkb-mcp"]
