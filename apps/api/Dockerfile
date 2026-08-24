# Multi-stage build for the FastAPI gateway.
#
# Third-party deps are installed from ``uv.lock`` (exported to a pinned
# requirements file at build time) so the container can never drift from
# the workspace again — the previous hand-maintained list silently
# omitted langgraph/langfuse/litellm/pandas and left those features dead
# in production. Workspace members (broker, engine, agents) stay
# path-imported via PYTHONPATH at runtime, exactly like local dev.
#
# Build context is the repo root. Build command (Railway picks this up
# from ``railway.toml``):
#
#     docker build -f apps/api/Dockerfile -t autotrader-api .
#
# Run:
#
#     docker run -e PORT=8000 -e DATABASE_URL=postgresql://... \
#                -e USE_POSTGRES=1 -e JWT_SECRET=... \
#                -p 8000:8000 autotrader-api

# ─────────────────────────────────────────────────────────────────────
# Stage 1 — install dependencies into a dedicated layer for caching
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS deps

# System packages required for: cryptography (libffi), asyncpg (no compile
# step in the wheel, but libpq client libs for psql introspection on push),
# bcrypt (libffi).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libpq5 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv resolves + installs from the committed lockfile.
RUN pip install --no-cache-dir uv==0.10.2

# Install third-party deps straight from uv.lock — single source of
# truth shared with local dev. ``--all-packages`` covers every workspace
# member's dependencies (api + agents + engine + broker);
# ``--no-emit-workspace`` keeps the members themselves out (they're
# PYTHONPATH-imported at runtime, not installed).
COPY pyproject.toml uv.lock /tmp/workspace/
COPY apps/api/pyproject.toml      /tmp/workspace/apps/api/
COPY apps/agents/pyproject.toml   /tmp/workspace/apps/agents/
COPY packages/engine/pyproject.toml /tmp/workspace/packages/engine/
COPY packages/broker/pyproject.toml /tmp/workspace/packages/broker/
RUN cd /tmp/workspace \
    && uv export --frozen --no-dev --no-emit-workspace --no-hashes \
         --all-packages -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && rm -rf /tmp/workspace /tmp/requirements.txt

# ─────────────────────────────────────────────────────────────────────
# Stage 2 — runtime image
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Keep the runtime image minimal — just libpq5 for the asyncpg binary
# wheel + the certs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed site-packages from the deps stage. ``/usr/local`` holds
# the python install + the packages we just put down.
COPY --from=deps /usr/local /usr/local

WORKDIR /app

# Source layout. The order matters only for cache reuse — we put the
# rarely-changing infra/ and packages/ first.
COPY infra            /app/infra
COPY packages/engine  /app/packages/engine
COPY packages/broker  /app/packages/broker
COPY apps/agents      /app/apps/agents
COPY apps/api         /app/apps/api

# Workspace siblings are imported via PYTHONPATH, exactly like
# ``make dev-api`` does locally.
ENV PYTHONPATH=/app/apps/api:/app/apps/agents:/app/packages/engine:/app/packages/broker
ENV PYTHONUNBUFFERED=1

# Start script handles ``alembic upgrade head`` then launches uvicorn
# bound to 0.0.0.0:$PORT. Railway / Fly inject $PORT at runtime.
COPY apps/api/scripts/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

EXPOSE 8000

# Health check matches the FastAPI /health endpoint. Railway uses its
# own healthcheck (railway.toml → healthcheckPath) + ignores this; it's
# here for local `docker run` + other orchestrators. Exec form with an
# explicit sh -c keeps the Python quoting clean.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["sh", "-c", "python -c 'import os,urllib.request; urllib.request.urlopen(\"http://127.0.0.1:\" + os.environ.get(\"PORT\",\"8000\") + \"/health\")'"]

CMD ["/usr/local/bin/start.sh"]
