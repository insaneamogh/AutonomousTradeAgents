"""PostgresNotificationStore — device_tokens-backed NotificationStore.

Wired against migration 0005. The ``(user_id, expo_push_token)`` UQ
drives idempotent register; ``revoke_by_token`` updates any matching
rows in one statement.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.services.notification_store import DeviceTokenRecord, Platform
from engine.db import async_session_factory
from engine.db.models import DeviceToken

logger = logging.getLogger("api.notification_store.postgres")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_record(r: DeviceToken) -> DeviceTokenRecord:
    return DeviceTokenRecord(
        id=str(r.id),
        user_id=str(r.user_id),
        expo_push_token=r.expo_push_token,
        platform=cast(Platform, r.platform),
        label=r.label,
        created_at=r.created_at,
        last_seen_at=r.last_seen_at,
        revoked_at=r.revoked_at,
    )


class PostgresNotificationStore:
    def __init__(self) -> None:
        self._session_factory = async_session_factory()

    async def register_device(
        self,
        *,
        user_id: str,
        expo_push_token: str,
        platform: Platform,
        label: str | None = None,
    ) -> DeviceTokenRecord:
        uid = uuid.UUID(user_id)
        now = _now()

        async with self._session_factory() as session:
            # ON CONFLICT (user_id, expo_push_token) DO UPDATE:
            #   - refresh last_seen_at
            #   - un-revoke (user opted back in)
            #   - allow platform/label drift (e.g. relabeled device)
            update_set = dict(
                platform=platform,
                last_seen_at=now,
                revoked_at=None,
            )
            if label is not None:
                update_set["label"] = label

            stmt = (
                pg_insert(DeviceToken)
                .values(
                    id=uuid.uuid4(),
                    user_id=uid,
                    expo_push_token=expo_push_token,
                    platform=platform,
                    label=label,
                )
                .on_conflict_do_update(
                    constraint="uq_device_tokens_user_token",
                    set_=update_set,
                )
                .returning(DeviceToken.id)
            )
            row_id = (await session.execute(stmt)).scalar_one()
            await session.commit()
            row = await session.get(DeviceToken, row_id)
            assert row is not None
        return _row_to_record(row)

    async def list_devices(self, user_id: str) -> list[DeviceTokenRecord]:
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return []
        async with self._session_factory() as session:
            stmt = select(DeviceToken).where(DeviceToken.user_id == uid)
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def list_active_devices(self, user_id: str) -> list[DeviceTokenRecord]:
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return []
        async with self._session_factory() as session:
            stmt = select(DeviceToken).where(
                DeviceToken.user_id == uid,
                DeviceToken.revoked_at.is_(None),
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def get_device(self, device_id: str) -> DeviceTokenRecord | None:
        try:
            did = uuid.UUID(device_id)
        except (ValueError, TypeError):
            return None
        async with self._session_factory() as session:
            row = await session.get(DeviceToken, did)
        return _row_to_record(row) if row is not None else None

    async def revoke_device(self, device_id: str) -> bool:
        try:
            did = uuid.UUID(device_id)
        except (ValueError, TypeError):
            return False
        async with self._session_factory() as session:
            result = await session.execute(
                update(DeviceToken)
                .where(DeviceToken.id == did, DeviceToken.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            await session.commit()
        return bool(result.rowcount)

    async def revoke_by_token(self, expo_push_token: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(DeviceToken)
                .where(
                    DeviceToken.expo_push_token == expo_push_token,
                    DeviceToken.revoked_at.is_(None),
                )
                .values(revoked_at=_now())
            )
            await session.commit()
