"""NotificationStore — device_tokens backing.

Same Protocol-+-Mock-+-Postgres-later pattern. PostgresNotificationStore
wired against migration 0005 ships in the Postgres-adapters follow-on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from app.core.singleton import LazyEnvSingleton
from app.core.time import utc_now

Platform = Literal["ios", "android", "web"]


@dataclass
class DeviceTokenRecord:
    id: str
    user_id: str
    expo_push_token: str
    platform: Platform
    label: str | None
    created_at: datetime = field(default_factory=lambda: utc_now())
    last_seen_at: datetime = field(default_factory=lambda: utc_now())
    revoked_at: datetime | None = None


@runtime_checkable
class NotificationStore(Protocol):
    async def register_device(
        self,
        *,
        user_id: str,
        expo_push_token: str,
        platform: Platform,
        label: str | None = None,
    ) -> DeviceTokenRecord: ...

    async def list_devices(self, user_id: str) -> list[DeviceTokenRecord]: ...

    async def list_active_devices(self, user_id: str) -> list[DeviceTokenRecord]: ...

    async def get_device(self, device_id: str) -> DeviceTokenRecord | None: ...

    async def revoke_device(self, device_id: str) -> bool: ...

    async def revoke_by_token(self, expo_push_token: str) -> None:
        """Revoke any row carrying this token. Called by the Expo Push client
        when Expo reports DeviceNotRegistered for it.
        """


class InMemoryNotificationStore:
    """Default in-memory backing."""

    def __init__(self) -> None:
        self._rows: dict[str, DeviceTokenRecord] = {}

    async def register_device(
        self,
        *,
        user_id: str,
        expo_push_token: str,
        platform: Platform,
        label: str | None = None,
    ) -> DeviceTokenRecord:
        # Idempotent on (user_id, expo_push_token). If a row exists, refresh
        # last_seen_at + un-revoke (user opted back in).
        existing = next(
            (
                r for r in self._rows.values()
                if r.user_id == user_id and r.expo_push_token == expo_push_token
            ),
            None,
        )
        now = utc_now()
        if existing is not None:
            existing.last_seen_at = now
            existing.revoked_at = None
            existing.platform = platform
            if label is not None:
                existing.label = label
            return existing

        rec = DeviceTokenRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            expo_push_token=expo_push_token,
            platform=platform,
            label=label,
        )
        self._rows[rec.id] = rec
        return rec

    async def list_devices(self, user_id: str) -> list[DeviceTokenRecord]:
        return [r for r in self._rows.values() if r.user_id == user_id]

    async def list_active_devices(self, user_id: str) -> list[DeviceTokenRecord]:
        return [
            r for r in self._rows.values()
            if r.user_id == user_id and r.revoked_at is None
        ]

    async def get_device(self, device_id: str) -> DeviceTokenRecord | None:
        return self._rows.get(device_id)

    async def revoke_device(self, device_id: str) -> bool:
        rec = self._rows.get(device_id)
        if rec is None or rec.revoked_at is not None:
            return False
        rec.revoked_at = utc_now()
        return True

    async def revoke_by_token(self, expo_push_token: str) -> None:
        for r in self._rows.values():
            if r.expo_push_token == expo_push_token and r.revoked_at is None:
                r.revoked_at = utc_now()


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def _build_postgres_notification_store() -> NotificationStore:
    from app.services.notifications.postgres_notification_store import (
        PostgresNotificationStore,
    )

    return PostgresNotificationStore()


_notification_store: LazyEnvSingleton[NotificationStore] = LazyEnvSingleton(
    InMemoryNotificationStore, _build_postgres_notification_store
)


def get_notification_store() -> NotificationStore:
    return _notification_store.get()


def reset_notification_store_for_tests() -> None:
    _notification_store.reset()
