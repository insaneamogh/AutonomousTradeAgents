# Multi-stage build for the FastAPI gateway — which also serves the web UI.
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
# Stage 0 — web UI: Expo's static web export of apps/mobile (Platinum
# Glass desktop + the calm/muted mobile UI — one bundle, client picks
# which to render). Node-only stage; nothing here reaches the final
# image except the exported apps/mobile/dist directory. Kept separate
# from the Python stages below so a JS dependency bump can't invalidate
# their (much slower) install-and-compile cache.
# ─────────────────────────────────────────────────────────────────────
FROM node:22-slim AS web-builder

RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

WORKDIR /repo

# Workspace manifests first — same cache-layering idea as the Python
# stage below: this layer only invalidates when a package.json/lockfile
# actually changes, not on every source edit.
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc tsconfig.base.json turbo.json ./
COPY apps/mobile/package.json apps/mobile/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/shared-types/package.json packages/shared-types/package.json
RUN pnpm install --frozen-lockfile

COPY apps/mobile apps/mobile
COPY packages/ui packages/ui
COPY packages/shared-types packages/shared-types

# ui and shared-types have no build step of their own — Metro resolves
# them straight from TS source, so this is the only compile this stage
# needs to run.
RUN pnpm --filter @app/mobile run build

# ─────────────────────────────────────────────────────────────────────
# Stage 1 — install Python dependencies into a dedicated layer for caching
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

# Web UI export from Stage 0. Lands at apps/mobile/dist relative to this
# WORKDIR, same as it would in a local checkout — app/main.py finds it by
# that relative path, no env var required.
COPY --from=web-builder /repo/apps/mobile/dist /app/apps/mobile/dist

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
