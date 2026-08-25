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
#
# Reading these logs on Railway: `railway logs <deployment-id> -d` shows
# BUILD output, not container stdout. To see the lines below you must pass
# a filter, e.g.
#
#     railway logs <deployment-id> -d --since 1h --filter 'start.sh'
#     railway logs <deployment-id> -d --since 1h --filter '@level:error'
#
# Without a filter the container's own output is invisible from the CLI,
# which is exactly how a dead database masqueraded for hours as a
# container that "never produced a single line of stdout".

set -e

# First line of output — proves the container entrypoint ran at all.
# (Debugging aid: a deploy that dies before this line is an image/CMD
# problem, after it is app-level.)
echo "[start.sh] boot $(date -u +%FT%TZ) — USE_POSTGRES=${USE_POSTGRES:-0} ENV=${ENV:-local}"

# Default PORT for local docker run. Railway / Fly inject this.
PORT="${PORT:-8000}"

if [ "${USE_POSTGRES:-0}" = "1" ] && [ -n "${DATABASE_URL:-}" ]; then
    echo "[start.sh] USE_POSTGRES=1 — running Alembic migrations"

    # Preflight the DB hostname before handing off to Alembic. On Railway a
    # `*.railway.internal` name only resolves while the target service has a
    # RUNNING deployment in the SAME environment — a stopped Postgres fails
    # DNS, and Alembic buries that in a 60-line SQLAlchemy/asyncpg traceback
    # whose only real content is `socket.gaierror: [Errno -2]`. Diagnose it
    # here in one line instead.
    db_host=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "${DATABASE_URL}" 2>/dev/null || true)
    if [ -n "${db_host}" ]; then
        dns_attempt=1
        dns_max=10
        until python3 -c 'import socket,sys; socket.getaddrinfo(sys.argv[1], None)' "${db_host}" 2>/dev/null; do
            if [ "$dns_attempt" -ge "$dns_max" ]; then
                echo "[start.sh] FATAL: database host '${db_host}' did not resolve after ${dns_max} attempts."
                echo "[start.sh]   A '*.railway.internal' name resolves ONLY while that service has a"
                echo "[start.sh]   running deployment in the SAME Railway environment. Check that the"
                echo "[start.sh]   Postgres service is deployed here, not just present in the sidebar."
                exit 1
            fi
            echo "[start.sh] waiting for DNS on ${db_host} (${dns_attempt}/${dns_max})"
            dns_attempt=$((dns_attempt + 1))
            sleep 3
        done
        echo "[start.sh] DNS ok: ${db_host}"
    fi

    # Retry the migration: the DB may still be provisioning on a cold
    # Railway deploy. Up to ~60s of retries (12 × 5s) before giving up.
    # Keep worst-case migration time well inside the Railway healthcheck
    # window: 6 attempts x (5s sleep + bounded lock wait). PGOPTIONS gives
    # Alembic's session a lock_timeout so a stuck advisory/DDL lock fails
    # fast instead of hanging the whole deploy.
    export PGOPTIONS="-c lock_timeout=30s -c statement_timeout=120s"
    attempt=1
    max_attempts=6
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
# --forwarded-allow-ips: trust X-Forwarded-* only from private ranges
# (Railway's edge proxies in-network). '*' let ANY client spoof
# X-Forwarded-For and defeat every per-IP rate limit (audit F9).
# Override via FORWARDED_ALLOW_IPS if the platform's proxy range differs.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers "${UVICORN_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1}"
