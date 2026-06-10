"""Alembic environment.

Imports ``engine.db.Base`` (and the side-effecting ``engine.db.models``) so
``--autogenerate`` can diff the live DB against the declared schema.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Register all models with Base.metadata.
from engine.db import Base
from engine.db import models  # noqa: F401  side-effect: register tables
from engine.db.session import _coerce_to_async_dialect

config = context.config

# Allow DATABASE_URL env to override alembic.ini for CI / Fly.io / Railway.
# Railway-style URLs come in as ``postgresql://...`` (sync form); we coerce
# them to the async dialect here so the engine binds correctly.
if (env_url := os.environ.get("DATABASE_URL")) is not None:
    config.set_main_option("sqlalchemy.url", _coerce_to_async_dialect(env_url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
