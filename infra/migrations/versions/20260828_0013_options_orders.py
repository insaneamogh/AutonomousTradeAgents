"""options fields on orders

Revision ID: 0013_options_orders
Revises: 0012_decision_reasoning
Create Date: 2026-08-28

Options trading Phase A (long calls/puts only — see docs/OPTIONS_PLAN.md
and CLAUDE.md's scope table) needs three fields on ``orders`` that an
equity order never carries: whether it's an option at all, its contract
multiplier (100 for standard US equity options, vs. 1 for equities —
P&L and mark reconstruction elsewhere multiply/divide by this), and
which side of an open/close pair it was (``buy_to_open`` /
``sell_to_close``; ``orders.side`` itself stays plain "BUY"/"SELL" — the
open/close nuance lives here, not there, since ``side`` is only 4 chars
wide and pre-dates options entirely).

This is the only real schema migration options trading needs — every
other new field (the agent-decision proposal JSONB, the position
snapshot JSONB) uses the existing schema-less extension points instead.

``is_option``/``multiplier`` default to today's equity behavior
(``False``/``1``) so every existing row and every non-option code path
is completely unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_options_orders"
down_revision: str | None = "0012_decision_reasoning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("is_option", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "orders",
        sa.Column("multiplier", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "orders",
        sa.Column("option_action", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "option_action")
    op.drop_column("orders", "multiplier")
    op.drop_column("orders", "is_option")
