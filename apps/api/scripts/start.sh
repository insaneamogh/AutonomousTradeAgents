#!/bin/sh
# Container entry point.
#
# Steps:
#   1. If USE_POSTGRES=1 + DATABASE_URL is set, run Alembic migrations
#      (with a retry loop — Railway can start the app + Postgres near-
#      simultaneously, so the DB may not accept connections on the first
#      attempt). Migrations are idempotent — `upgrade head` is safe to
#      re-run.
#   2. Launch uvicorn bound to 0.0.0.0:$PORT.

set -e

# Default PORT for local docker run. Railway / Fly inject this.
PORT="${PORT:-8000}"

if [ "${USE_POSTGRES:-0}" = "1" ] && [ -n "${DATABASE_URL:-}" ]; then
    echo "[start.sh] USE_POSTGRES=1 — running Alembic migrations"

    # Retry the migration: the DB may still be provisioning on a cold
    # Railway deploy. Up to ~60s of retries (12 × 5s) before giving up.
    attempt=1
    max_attempts=12
    until alembic -c /app/infra/migrations/alembic.ini upgrade head; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            echo "[start.sh] Migrations failed after ${max_attempts} attempts — exiting"
            exit 1
        fi
        echo "[start.sh] Migration attempt ${attempt}/${max_attempts} failed (DB not ready?) — retrying in 5s"
        attempt=$((attempt + 1))
        sleep 5
    done
    echo "[start.sh] Migrations applied"
else
    echo "[start.sh] USE_POSTGRES not set or DATABASE_URL empty — skipping migrations (MockStore mode)"
fi

echo "[start.sh] Launching uvicorn on 0.0.0.0:${PORT}"
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers "${UVICORN_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
