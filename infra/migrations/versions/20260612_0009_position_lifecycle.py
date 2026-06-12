"""position lifecycle — exit modes, close tracking, per-user watchlist

Revision ID: 0009_position_lifecycle
Revises: 0008_ghost_outcomes
Create Date: 2026-06-12

Auto-mode groundwork (entries stay human-approved; exits may be delegated):

  agent_decisions.exit_mode     'agent' | 'manual' — chosen by the user on
                                the approval card. 'agent' lets the position
                                manager enforce time-stops + early exits;
                                'manual' means we only ever watch.
  agent_decisions.closed_at     When the position from this decision went
                                flat (any path).
  agent_decisions.close_reason  'agent_target' | 'agent_stop' |
                                'agent_time' | 'agent_signal' |
                                'user_manual' | 'external_broker' — named
                                like risk veto rules so the audit trail is
                                greppable.

  user_watchlist                The symbols a user told the agent to track.
                                asset_class is 'equity'-only in v1 — the
                                column exists so options can slot in later
                                without a rework.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_position_lifecycle"
down_revision: str | None = "0008_ghost_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_decisions",
        sa.Column(
            "exit_mode",
            sa.String(10),
            nullable=False,
            server_default="agent",
        ),
    )
    op.add_column(
        "agent_decisions",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_decisions",
        sa.Column("close_reason", sa.String(20), nullable=True),
    )

    op.create_table(
        "user_watchlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column(
            "asset_class",
            sa.String(10),
            nullable=False,
            server_default="equity",
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
    )
    op.create_index("ix_user_watchlist_user_id", "user_watchlist", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_watchlist_user_id", table_name="user_watchlist")
    op.drop_table("user_watchlist")
    op.drop_column("agent_decisions", "close_reason")
    op.drop_column("agent_decisions", "closed_at")
    op.drop_column("agent_decisions", "exit_mode")
