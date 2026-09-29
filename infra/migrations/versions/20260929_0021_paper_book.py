"""paper_accounts / paper_positions / paper_orders: a persisted paper book

Revision ID: 0021_paper_book
Revises: 0020_symbol_length
Create Date: 2026-09-29

Zerodha has no paper account, so India paper trading runs a simulated book
against Kite's real quotes (app.services.orders.kite_paper). The old
in-memory paper engine reset on every redeploy; a forward track record
cannot. See engine.db.models.paper.

New tables only; nothing existing changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_paper_book"
down_revision: str | None = "0020_symbol_length"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    def user_fk() -> sa.ForeignKey:
        return sa.ForeignKey("users.id", ondelete="CASCADE")

    op.create_table(
        "paper_accounts",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), user_fk(), primary_key=True),
        sa.Column("broker", sa.String(20), primary_key=True),
        sa.Column("cash", sa.Numeric(18, 2), nullable=False),
        sa.Column("starting_cash", sa.Numeric(18, 2), nullable=False),
        sa.Column("charges_paid", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_table(
        "paper_positions",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), user_fk(), primary_key=True),
        sa.Column("broker", sa.String(20), primary_key=True),
        sa.Column("symbol", sa.String(40), primary_key=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("avg_entry_price", sa.Numeric(18, 4), nullable=False),
    )
    op.create_table(
        "paper_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), user_fk(), nullable=False),
        sa.Column("broker", sa.String(20), nullable=False),
        sa.Column("broker_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("client_order_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("side", sa.String(20), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("order_type", sa.String(20), nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("stop_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("gtt_legs", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("filled_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_fill_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("filled_leg", sa.String(10), nullable=True),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_paper_orders_user_id", "paper_orders", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_paper_orders_user_id", table_name="paper_orders")
    op.drop_table("paper_orders")
    op.drop_table("paper_positions")
    op.drop_table("paper_accounts")
