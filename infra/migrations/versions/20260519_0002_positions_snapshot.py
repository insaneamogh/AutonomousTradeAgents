"""positions_snapshot — reconciler-written portfolio cache

Revision ID: 0002_positions_snapshot
Revises: 0001_initial_schema
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_positions_snapshot"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "positions_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("account_equity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("cash", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("buying_power", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "open_positions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("daily_pnl", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("daily_pnl_pct", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_positions_snapshot_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_positions_snapshot"),
    )
    op.create_index(
        "ix_positions_snapshot_user_id_captured_at",
        "positions_snapshot",
        ["user_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_positions_snapshot_user_id_captured_at",
        table_name="positions_snapshot",
    )
    op.drop_table("positions_snapshot")
