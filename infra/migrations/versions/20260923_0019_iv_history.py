"""iv_history: one constant-maturity ATM IV snapshot per underlying per day

Revision ID: 0019_iv_history
Revises: 0018_watchlist_source
Create Date: 2026-09-23

`options_context.iv_rank` has been None since options shipped because there
was no IV history to rank against, and the free Alpaca tier cannot supply
one retroactively. It has to be recorded daily. See
engine.db.models.market.IvHistory and trading_agents.jobs.iv_snapshot.

A new table only; nothing existing changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_iv_history"
down_revision: str | None = "0018_watchlist_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "iv_history",
        sa.Column("symbol", sa.String(20), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("atm_iv_30d", sa.Numeric(8, 5), nullable=True),
        sa.Column("atm_iv_60d", sa.Numeric(8, 5), nullable=True),
        sa.Column("n_expiries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("feed", sa.String(20), nullable=False),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("iv_history")
