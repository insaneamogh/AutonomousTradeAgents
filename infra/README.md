# infra/

Local dev infrastructure + database migrations.

## Local stack
```bash
make infra-up    # Postgres (with TimescaleDB) + Redis
make infra-down  # stop
```

Postgres: `postgresql://app:app@localhost:5432/trading_agent`
Redis: `redis://localhost:6379/0`

## Migrations
Alembic, scoped here so both `apps/api` and `apps/agents` share a single schema source of truth.

```bash
# Generate
uv run alembic -c infra/migrations/alembic.ini revision --autogenerate -m "add orders table"
# Apply
uv run alembic -c infra/migrations/alembic.ini upgrade head
```

## Production
Fly.io for app services (api + agents), Fly Postgres + Upstash Redis for state. `docker-compose.yml` is **local-dev only** — not a deploy artifact.
