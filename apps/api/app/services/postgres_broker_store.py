"""PostgresBrokerStore — broker_connections-backed BrokerStore.

Wired against migration 0001's ``broker_connections`` table. The
``(user_id, broker, is_paper)`` UQ drives the upsert path. Token columns
hold Fernet ciphertext only — encryption happens at the router boundary
via ``app.services.crypto``; this store never sees plaintext.

The ``PendingOAuthCache`` stays in-memory — it's a hot path that belongs
in Redis, not Postgres. Migration to Redis is a Phase 3.2 follow-on.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.services.broker_store import BrokerConnectionRecord
from engine.db import async_session_factory
from engine.db.models import BrokerConnection

logger = logging.getLogger("api.broker_store.postgres")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_record(b: BrokerConnection) -> BrokerConnectionRecord:
    return BrokerConnectionRecord(
        id=str(b.id),
        user_id=str(b.user_id),
        broker=b.broker.lower(),
        is_paper=b.is_paper,
        account_number=b.account_number,
        encrypted_access_token=b.encrypted_access_token,
        encrypted_refresh_token=b.encrypted_refresh_token,
        access_token_expires_at=b.access_token_expires_at,
        refresh_token_expires_at=b.refresh_token_expires_at,
        status=b.status,
        last_used_at=b.last_used_at,
        created_at=b.created_at,
        updated_at=b.updated_at,
    )


class PostgresBrokerStore:
    def __init__(self) -> None:
        self._session_factory = async_session_factory()

    async def upsert_connection(
        self,
        *,
        user_id: str,
        broker: str,
        is_paper: bool,
        account_number: str | None,
        encrypted_access_token: str,
        encrypted_refresh_token: str | None,
        access_token_expires_at: datetime | None,
    ) -> BrokerConnectionRecord:
        broker = broker.lower()
        uid = uuid.UUID(user_id)

        async with self._session_factory() as session:
            # ON CONFLICT (user_id, broker, is_paper) DO UPDATE — exactly
            # what we want: re-issuing a connection rotates the encrypted
            # tokens in place. The UQ name matches migration 0001.
            stmt = (
                pg_insert(BrokerConnection)
                .values(
                    id=uuid.uuid4(),
                    user_id=uid,
                    broker=broker,
                    is_paper=is_paper,
                    account_number=account_number,
                    encrypted_access_token=encrypted_access_token,
                    encrypted_refresh_token=encrypted_refresh_token,
                    access_token_expires_at=access_token_expires_at,
                    status="active",
                )
                .on_conflict_do_update(
                    constraint="uq_broker_connections_user_broker_env",
                    set_=dict(
                        account_number=account_number,
                        encrypted_access_token=encrypted_access_token,
                        encrypted_refresh_token=encrypted_refresh_token,
                        access_token_expires_at=access_token_expires_at,
                        status="active",
                        updated_at=_now(),
                    ),
                )
                .returning(BrokerConnection.id)
            )
            row_id = (await session.execute(stmt)).scalar_one()
            await session.commit()
            row = await session.get(BrokerConnection, row_id)
            assert row is not None
        return _row_to_record(row)

    async def list_connections(self, user_id: str) -> list[BrokerConnectionRecord]:
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return []
        async with self._session_factory() as session:
            stmt = select(BrokerConnection).where(BrokerConnection.user_id == uid)
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def list_active_connections_by_broker(
        self, broker: str
    ) -> list[BrokerConnectionRecord]:
        """All ACTIVE connections for one broker, across users. Cron fan-out
        (e.g. the Zerodha daily-reconnect reminder) iterates this.
        """
        async with self._session_factory() as session:
            stmt = select(BrokerConnection).where(
                BrokerConnection.broker == broker.lower(),
                BrokerConnection.status == "active",
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def get_connection(self, connection_id: str) -> BrokerConnectionRecord | None:
        try:
            cid = uuid.UUID(connection_id)
        except (ValueError, TypeError):
            return None
        async with self._session_factory() as session:
            row = await session.get(BrokerConnection, cid)
        return _row_to_record(row) if row is not None else None

    async def revoke_connection(self, connection_id: str) -> bool:
        try:
            cid = uuid.UUID(connection_id)
        except (ValueError, TypeError):
            return False
        async with self._session_factory() as session:
            result = await session.execute(
                update(BrokerConnection)
                .where(BrokerConnection.id == cid, BrokerConnection.status != "revoked")
                .values(
                    status="revoked",
                    encrypted_access_token="",
                    encrypted_refresh_token=None,
                    updated_at=_now(),
                )
            )
            await session.commit()
        return bool(result.rowcount)
