"""Watchlist store — the symbols a user told the agent to track.

Same Protocol + InMemory + Postgres pattern as the rest of the app.
v1 is stocks/ETFs only (locked decision): ``asset_class`` is persisted as
'equity' and anything else is rejected at the router. The column exists so
options can slot in later without a schema rework.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.singleton import LazyEnvSingleton
from app.core.time import utc_now

logger = logging.getLogger("api.watchlist_store")

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


@dataclass(frozen=True)
class WatchlistItem:
    id: str
    user_id: str
    symbol: str
    asset_class: str
    active: bool
    created_at: datetime


@runtime_checkable
class WatchlistStore(Protocol):
    async def list_items(self, user_id: str) -> list[WatchlistItem]: ...
    async def add(self, user_id: str, symbol: str) -> WatchlistItem: ...
    async def remove(self, user_id: str, symbol: str) -> bool: ...


class InMemoryWatchlistStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], WatchlistItem] = {}

    async def list_items(self, user_id: str) -> list[WatchlistItem]:
        return sorted(
            (i for (uid, _), i in self._items.items() if uid == user_id),
            key=lambda i: i.symbol,
        )

    async def add(self, user_id: str, symbol: str) -> WatchlistItem:
        key = (user_id, symbol.upper())
        existing = self._items.get(key)
        if existing is not None:
            return existing
        item = WatchlistItem(
            id=str(uuid.uuid4()),
            user_id=user_id,
            symbol=symbol.upper(),
            asset_class="equity",
            active=True,
            created_at=utc_now(),
        )
        self._items[key] = item
        return item

    async def remove(self, user_id: str, symbol: str) -> bool:
        return self._items.pop((user_id, symbol.upper()), None) is not None


class PostgresWatchlistStore:
    def __init__(self) -> None:
        from engine.db.session import async_session_factory

        self._session_factory = async_session_factory()

    async def list_items(self, user_id: str) -> list[WatchlistItem]:
        from sqlalchemy import select

        from engine.db.models import UserWatchlistItem

        async with self._session_factory() as session:
            stmt = (
                select(UserWatchlistItem)
                .where(UserWatchlistItem.user_id == uuid.UUID(user_id))
                .where(UserWatchlistItem.active.is_(True))
                .order_by(UserWatchlistItem.symbol)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [
            WatchlistItem(
                id=str(r.id),
                user_id=str(r.user_id),
                symbol=r.symbol,
                asset_class=r.asset_class,
                active=r.active,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def add(self, user_id: str, symbol: str) -> WatchlistItem:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from engine.db.models import UserWatchlistItem

        sym = symbol.upper()
        uid = uuid.UUID(user_id)
        async with self._session_factory() as session:
            stmt = (
                pg_insert(UserWatchlistItem)
                .values(id=uuid.uuid4(), user_id=uid, symbol=sym, asset_class="equity", active=True)
                .on_conflict_do_update(
                    constraint="uq_user_watchlist_user_symbol",
                    set_={"active": True},
                )
            )
            await session.execute(stmt)
            await session.commit()

            row_stmt = (
                select(UserWatchlistItem)
                .where(UserWatchlistItem.user_id == uid)
                .where(UserWatchlistItem.symbol == sym)
            )
            row = (await session.execute(row_stmt)).scalar_one()
        return WatchlistItem(
            id=str(row.id),
            user_id=str(row.user_id),
            symbol=row.symbol,
            asset_class=row.asset_class,
            active=row.active,
            created_at=row.created_at,
        )

    async def remove(self, user_id: str, symbol: str) -> bool:
        from sqlalchemy import update

        from engine.db.models import UserWatchlistItem

        async with self._session_factory() as session:
            result = await session.execute(
                update(UserWatchlistItem)
                .where(UserWatchlistItem.user_id == uuid.UUID(user_id))
                .where(UserWatchlistItem.symbol == symbol.upper())
                .where(UserWatchlistItem.active.is_(True))
                .values(active=False)
            )
            await session.commit()
        return bool(result.rowcount)


_store: LazyEnvSingleton[WatchlistStore] = LazyEnvSingleton(
    InMemoryWatchlistStore, PostgresWatchlistStore
)


def get_watchlist_store() -> WatchlistStore:
    return _store.get()


def reset_watchlist_store_for_tests() -> None:
    """Drop the singleton. Tests use this to start from a clean state.

    Added for parity with the other five stores in this refactor — nothing
    currently calls it (no test exercises cross-test state for the
    watchlist store yet), but every store should have the reset half of
    the pair available so a future test doesn't have to add it under
    time pressure.
    """
    _store.reset()
