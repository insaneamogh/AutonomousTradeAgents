"""platform_settings / platform_setting_audit: the runtime live-trading switch

Revision ID: 0022_platform_settings
Revises: 0021_paper_book
Create Date: 2026-11-13

An admin can switch the platform between paper and real-money trading
without a redeploy (app.services.platform.trading_mode). The value lives
in platform_settings; every change appends a platform_setting_audit row.

New tables only; nothing existing changes. No row is seeded: an absent
row means "paper".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_platform_settings"
down_revision: str | None = "0021_paper_book"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(320), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_table(
        "platform_setting_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.String(320), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_platform_setting_audit_key", "platform_setting_audit", ["key"])


def downgrade() -> None:
    op.drop_index("ix_platform_setting_audit_key", table_name="platform_setting_audit")
    op.drop_table("platform_setting_audit")
    op.drop_table("platform_settings")
