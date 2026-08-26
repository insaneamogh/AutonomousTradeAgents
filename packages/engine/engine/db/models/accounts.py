"""Everything that hangs off a user account.

    users                App users — the tenant boundary every other table
                         keys off. ``user_id`` is the isolation unit.
    broker_connections   Per-user encrypted broker session (Alpaca, Zerodha).
    user_watchlist       Symbols the user told the agent to track (mig 0009).
    user_sessions        Phase 3 refresh-token sessions (mig 0004).
    magic_link_tokens    Single-use email login tokens (mig 0004).
    device_tokens        Expo push targets, one per device (mig 0005).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import false as text_false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from engine.db.base import Base

# ─────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Phase 3 (migration 0004): which auth path the user came in through.
    # 'magic_link' | 'password' | 'oauth_alpaca' | 'dev_bypass' | 'google'.
    auth_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="magic_link", server_default="magic_link"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ─────────────────────────────────────────────────────────────────────
# Broker connections
# ─────────────────────────────────────────────────────────────────────


class BrokerConnection(Base):
    """Encrypted broker credentials. One row per (user, broker, environment).

    Token columns store ciphertext. Encryption happens at the application
    boundary (``apps/api/app/core/crypto.py``) — never store plaintext.
    """

    __tablename__ = "broker_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "broker", "is_paper", name="uq_broker_connections_user_broker_env"),
        Index("ix_broker_connections_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    broker: Mapped[str] = mapped_column(String(20), nullable=False)  # 'alpaca'
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    account_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # Per-connection real-money opt-in (migration 0011). A live order needs
    # BOTH this AND the global LIVE_TRADING_ENABLED env. Default False.
    live_trading_consent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text_false()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────
# Watchlist — the symbols a user told the agent to track (migration 0009)
# ─────────────────────────────────────────────────────────────────────


class UserWatchlistItem(Base):
    """One row per (user, symbol) the daily council should evaluate.

    ``asset_class`` is 'equity'-only in v1 (US stocks + ETFs). The column
    exists so options can slot in later without a schema rework — the API
    rejects anything else until then.
    """

    __tablename__ = "user_watchlist"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
        Index("ix_user_watchlist_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_class: Mapped[str] = mapped_column(
        String(10), nullable=False, default="equity", server_default="equity"
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────
# Phase 3 auth — sessions + magic links (migration 0004)
# ─────────────────────────────────────────────────────────────────────


class UserSession(Base):
    """One row per active refresh-token. Each device gets its own session
    row; refresh rotation swaps ``refresh_token_hash`` and bumps
    ``last_seen_at``. Logout sets ``revoked_at`` — the table is the
    source of truth, NOT the JWT itself (so a stolen refresh can be
    revoked even when the JWT hasn't expired).
    """

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # bcrypt/scrypt hash — never the raw token.
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    device_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MagicLinkToken(Base):
    """Single-use email login tokens. Hashed at rest (scrypt). ``used_at``
    locks the row after first verify so replays fail.
    """

    __tablename__ = "magic_link_tokens"
    __table_args__ = (Index("ix_magic_link_tokens_email", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ─────────────────────────────────────────────────────────────────────
# Phase 3 notifications — device push tokens (migration 0005)
# ─────────────────────────────────────────────────────────────────────


class DeviceToken(Base):
    """Expo push tokens per (user, device). Idempotent UQ on
    (user_id, expo_push_token) — re-registering the same device hits
    the existing row.
    """

    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "expo_push_token", name="uq_device_tokens_user_token"),
        Index("ix_device_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expo_push_token: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # 'ios' | 'android' | 'web'
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
