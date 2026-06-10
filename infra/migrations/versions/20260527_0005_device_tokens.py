"""device_tokens — push notification targets

Revision ID: 0005_device_tokens
Revises: 0004_auth_sessions
Create Date: 2026-05-27

PLAN.md §3 calls for push notifications on proposal-pending events. Each
mobile install registers ONE expo_push_token per (user, device). A user
with two phones gets two rows; reinstalling the app rotates the token,
so the same (user_id, expo_push_token) pair is idempotent on re-register.

Why this table is separate from ``user_sessions``:
  - A user can have an authenticated session WITHOUT push permission
    (the OS prompt may have been denied or not yet shown).
  - A user can have push permission WITHOUT an active session (the
    refresh token expired but the OS still routes a delivery).
  - Logout per-session shouldn't necessarily wipe push registration —
    the user may sign back in on the same device.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_device_tokens"
down_revision: str | None = "0004_auth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Expo push tokens look like ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx].
        # 255 chars covers the published spec + buffer.
        sa.Column("expo_push_token", sa.String(length=255), nullable=False),
        # 'ios' | 'android' | 'web'. Drives the platform-channel pick in
        # Expo's push API.
        sa.Column("platform", sa.String(length=16), nullable=False),
        # Display-only label. Pulled from Device.modelName when available
        # ("Amogh's iPhone"); free-form otherwise.
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Revoked on user-initiated disconnect OR when Expo Push returns
        # DeviceNotRegistered for the token (the OS uninstalled the app).
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_device_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_device_tokens"),
        # Idempotent register: same (user, token) won't double-insert.
        sa.UniqueConstraint(
            "user_id",
            "expo_push_token",
            name="uq_device_tokens_user_token",
        ),
    )
    op.create_index(
        "ix_device_tokens_user_id",
        "device_tokens",
        ["user_id"],
    )
    # Partial index for the "fan-out targets" hot query: active devices
    # for a given user. Reflection / proposal-pending hook reads this on
    # every council pass that produces a proposal.
    op.create_index(
        "ix_device_tokens_active",
        "device_tokens",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_device_tokens_active", table_name="device_tokens")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_table("device_tokens")
