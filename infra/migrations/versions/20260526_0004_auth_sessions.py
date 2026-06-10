"""auth_sessions + magic_link_tokens — Phase 3 auth foundation

Revision ID: 0004_auth_sessions
Revises: 0003_agent_decisions
Create Date: 2026-05-26

PLAN.md §3 Phase 3 — mobile auth + biometric + push. This migration carries
the API-side foundation:

  - ALTER users: add ``auth_method`` (magic_link | password | oauth_alpaca)
    so we can tell which login route a user came in through.

  - CREATE user_sessions: one row per active refresh-token. Each device
    gets its own session row; refresh-token rotation swaps the row's
    ``refresh_token_hash`` and bumps ``last_seen_at``. Logout / revocation
    sets ``revoked_at`` — the table is the source of truth, NOT the
    refresh JWT itself (so a stolen refresh token can be revoked).

  - CREATE magic_link_tokens: single-use email login tokens. Hashed at
    rest (bcrypt) so a DB leak doesn't expose unsent tokens. ``used_at``
    locks the token after first verify so replays fail.

Postgres impls of AuthStore wired against these tables ship in a later
session; the in-memory ``MockAuthStore`` is the live default while
``USE_POSTGRES`` is opt-in.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_auth_sessions"
down_revision: str | None = "0003_agent_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── ALTER users — add auth_method ────────────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "auth_method",
            sa.String(length=20),
            nullable=False,
            server_default="magic_link",
        ),
    )

    # ── CREATE user_sessions ─────────────────────────────────────────
    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # bcrypt-hashed refresh token. We never store the raw token —
        # rotation generates a new opaque token, hashes it, swaps this in.
        sa.Column("refresh_token_hash", sa.String(length=255), nullable=False),
        sa.Column("device_id", sa.String(length=120), nullable=True),
        sa.Column("device_label", sa.String(length=120), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
    )
    op.create_index(
        "ix_user_sessions_user_id",
        "user_sessions",
        ["user_id"],
    )
    # Partial index covering the verify-refresh hot path: "active sessions
    # only". Saves a sequential scan once we accumulate revoked rows.
    op.create_index(
        "ix_user_sessions_active",
        "user_sessions",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ── CREATE magic_link_tokens ─────────────────────────────────────
    op.create_table(
        "magic_link_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "email",
            sa.String(length=255),
            nullable=False,
        ),
        # bcrypt hash of the opaque token. The raw token is emailed once
        # and never persisted in cleartext.
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # used_at locks single-use semantics. Replays after first verify
        # fail rather than minting another session.
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_magic_link_tokens"),
    )
    op.create_index(
        "ix_magic_link_tokens_email",
        "magic_link_tokens",
        ["email"],
    )
    # Partial index for the "pending verifications" lookup — same idea.
    op.create_index(
        "ix_magic_link_tokens_pending",
        "magic_link_tokens",
        ["expires_at"],
        postgresql_where=sa.text("used_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_magic_link_tokens_pending",
        table_name="magic_link_tokens",
    )
    op.drop_index(
        "ix_magic_link_tokens_email",
        table_name="magic_link_tokens",
    )
    op.drop_table("magic_link_tokens")
    op.drop_index(
        "ix_user_sessions_active",
        table_name="user_sessions",
    )
    op.drop_index(
        "ix_user_sessions_user_id",
        table_name="user_sessions",
    )
    op.drop_table("user_sessions")
    op.drop_column("users", "auth_method")
