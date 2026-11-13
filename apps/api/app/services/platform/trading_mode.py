"""The platform's paper / real-money switch, and the ONE place that decides it.

Before this module the only way to turn real-money trading on was the env
var ``LIVE_TRADING_ENABLED`` plus a redeploy. An admin can now flip it at
runtime. Every reader of "is live trading on?" goes through
``live_trading_state()`` (CLAUDE.md §4.4: one number, one place).

Resolution, in order:

  1. env ``LIVE_TRADING_ENABLED`` truthy       -> enabled,  source "env"
  2. env set to a falsy / unrecognised value   -> disabled, source "env",
     ``locked=True``: a deploy-level lock the admin switch cannot override.
     An unrecognised value (a typo) locks too — it fails closed, matching
     ``env_flag``. An empty string counts as unset (Railway often leaves
     empty variables behind).
  3. env unset                                 -> the stored admin value
     (default False), source "admin" once an admin has set it, else
     "default".
  4. ANY error reading the store               -> disabled. Fail closed.

This is only one of the keys for a real order: the connection must also be
live (non-paper) and carry its own ``live_trading_consent``.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from app.core.singleton import LazyEnvSingleton
from app.core.time import utc_now
from engine.env import env_flag

logger = logging.getLogger("api.trading_mode")

SETTING_KEY = "live_trading_enabled"
_FALSY = frozenset({"0", "false", "no", "off"})

Source = Literal["env", "admin", "default"]


@dataclass(frozen=True)
class LiveTradingState:
    enabled: bool
    source: Source
    locked: bool = False
    """True when env pins it OFF; the admin switch cannot override."""


@dataclass(frozen=True)
class SettingRecord:
    key: str
    value: str
    updated_by: str | None
    updated_at: datetime


@dataclass(frozen=True)
class AuditRecord:
    id: str
    key: str
    old_value: str | None
    new_value: str
    changed_by: str | None
    changed_at: datetime


class SettingStore(Protocol):
    async def get(self, key: str) -> SettingRecord | None: ...

    async def set(self, key: str, value: str, *, changed_by: str | None) -> SettingRecord:
        """Upsert AND append the audit row, atomically."""
        ...

    async def audit(self, key: str) -> list[AuditRecord]: ...


class InMemorySettingStore:
    def __init__(self) -> None:
        self._rows: dict[str, SettingRecord] = {}
        self._audit: list[AuditRecord] = []

    async def get(self, key: str) -> SettingRecord | None:
        return self._rows.get(key)

    async def set(self, key: str, value: str, *, changed_by: str | None) -> SettingRecord:
        now = utc_now()
        old = self._rows.get(key)
        rec = SettingRecord(key, value, changed_by, now)
        self._rows[key] = rec
        self._audit.append(
            AuditRecord(
                str(uuid.uuid4()), key, old.value if old else None, value, changed_by, now
            )
        )
        return rec

    async def audit(self, key: str) -> list[AuditRecord]:
        return [a for a in self._audit if a.key == key]


class PostgresSettingStore:
    def __init__(self) -> None:
        from engine.db import async_session_factory

        self._session_factory = async_session_factory()

    async def get(self, key: str) -> SettingRecord | None:
        from engine.db.models import PlatformSetting

        async with self._session_factory() as session:
            row = await session.get(PlatformSetting, key)
            if row is None:
                return None
            return SettingRecord(row.key, row.value, row.updated_by, row.updated_at)

    async def set(self, key: str, value: str, *, changed_by: str | None) -> SettingRecord:
        from engine.db.models import PlatformSetting, PlatformSettingAudit

        now = utc_now()
        async with self._session_factory() as session:
            row = await session.get(PlatformSetting, key, with_for_update=True)
            old_value = row.value if row is not None else None
            if row is None:
                session.add(
                    PlatformSetting(key=key, value=value, updated_by=changed_by, updated_at=now)
                )
            else:
                row.value = value
                row.updated_by = changed_by
                row.updated_at = now
            session.add(
                PlatformSettingAudit(
                    id=uuid.uuid4(),
                    key=key,
                    old_value=old_value,
                    new_value=value,
                    changed_by=changed_by,
                    changed_at=now,
                )
            )
            await session.commit()  # setting + audit row in one transaction
        return SettingRecord(key, value, changed_by, now)

    async def audit(self, key: str) -> list[AuditRecord]:
        from sqlalchemy import select

        from engine.db.models import PlatformSettingAudit

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(PlatformSettingAudit)
                    .where(PlatformSettingAudit.key == key)
                    .order_by(PlatformSettingAudit.changed_at)
                )
            ).scalars().all()
        return [
            AuditRecord(str(r.id), r.key, r.old_value, r.new_value, r.changed_by, r.changed_at)
            for r in rows
        ]


def _build_postgres_store() -> SettingStore:
    return PostgresSettingStore()


_store: LazyEnvSingleton[SettingStore] = LazyEnvSingleton(
    InMemorySettingStore, _build_postgres_store
)


def get_setting_store() -> SettingStore:
    return _store.get()


def reset_setting_store_for_tests() -> None:
    _store.reset()


def _env_verdict() -> LiveTradingState | None:
    """The env's say, or None when env is unset/empty (the admin decides)."""
    raw = os.environ.get("LIVE_TRADING_ENABLED")
    if raw is None or not raw.strip():
        return None
    if env_flag("LIVE_TRADING_ENABLED"):
        return LiveTradingState(enabled=True, source="env")
    # Explicit falsy OR an unrecognised value: either way a deploy-level lock.
    if raw.strip().lower() not in _FALSY:
        logger.warning(
            "LIVE_TRADING_ENABLED=%r is not a recognised value — treating as OFF and locked",
            raw,
        )
    return LiveTradingState(enabled=False, source="env", locked=True)


async def live_trading_state() -> LiveTradingState:
    """Is real-money trading switched on right now? Fails closed.

    The single source of truth — see the module docstring.
    """
    env = _env_verdict()
    if env is not None:
        return env
    try:
        rec = await get_setting_store().get(SETTING_KEY)
    except Exception:
        logger.exception("trading_mode: cannot read the admin setting — failing CLOSED (paper)")
        return LiveTradingState(enabled=False, source="default")
    if rec is None:
        return LiveTradingState(enabled=False, source="default")
    return LiveTradingState(enabled=rec.value.strip().lower() == "true", source="admin")


async def last_change() -> SettingRecord | None:
    """Who/when of the stored value; None if never set or unreadable."""
    try:
        return await get_setting_store().get(SETTING_KEY)
    except Exception:
        logger.exception("trading_mode: cannot read the setting row")
        return None


async def set_live_trading(enabled: bool, *, changed_by: str) -> SettingRecord:
    """Persist the admin's choice and audit it. Raises on store errors —
    an admin must see a failed switch, never a silent one."""
    return await get_setting_store().set(
        SETTING_KEY, "true" if enabled else "false", changed_by=changed_by
    )
