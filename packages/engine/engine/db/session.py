"""Async SQLAlchemy engine + session factory.

Single global engine per process — created lazily on first call. Callers get
a session via ``async with async_session_factory() as session: ...``.

DATABASE_URL handling:
    - Default (local dev) is ``postgresql+asyncpg://app:app@localhost:5432/trading_agent``.
    - Railway / Render / most hosted Postgres providers hand out URLs in the
      sync form ``postgresql://user:pass@host:port/db``. We rewrite to the
      async dialect transparently so operators don't have to mangle their
      Railway-provided env var.
    - ``postgres://`` (Heroku legacy spelling) is also accepted.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_DEFAULT_URL = "postgresql+asyncpg://app:app@localhost:5432/trading_agent"


def _coerce_to_async_dialect(url: str) -> str:
    """Rewrite a sync ``postgresql://`` URL into the async ``+asyncpg`` form.

    Examples:
        ``postgresql://u:p@h/d``         → ``postgresql+asyncpg://u:p@h/d``
        ``postgres://u:p@h/d``           → ``postgresql+asyncpg://u:p@h/d``
        ``postgresql+asyncpg://u:p@h/d`` → unchanged
        ``sqlite+aiosqlite:///x.db``     → unchanged (any non-postgres dialect)
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql+psycopg://") or url.startswith("postgresql+psycopg2://"):
        # Caller explicitly wants psycopg — respect their choice + warn loudly
        # by leaving it alone (engine creation will fail at runtime).
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


def _database_url() -> str:
    raw = os.environ.get("DATABASE_URL", _DEFAULT_URL)
    return _coerce_to_async_dialect(raw)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Lazy singleton. Re-import after monkeypatching DATABASE_URL in tests."""
    return create_async_engine(
        _database_url(),
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


@lru_cache(maxsize=1)
def _session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)


def async_session_factory() -> async_sessionmaker[AsyncSession]:
    return _session_factory()
