"""PostgresAuthStore — SQLAlchemy-backed AuthStore.

Wired against migrations 0001 (users) + 0004 (user_sessions, magic_link_tokens).
Drop-in for ``MockAuthStore`` — the routers don't care which one they got.

Idempotent fixture-user seed on first use so ``DEV_AUTH_BYPASS=1`` still
resolves under Postgres (the route reads ``get_user_by_id(FIXTURE_USER_ID)``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.auth_store import (
    FIXTURE_USER_EMAIL,
    FIXTURE_USER_ID,
    MagicLinkRecord,
    SessionRecord,
    UserRecord,
)
from engine.db import async_session_factory
from engine.db.models import MagicLinkToken, User, UserSession

logger = logging.getLogger("api.auth_store.postgres")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user_to_record(u: User) -> UserRecord:
    return UserRecord(
        id=str(u.id),
        email=u.email,
        auth_method=u.auth_method,
        created_at=u.created_at,
        display_name=u.display_name,
    )


def _session_to_record(s: UserSession) -> SessionRecord:
    return SessionRecord(
        id=str(s.id),
        user_id=str(s.user_id),
        refresh_token_hash=s.refresh_token_hash,
        expires_at=s.expires_at,
        device_id=s.device_id,
        device_label=s.device_label,
        created_at=s.created_at,
        last_seen_at=s.last_seen_at,
        revoked_at=s.revoked_at,
    )


def _magic_to_record(m: MagicLinkToken) -> MagicLinkRecord:
    return MagicLinkRecord(
        id=str(m.id),
        email=m.email,
        token_hash=m.token_hash,
        expires_at=m.expires_at,
        created_at=m.created_at,
        used_at=m.used_at,
    )


class PostgresAuthStore:
    def __init__(self) -> None:
        self._session_factory = async_session_factory()
        self._seeded = False

    async def _ensure_seed(self, session: AsyncSession) -> None:
        """Insert the fixture user once per process. Matches the seed in
        MockAuthStore so the DEV_AUTH_BYPASS path resolves the same way
        regardless of which store backs the request.
        """
        if self._seeded:
            return
        stmt = pg_insert(User).values(
            id=uuid.UUID(FIXTURE_USER_ID),
            email=FIXTURE_USER_EMAIL,
            auth_method="dev_bypass",
            display_name="Demo (dev_bypass)",
        ).on_conflict_do_nothing(index_elements=["id"])
        await session.execute(stmt)
        await session.commit()
        self._seeded = True

    # ── users ─────────────────────────────────────────────────────────

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            stmt = select(User).where(User.email == email.lower())
            u = (await session.execute(stmt)).scalar_one_or_none()
        return _user_to_record(u) if u is not None else None

    async def get_user_by_id(self, user_id: str) -> UserRecord | None:
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return None
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            u = await session.get(User, uid)
        return _user_to_record(u) if u is not None else None

    async def upsert_user(self, email: str, *, auth_method: str = "magic_link") -> UserRecord:
        email = email.strip().lower()
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            # Try insert; ON CONFLICT (email) returns existing row.
            stmt = pg_insert(User).values(
                id=uuid.uuid4(),
                email=email,
                auth_method=auth_method,
            ).on_conflict_do_nothing(index_elements=["email"]).returning(User.id)
            inserted_id = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()

            if inserted_id is None:
                # Existed already — fetch.
                row = (
                    await session.execute(select(User).where(User.email == email))
                ).scalar_one()
            else:
                row = await session.get(User, inserted_id)
                assert row is not None
        return _user_to_record(row)

    # ── magic links ──────────────────────────────────────────────────

    async def create_magic_link(
        self,
        *,
        email: str,
        token_hash: str,
        expires_at: datetime,
    ) -> MagicLinkRecord:
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            row = MagicLinkToken(
                id=uuid.uuid4(),
                email=email.lower(),
                token_hash=token_hash,
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _magic_to_record(row)

    async def find_unused_magic_link(self, *, email: str) -> list[MagicLinkRecord]:
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            now = _now()
            stmt = select(MagicLinkToken).where(
                MagicLinkToken.email == email.lower(),
                MagicLinkToken.used_at.is_(None),
                MagicLinkToken.expires_at > now,
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_magic_to_record(r) for r in rows]

    async def mark_magic_link_used(self, magic_link_id: str) -> None:
        try:
            mid = uuid.UUID(magic_link_id)
        except (ValueError, TypeError):
            return
        async with self._session_factory() as session:
            await session.execute(
                update(MagicLinkToken)
                .where(MagicLinkToken.id == mid, MagicLinkToken.used_at.is_(None))
                .values(used_at=_now())
            )
            await session.commit()

    # ── sessions ─────────────────────────────────────────────────────

    async def create_session(
        self,
        *,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        device_id: str | None = None,
        device_label: str | None = None,
    ) -> SessionRecord:
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            row = UserSession(
                id=uuid.uuid4(),
                user_id=uuid.UUID(user_id),
                refresh_token_hash=refresh_token_hash,
                expires_at=expires_at,
                device_id=device_id,
                device_label=device_label,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _session_to_record(row)

    async def get_session(self, session_id: str) -> SessionRecord | None:
        try:
            sid = uuid.UUID(session_id)
        except (ValueError, TypeError):
            return None
        async with self._session_factory() as session:
            row = await session.get(UserSession, sid)
        return _session_to_record(row) if row is not None else None

    async def rotate_session(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
    ) -> SessionRecord:
        sid = uuid.UUID(session_id)
        async with self._session_factory() as session:
            await session.execute(
                update(UserSession)
                .where(UserSession.id == sid)
                .values(refresh_token_hash=new_refresh_token_hash, last_seen_at=_now())
            )
            await session.commit()
            row = await session.get(UserSession, sid)
            assert row is not None
        return _session_to_record(row)

    async def revoke_session(self, session_id: str) -> None:
        try:
            sid = uuid.UUID(session_id)
        except (ValueError, TypeError):
            return
        async with self._session_factory() as session:
            await session.execute(
                update(UserSession)
                .where(UserSession.id == sid, UserSession.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            await session.commit()
