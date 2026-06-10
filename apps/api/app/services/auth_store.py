"""AuthStore — backing for users, sessions, magic-link tokens.

Same Protocol-+-Mock-+-Postgres-later pattern as ``app.services.store``.
The MockAuthStore is the live default while ``USE_POSTGRES`` is opt-in.
The Postgres impl wired against migrations 0001 + 0004 ships in a later
session.

Architectural note: refresh tokens are stored HASHED. We never write the
raw token. Rotation generates a fresh token, hashes it, swaps the row's
``refresh_token_hash``. A stolen DB can't replay an active session.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@dataclass
class UserRecord:
    id: str
    email: str
    auth_method: str = "magic_link"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    display_name: str | None = None


@dataclass
class SessionRecord:
    id: str
    user_id: str
    refresh_token_hash: str
    expires_at: datetime
    device_id: str | None = None
    device_label: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    revoked_at: datetime | None = None


@dataclass
class MagicLinkRecord:
    id: str
    email: str
    token_hash: str
    expires_at: datetime
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    used_at: datetime | None = None


@runtime_checkable
class AuthStore(Protocol):
    # ── users ─────────────────────────────────────────────────────────
    async def get_user_by_email(self, email: str) -> UserRecord | None: ...
    async def get_user_by_id(self, user_id: str) -> UserRecord | None: ...
    async def upsert_user(self, email: str, *, auth_method: str = "magic_link") -> UserRecord: ...

    # ── magic-link tokens ────────────────────────────────────────────
    async def create_magic_link(self, *, email: str, token_hash: str, expires_at: datetime) -> MagicLinkRecord: ...
    async def find_unused_magic_link(self, *, email: str) -> list[MagicLinkRecord]: ...
    async def mark_magic_link_used(self, magic_link_id: str) -> None: ...

    # ── sessions ─────────────────────────────────────────────────────
    async def create_session(
        self,
        *,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        device_id: str | None = None,
        device_label: str | None = None,
    ) -> SessionRecord: ...
    async def get_session(self, session_id: str) -> SessionRecord | None: ...
    async def rotate_session(self, session_id: str, *, new_refresh_token_hash: str) -> SessionRecord: ...
    async def revoke_session(self, session_id: str) -> None: ...


# ─────────────────────────────────────────────────────────────────────
# In-memory impl
# ─────────────────────────────────────────────────────────────────────


# Matches the fixture user id used by the existing MockStore so the
# DEV_AUTH_BYPASS path resolves to the same user the rest of the system
# already knows about.
FIXTURE_USER_ID: str = "00000000-0000-0000-0000-000000000001"
FIXTURE_USER_EMAIL: str = "dev@local.invalid"


class MockAuthStore:
    """Process-local auth store. Default in Phase 3 until Postgres ships."""

    def __init__(self) -> None:
        self._users: dict[str, UserRecord] = {}
        self._users_by_email: dict[str, str] = {}
        self._sessions: dict[str, SessionRecord] = {}
        self._magic_links: dict[str, MagicLinkRecord] = {}
        # Pre-seed the fixture user so DEV_AUTH_BYPASS works out of the box
        # against MockStore's hardcoded user id.
        self._seed_fixture()

    def _seed_fixture(self) -> None:
        u = UserRecord(
            id=FIXTURE_USER_ID,
            email=FIXTURE_USER_EMAIL,
            auth_method="dev_bypass",
        )
        self._users[u.id] = u
        self._users_by_email[u.email.lower()] = u.id

    # users -----------------------------------------------------------
    async def get_user_by_email(self, email: str) -> UserRecord | None:
        uid = self._users_by_email.get(email.lower())
        return self._users.get(uid) if uid else None

    async def get_user_by_id(self, user_id: str) -> UserRecord | None:
        return self._users.get(user_id)

    async def upsert_user(self, email: str, *, auth_method: str = "magic_link") -> UserRecord:
        existing = await self.get_user_by_email(email)
        if existing is not None:
            return existing
        u = UserRecord(
            id=str(uuid.uuid4()),
            email=email.lower(),
            auth_method=auth_method,
        )
        self._users[u.id] = u
        self._users_by_email[u.email] = u.id
        return u

    # magic links -----------------------------------------------------
    async def create_magic_link(
        self,
        *,
        email: str,
        token_hash: str,
        expires_at: datetime,
    ) -> MagicLinkRecord:
        m = MagicLinkRecord(
            id=str(uuid.uuid4()),
            email=email.lower(),
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._magic_links[m.id] = m
        return m

    async def find_unused_magic_link(self, *, email: str) -> list[MagicLinkRecord]:
        now = datetime.now(timezone.utc)
        return [
            m for m in self._magic_links.values()
            if m.email == email.lower() and m.used_at is None and m.expires_at > now
        ]

    async def mark_magic_link_used(self, magic_link_id: str) -> None:
        m = self._magic_links.get(magic_link_id)
        if m is not None:
            m.used_at = datetime.now(timezone.utc)

    # sessions --------------------------------------------------------
    async def create_session(
        self,
        *,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        device_id: str | None = None,
        device_label: str | None = None,
    ) -> SessionRecord:
        s = SessionRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
            device_id=device_id,
            device_label=device_label,
        )
        self._sessions[s.id] = s
        return s

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)

    async def rotate_session(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
    ) -> SessionRecord:
        s = self._sessions[session_id]
        s.refresh_token_hash = new_refresh_token_hash
        s.last_seen_at = datetime.now(timezone.utc)
        return s

    async def revoke_session(self, session_id: str) -> None:
        s = self._sessions.get(session_id)
        if s is not None:
            s.revoked_at = datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


_auth_store: AuthStore | None = None


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def get_auth_store() -> AuthStore:
    """Return the active auth store. Singleton per process.

    When ``USE_POSTGRES=1``, returns ``PostgresAuthStore``. Otherwise
    ``MockAuthStore`` (default). Switching is env-driven + idempotent.
    """
    global _auth_store
    if _auth_store is None:
        if _is_truthy(os.environ.get("USE_POSTGRES")):
            from app.services.postgres_auth_store import PostgresAuthStore

            _auth_store = PostgresAuthStore()
        else:
            _auth_store = MockAuthStore()
    return _auth_store


def reset_auth_store_for_tests() -> None:
    """Drop the singleton. Tests use this to start from a clean state."""
    global _auth_store
    _auth_store = None
